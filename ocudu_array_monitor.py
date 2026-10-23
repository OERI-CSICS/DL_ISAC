#!/usr/bin/env python3
"""
OCUDU Optimized Low-Latency Flexible Array Monitor (PyQtGraph).

Design
------
- Supports 1..4 selectable RX channels on X410 or N310.
- Uses one aligned timed UHD stream for all selected channels.
- Opens the GUI immediately.
- Runs capture, synchronization, and channel estimation in a worker thread.
- Uses a latest-state mailbox rather than a rendering queue.
- Drops stale visual frames automatically.
- Uses a QTimer-driven PyQtGraph dashboard for low-latency rendering.
- Keeps raw RF activity, synchronized amplitude/phase profiles, waterfall,
  residual scores, and relative direction trend on screen.
- Supports optional terminal-only mode, compact metrics, and related AI/ML CSV datasets.

The signal-processing core is imported from the proven Step 4 package.
Legacy duplicate monitor variants were consolidated into this single entry point.
The monitor is passive receive-only. Use only in an authorized lab/testbed.
"""

from __future__ import annotations

import argparse
import csv
from collections import deque
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Optional

import numpy as np

import isac_core as default_core
import nr_pss as default_step1
import nr_cell_sync as default_step2
import nr_sync as default_sync
from usrp_io import DEFAULT_DEVICE_ARGS, device_label
from dataset_recorder import MLDatasetRecorder, mean_angle

SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Imports and helper loading
# ---------------------------------------------------------------------------

def now_utc() -> str:
    """Handle the now utc operation for this OCUDU processing stage."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Latest-state mailbox: old GUI frames are replaced, not queued.
# ---------------------------------------------------------------------------

@dataclass
class MonitorSnapshot:
    """Represent monitor snapshot state and behavior used by the OCUDU pipeline."""
    sequence: int = 0
    raw_sequence: int = 0
    sync_sequence: int = 0

    startup_stage: str = "INITIALIZING"
    status: str = "WAITING FOR RX"
    detail: str = ""
    labels: list[str] = field(default_factory=list)

    windows: int = 0
    locks: int = 0
    rejected: int = 0
    calibration_count: int = 0
    calibration_target: int = 0
    stillness_count: int = 0
    stillness_target: int = 0

    raw_rms_db: Optional[np.ndarray] = None
    amp_profiles: Optional[np.ndarray] = None
    phase_profiles: Optional[np.ndarray] = None
    event_row: Optional[np.ndarray] = None
    antenna_scores_db: Optional[np.ndarray] = None
    spatial_scores_db: Optional[np.ndarray] = None

    fused_score_db: float = 0.0
    direction_value_rad: float = 0.0
    direction: str = "WAITING"
    pci: Optional[int] = None
    cfo_hz: float = 0.0
    pss_corr: float = 0.0
    sss_corr: float = 0.0
    similarity: float = 0.0
    threshold_on_db: Optional[float] = None
    threshold_off_db: Optional[float] = None

    # Synchronization continuity/health. These fields distinguish a brief
    # weak-signal dropout from a true loss of the selected PCI.
    lock_health: str = "SEARCHING"
    consecutive_rejects: int = 0
    lock_duty_cycle_pct: float = 0.0
    seconds_since_lock: Optional[float] = None
    last_rejection_reason: str = ""
    sync_source_index: Optional[int] = None
    sync_source_label: str = ""
    sync_fallback_uses: int = 0

    # The waterfall row to draw after display policy has been applied.
    waterfall_row: Optional[np.ndarray] = None


class LatestState:
    """Thread-safe latest-state mailbox plus loss-bounded waterfall row queue.

    Scalar/curve state uses latest-value semantics so a slow GUI can never
    back-pressure RF processing. Waterfall rows are different: every accepted
    sensing window carries time history, so a bounded deque preserves the
    intermediate rows that would otherwise disappear between GUI refreshes.
    """
    def __init__(self, labels: list[str], waterfall_capacity: int = 512):
        """Initialize the mailbox and bounded waterfall queue."""
        self._lock = threading.Lock()
        self._snapshot = MonitorSnapshot(labels=list(labels))
        self._waterfall_rows = deque(maxlen=max(8, int(waterfall_capacity)))
        self._waterfall_dropped_total = 0

    def update(self, **changes) -> None:
        """Publish new state without allowing rendering to block sensing."""
        with self._lock:
            row = changes.get("waterfall_row")
            if row is not None:
                if len(self._waterfall_rows) == self._waterfall_rows.maxlen:
                    self._waterfall_dropped_total += 1
                self._waterfall_rows.append(np.asarray(row, dtype=np.float32).copy())

            current = self._snapshot
            data = dict(current.__dict__)
            data.update(changes)
            data["sequence"] = int(current.sequence) + 1
            self._snapshot = MonitorSnapshot(**data)

    def drain_waterfall_rows(self) -> tuple[np.ndarray, int]:
        """Return all queued waterfall rows in sensing order and clear the queue."""
        with self._lock:
            dropped = int(self._waterfall_dropped_total)
            self._waterfall_dropped_total = 0
            if not self._waterfall_rows:
                return np.empty((0, 127), dtype=np.float32), dropped
            rows = np.stack(tuple(self._waterfall_rows), axis=0)
            self._waterfall_rows.clear()
            return rows, dropped

    def get(self) -> MonitorSnapshot:
        """Return a thread-safe copy of the latest state."""
        with self._lock:
            s = self._snapshot
            # Copy arrays so the GUI never sees worker-owned mutable buffers.
            data = dict(s.__dict__)
            for key in [
                "raw_rms_db",
                "amp_profiles",
                "phase_profiles",
                "event_row",
                "antenna_scores_db",
                "spatial_scores_db",
                "waterfall_row",
            ]:
                value = data.get(key)
                if value is not None:
                    data[key] = np.asarray(value).copy()
            return MonitorSnapshot(**data)


@dataclass(frozen=True)
class LockContinuityDecision:
    """Display and detector-continuity decision after a rejected window."""

    status: str
    startup_stage: str
    lock_health: str
    consecutive_rejects: int
    seconds_since_lock: Optional[float]
    hold_last_state: bool
    reset_previous_channel: bool


class LockContinuityController:
    """Prevent one weak synchronization window from erasing a valid state.

    A distant cell can be detected in one capture and missed in the next because
    of low SNR, fading, or because the capture window does not contain an SSB
    occasion.  The controller keeps the most recent valid detector status for a
    short, bounded grace interval.  After the grace interval it explicitly
    reports reacquisition and requests the temporal channel reference be reset,
    avoiding a false motion event when the cell returns after a long gap.
    """

    def __init__(
        self,
        *,
        pci: int,
        grace_windows: int,
        grace_seconds: float,
        reset_windows: int,
        reset_seconds: float,
        health_window: int,
    ):
        """Initialize the LockContinuityController instance and its runtime state."""
        self.pci = int(pci)
        self.grace_windows = max(0, int(grace_windows))
        self.grace_seconds = max(0.0, float(grace_seconds))
        self.reset_windows = max(self.grace_windows + 1, int(reset_windows))
        self.reset_seconds = max(self.grace_seconds, float(reset_seconds))
        self.history = deque(maxlen=max(1, int(health_window)))
        self.last_lock_time: Optional[float] = None
        self.last_valid_status: str = "SEARCHING FOR PCI LOCK"
        self.consecutive_rejects = 0
        self._previous_reset_issued = False

    @property
    def duty_cycle_pct(self) -> float:
        """Handle the duty cycle pct operation for this OCUDU processing stage."""
        if not self.history:
            return 0.0
        return 100.0 * float(sum(self.history)) / float(len(self.history))

    def accepted(self, status: str, now: Optional[float] = None) -> None:
        """Record a successful synchronization decision."""
        t = time.monotonic() if now is None else float(now)
        self.history.append(True)
        self.last_lock_time = t
        self.last_valid_status = str(status)
        self.consecutive_rejects = 0
        self._previous_reset_issued = False

    def rejected(self, now: Optional[float] = None) -> LockContinuityDecision:
        """Record a rejected synchronization window and return the continuity decision."""
        t = time.monotonic() if now is None else float(now)
        self.history.append(False)
        self.consecutive_rejects += 1

        if self.last_lock_time is None:
            return LockContinuityDecision(
                status="SEARCHING FOR PCI LOCK",
                startup_stage="INITIAL PCI ACQUISITION",
                lock_health="SEARCHING",
                consecutive_rejects=self.consecutive_rejects,
                seconds_since_lock=None,
                hold_last_state=False,
                reset_previous_channel=False,
            )

        age = max(0.0, t - self.last_lock_time)
        within_window_grace = self.consecutive_rejects <= self.grace_windows
        within_time_grace = age <= self.grace_seconds
        if within_window_grace and within_time_grace:
            return LockContinuityDecision(
                status=self.last_valid_status,
                startup_stage=(
                    f"WEAK LOCK HOLD {self.consecutive_rejects}/"
                    f"{self.grace_windows}"
                ),
                lock_health="HOLDING LAST VALID STATE",
                consecutive_rejects=self.consecutive_rejects,
                seconds_since_lock=age,
                hold_last_state=True,
                reset_previous_channel=False,
            )

        should_reset = (
            self.consecutive_rejects >= self.reset_windows
            or age >= self.reset_seconds
        )
        issue_reset = should_reset and not self._previous_reset_issued
        if issue_reset:
            self._previous_reset_issued = True

        return LockContinuityDecision(
            status=f"REACQUIRING PCI {self.pci}",
            startup_stage="SELECTED CELL LOCK LOST",
            lock_health="REACQUIRING",
            consecutive_rejects=self.consecutive_rejects,
            seconds_since_lock=age,
            hold_last_state=False,
            reset_previous_channel=issue_reset,
        )


# ---------------------------------------------------------------------------
# Processing worker
# ---------------------------------------------------------------------------

class ProcessingWorker(threading.Thread):
    """Represent processing worker state and behavior used by the OCUDU pipeline."""
    def __init__(
        self,
        args,
        latest: LatestState,
        core: ModuleType,
        step1: ModuleType,
        step2: ModuleType,
        sync: ModuleType,
    ):
        """Initialize the ProcessingWorker instance and its runtime state."""
        super().__init__(name="ocudu-processing-worker", daemon=True)
        self.args = args
        self.latest = latest
        self.core = core
        self.step1 = step1
        self.step2 = step2
        self.sync = sync
        self.stop_event = threading.Event()
        self.receiver = None
        self.csv_file = None
        self.csv_writer = None
        self.dataset_recorder: Optional[MLDatasetRecorder] = None

    def request_stop(self) -> None:
        """Request a cooperative stop without blocking the caller."""
        self.stop_event.set()

    def _open_csv(self, labels: list[str]) -> None:
        """Internal helper for open csv."""
        if not self.args.save_metrics:
            return
        path = Path(self.args.save_metrics).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_file = path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)

        header = [
            "timestamp_utc",
            "windows",
            "locks",
            "selected_mcc",
            "selected_mnc",
            "selected_plmn",
            "selected_operator_name",
            "selected_operator_source",
            "pci",
            "cfo_hz",
            "pss_corr",
            "sss_corr",
            "similarity",
            "status",
            "direction",
            "direction_value_rad",
            "fused_score_db",
            "lock_health",
            "consecutive_rejects",
            "lock_duty_cycle_pct",
            "seconds_since_lock",
            "last_rejection_reason",
            "sync_source_index",
            "sync_source_label",
            "sync_fallback_uses",
        ]
        header += [f"{label}_raw_rms_db" for label in labels]
        header += [f"{label}_event_score_db" for label in labels]
        header += [f"{label}_spatial_score_db" for label in labels]
        self.csv_writer.writerow(header)
        self.csv_file.flush()
        print(f"[CSV] Saving compact metrics to: {path}")

    def _write_csv(
        self,
        snapshot: MonitorSnapshot,
    ) -> None:
        """Internal helper for write csv."""
        if self.csv_writer is None:
            return

        raw = [] if snapshot.raw_rms_db is None else list(snapshot.raw_rms_db)
        antenna = [] if snapshot.antenna_scores_db is None else list(snapshot.antenna_scores_db)
        spatial = [] if snapshot.spatial_scores_db is None else list(snapshot.spatial_scores_db)

        row = [
            now_utc(),
            snapshot.windows,
            snapshot.locks,
            self.args.selected_mcc,
            self.args.selected_mnc,
            self.args.selected_plmn,
            self.args.selected_operator_name,
            self.args.selected_operator_source,
            "" if snapshot.pci is None else snapshot.pci,
            f"{snapshot.cfo_hz:.6f}",
            f"{snapshot.pss_corr:.6f}",
            f"{snapshot.sss_corr:.6f}",
            f"{snapshot.similarity:.6f}",
            snapshot.status,
            snapshot.direction,
            f"{snapshot.direction_value_rad:.6f}",
            f"{snapshot.fused_score_db:.6f}",
            snapshot.lock_health,
            snapshot.consecutive_rejects,
            f"{snapshot.lock_duty_cycle_pct:.3f}",
            "" if snapshot.seconds_since_lock is None else f"{snapshot.seconds_since_lock:.6f}",
            snapshot.last_rejection_reason,
            "" if snapshot.sync_source_index is None else snapshot.sync_source_index,
            snapshot.sync_source_label,
            snapshot.sync_fallback_uses,
        ]
        row += [f"{v:.6f}" for v in raw]
        row += [f"{v:.6f}" for v in antenna]
        row += [f"{v:.6f}" for v in spatial]
        self.csv_writer.writerow(row)

        if snapshot.locks % max(1, int(self.args.csv_flush_every)) == 0:
            self.csv_file.flush()

    def _open_dataset_recorder(
        self,
        labels: list[str],
        ssb_freq: float,
        window_samples: int,
    ) -> None:
        """Create the related CSV tables used for AI/ML training."""
        if not self.args.record_dataset:
            return
        if self.receiver is None:
            raise RuntimeError("The USRP receiver must exist before dataset recording starts.")

        metadata = {
            "application": "CSICS - Passive ISAC",
            "selected_mcc": self.args.selected_mcc,
            "selected_mnc": self.args.selected_mnc,
            "selected_plmn": self.args.selected_plmn,
            "selected_operator_name": self.args.selected_operator_name,
            "selected_operator_source": self.args.selected_operator_source,
            "expected_pci": self.args.pci,
            "selected_gscn": self.args.selected_gscn,
            "ssb_frequency_hz": ssb_freq,
            "dl_frequency_hz": self.args.dl_frequency_hz,
            "detector_rate_hz": self.args.detector_rate,
            "subcarrier_spacing_hz": self.args.scs,
            "burst_ms": self.args.burst_ms,
            "window_samples": window_samples,
            "primary_index": self.args.primary_index,
            "array_spacing_m": self.args.spacing_m,
            "dataset_label": self.args.dataset_label,
            "subject_id": self.args.subject_id,
            "scenario": self.args.scenario,
            "session_notes": self.args.session_notes,
        }
        self.dataset_recorder = MLDatasetRecorder(
            output_dir=self.args.record_dataset,
            labels=labels,
            hardware_channels=self.args.channels,
            antennas=self.receiver.actual_antennas,
            gains_db=self.receiver.actual_gains,
            rates_hz=self.receiver.actual_rates,
            frequencies_hz=self.receiver.actual_freqs,
            static_metadata=metadata,
            record_iq_csv=bool(self.args.record_iq_csv),
            iq_record_source=self.args.iq_record_source,
            iq_decimation=int(self.args.iq_csv_decimation),
            iq_rows_per_file=int(self.args.iq_csv_rows_per_file),
            iq_gzip=bool(self.args.iq_csv_gzip),
            queue_size=int(self.args.record_queue_size),
            overflow_policy=self.args.record_overflow_policy,
        )

        if self.args.record_iq_csv and self.args.iq_record_source == "continuous":
            self.receiver.chunk_callback = self.dataset_recorder.enqueue_continuous_iq

        print(f"[DATASET] Recording related CSV tables in: {self.dataset_recorder.output_dir}")
        if self.args.record_iq_csv:
            print(
                "[DATASET] Raw IQ CSV enabled: "
                f"source={self.args.iq_record_source}, "
                f"decimation={self.args.iq_csv_decimation}, "
                f"overflow={self.args.record_overflow_policy}; "
                "IQ capture starts only after baseline and stillness calibration. "
                "During live RF capture it is written to a fast binary spool; "
                "when the monitor stops, the spool is converted to the single "
                "final file iq/iq_samples.csv."
            )

    def _record_dataset_window(
        self,
        *,
        labels: list[str],
        windows: int,
        locks: int,
        rejected: int,
        generation: int,
        sample_start: int,
        sample_end: int,
        raw: np.ndarray,
        raw_levels: np.ndarray,
        snapshot: MonitorSnapshot,
        result,
        accepted: bool,
        rejection_reason: str,
        sensing: Optional[np.ndarray] = None,
        spatial: Optional[np.ndarray] = None,
        similarities: Optional[list[float]] = None,
        activity=None,
        include_iq: bool = False,
    ) -> None:
        """Record one processing window and optionally its post-calibration IQ.

        Feature/quality rows can describe calibration or rejected windows. Raw
        IQ is included only when ``include_iq`` is true, which the processing
        loop sets for accepted windows after stillness calibration has already
        completed.
        """
        recorder = self.dataset_recorder
        if recorder is None or self.receiver is None:
            return

        timestamp = now_utc()
        similarity_values = [] if similarities is None else list(similarities)
        mean_similarity = (
            float(np.mean(similarity_values)) if similarity_values else float(snapshot.similarity)
        )
        result_value = lambda name, default="": (
            getattr(result, name, default) if result is not None else default
        )
        window_row = {
            "window_id": int(windows),
            "timestamp_utc": timestamp,
            "capture_generation": int(generation),
            "sample_start": int(sample_start),
            "sample_end_exclusive": int(sample_end),
            "samples_per_channel": int(raw.shape[1]),
            "sample_rate_hz": float(self.receiver.actual_rates[0]),
            "iq_record_source": self.args.iq_record_source,
            "dataset_label": self.args.dataset_label,
            "subject_id": self.args.subject_id,
            "scenario": self.args.scenario,
            "accepted": bool(accepted),
            "status": snapshot.status,
            "startup_stage": snapshot.startup_stage,
            "lock_health": snapshot.lock_health,
            "windows_seen": int(windows),
            "locks_seen": int(locks),
            "rejected_seen": int(rejected),
            "expected_pci": int(self.args.pci),
            "detected_pci": result_value("pci"),
            "nid1": result_value("nid1"),
            "nid2": result_value("nid2"),
            "cfo_hz": result_value("cfo_hz"),
            "pss_corr": result_value("pss_corr"),
            "sss_corr": result_value("sss_corr"),
            "sss_peak_ratio": result_value("sss_peak_ratio"),
            "cp_metric": result_value("cp_metric"),
            "coarse_time_ms": result_value("coarse_time_ms"),
            "coarse_peak_over_threshold": result_value("coarse_peak_over_threshold"),
            "coarse_peak_over_median": result_value("coarse_peak_over_median"),
            "pss_useful_start": result_value("pss_useful_start"),
            "sss_useful_start": result_value("sss_useful_start"),
            "sss_timing_delta": result_value("sss_timing_delta"),
            "ssb_bin_offset": result_value("ssb_bin_offset"),
            "sync_source_index": "" if snapshot.sync_source_index is None else snapshot.sync_source_index,
            "sync_source_label": snapshot.sync_source_label,
            "sync_fallback_uses": snapshot.sync_fallback_uses,
            "similarity_mean": mean_similarity,
            "fused_score_db": snapshot.fused_score_db,
            "direction": snapshot.direction,
            "direction_value_rad": snapshot.direction_value_rad,
            "threshold_on_db": "" if snapshot.threshold_on_db is None else snapshot.threshold_on_db,
            "threshold_off_db": "" if snapshot.threshold_off_db is None else snapshot.threshold_off_db,
            "calibration_count": snapshot.calibration_count,
            "calibration_target": snapshot.calibration_target,
            "stillness_count": snapshot.stillness_count,
            "stillness_target": snapshot.stillness_target,
            "lock_duty_cycle_pct": snapshot.lock_duty_cycle_pct,
            "consecutive_rejects": snapshot.consecutive_rejects,
            "seconds_since_lock": "" if snapshot.seconds_since_lock is None else snapshot.seconds_since_lock,
            "rejection_reason": rejection_reason,
        }

        channel_rows = []
        for m, label in enumerate(labels):
            sensing_m = None if sensing is None else np.asarray(sensing[m])
            spatial_m = None if spatial is None else np.asarray(spatial[m])
            channel_rows.append(
                {
                    "window_id": int(windows),
                    "timestamp_utc": timestamp,
                    "dataset_label": self.args.dataset_label,
                    "subject_id": self.args.subject_id,
                    "scenario": self.args.scenario,
                    "channel_array_index": m,
                    "hardware_channel": self.args.channels[m],
                    "channel_label": label,
                    "antenna": self.receiver.actual_antennas[m],
                    "gain_db": self.receiver.actual_gains[m],
                    "center_frequency_hz": self.receiver.actual_freqs[m],
                    "sample_rate_hz": self.receiver.actual_rates[m],
                    "is_sync_source": int(snapshot.sync_source_index == m),
                    "raw_rms_db": float(raw_levels[m]),
                    "event_score_db": "" if activity is None else float(activity.antenna_scores_db[m]),
                    "spatial_score_db": "" if activity is None else float(activity.spatial_scores_db[m]),
                    "pss_sss_similarity": "" if m >= len(similarity_values) else float(similarity_values[m]),
                    "sensing_magnitude_mean": "" if sensing_m is None else float(np.mean(np.abs(sensing_m))),
                    "sensing_magnitude_std": "" if sensing_m is None else float(np.std(np.abs(sensing_m))),
                    "sensing_phase_mean_rad": "" if sensing_m is None else mean_angle(sensing_m),
                    "spatial_magnitude_mean": "" if spatial_m is None else float(np.mean(np.abs(spatial_m))),
                    "spatial_magnitude_std": "" if spatial_m is None else float(np.std(np.abs(spatial_m))),
                    "spatial_phase_mean_rad": "" if spatial_m is None else mean_angle(spatial_m),
                }
            )

        subcarrier_rows = []
        if accepted and sensing is not None and spatial is not None and activity is not None:
            for m, label in enumerate(labels):
                for k in range(int(sensing.shape[1])):
                    subcarrier_rows.append(
                        {
                            "window_id": int(windows),
                            "timestamp_utc": timestamp,
                            "dataset_label": self.args.dataset_label,
                            "subject_id": self.args.subject_id,
                            "scenario": self.args.scenario,
                            "channel_array_index": m,
                            "hardware_channel": self.args.channels[m],
                            "channel_label": label,
                            "subcarrier_index": k,
                            "sensing_i": float(np.real(sensing[m, k])),
                            "sensing_q": float(np.imag(sensing[m, k])),
                            "spatial_i": float(np.real(spatial[m, k])),
                            "spatial_q": float(np.imag(spatial[m, k])),
                            "amplitude_profile_db": float(activity.amp_profiles[m, k]),
                            "phase_profile_rad": float(activity.phase_profiles[m, k]),
                            "spatial_phase_profile_rad": float(activity.spatial_phase_profiles[m, k]),
                            "fused_event_residual_db": float(activity.event_row[k]),
                        }
                    )

        recorder.record_window(
            window_row=window_row,
            raw_iq=raw,
            channel_rows=channel_rows,
            subcarrier_rows=subcarrier_rows,
            include_iq=bool(include_iq),
        )

    def run(self) -> None:
        """Run this worker until completion or a stop request is received."""
        args = self.args
        core = self.core
        step1 = self.step1
        step2 = self.step2
        sync = self.sync

        try:
            nch = len(args.channels)
            labels = list(args.element_labels)
            positions_m = np.arange(nch, dtype=np.float64) * float(args.spacing_m)

            if args.dl_frequency_hz is not None:
                dl_freq = float(args.dl_frequency_hz)
            elif args.selected_gscn is not None:
                # PSS/SSS discovery identifies SS_REF but not the NR carrier
                # center.  PBCH/MIB/SIB1 decoding is a later milestone.
                dl_freq = None
            else:
                dl_freq = step1.nr_arfcn_to_freq_hz(args.dl_arfcn)
            ssb_freq = (
                float(args.ssb_frequency_hz)
                if args.ssb_frequency_hz is not None
                else step1.nr_arfcn_to_freq_hz(args.ssb_arfcn)
            )
            window_samples = int(round(args.burst_ms * 1e-3 * args.rate))

            calibration = core.Calibration(
                target=int(args.baseline_locks),
                nch=nch,
                primary_index=int(args.primary_index),
            )
            detector = core.EventDetector(
                calibration_locks=int(args.event_calibration_locks),
                threshold_sigma=float(args.event_threshold_sigma),
                minimum_threshold_db=float(args.event_min_threshold_db),
                off_ratio=float(args.event_off_ratio),
            )

            self.latest.update(
                startup_stage="CONFIGURING USRP",
                status="INITIALIZING",
                detail=(
                    f"SSB={ssb_freq / 1e6:.6f} MHz | "
                    f"GSCN={args.selected_gscn if args.selected_gscn is not None else '-'} | "
                    f"PLMN={args.selected_plmn or 'Unknown'} | "
                    f"operator={args.selected_operator_name or 'Unknown'} | "
                    f"channels={args.channels} | primary={args.primary_index}"
                ),
                calibration_target=calibration.target,
                stillness_target=detector.calibration_locks,
            )

            self.receiver = core.MultiChannelContinuousX410Receiver(
                step1=step1,
                device_args=args.device_args,
                rate=float(args.rate),
                freq=float(ssb_freq),
                gains=args.gains,
                antennas=args.antennas,
                channels=args.channels,
                settle_time=float(args.settle_time),
                window_samples=window_samples,
                ring_windows=int(args.ring_windows),
                stream_start_delay_s=float(args.stream_start_delay_s),
                recv_timeout_s=float(args.rx_timeout_s),
                timeout_warning_interval_s=float(args.rx_timeout_warning_interval_s),
            )

            print("========== CSICS - Passive ISAC Array Monitor ==========")
            if dl_freq is None:
                print("DL carrier center:       unknown (SSB discovery only)")
            else:
                print(f"DL carrier center:       {dl_freq / 1e6:.6f} MHz")
            print(f"SSB center:              {ssb_freq / 1e6:.6f} MHz")
            if args.selected_gscn is not None:
                print(f"Selected GSCN:           {args.selected_gscn}")
            print(f"Selected MCC:            {args.selected_mcc or 'Unknown'}")
            print(f"Selected MNC:            {args.selected_mnc or 'Unknown'}")
            print(f"Selected PLMN:           {args.selected_plmn or 'Unknown'}")
            print(f"Selected operator:       {args.selected_operator_name or 'Unknown'}")
            print(f"Operator metadata source:{' ' if args.selected_operator_source else ''}{args.selected_operator_source or 'Unmapped'}")
            print(f"Selected channels:       {args.channels}")
            print(f"Array labels:            {labels}")
            print(f"Antennas:                {args.antennas}")
            print(f"Gains:                   {args.gains} dB")
            print(f"Primary sync element:    {args.primary_index} ({labels[args.primary_index]})")
            print("GUI architecture:        PyQtGraph latest-state rendering")
            print("Frame policy:            newest state only; stale visual states discarded")
            print("Press Ctrl+C to stop.")

            # Create the dataset recorder before streaming so continuous IQ
            # mode can observe the first aligned packet from every channel.
            self._open_dataset_recorder(labels, ssb_freq, window_samples)
            self.receiver.start()
            self._open_csv(labels)

            self.latest.update(
                startup_stage="RX STREAM ACTIVE",
                status="WAITING FOR FIRST RX WINDOW",
                detail=(
                    f"actual rates={[round(r / 1e6, 6) for r in self.receiver.actual_rates]} MS/s"
                ),
            )

            last_generation = -1
            windows = 0
            locks = 0
            rejected = 0
            sync_fallback_uses = 0
            previous_sensing = None
            previous_spatial = None
            lock_continuity = LockContinuityController(
                pci=int(args.pci),
                grace_windows=int(args.lock_grace_windows),
                grace_seconds=float(args.lock_grace_seconds),
                reset_windows=int(args.lock_reference_reset_windows),
                reset_seconds=float(args.lock_reference_reset_seconds),
                health_window=int(args.lock_health_window),
            )

            while not self.stop_event.is_set():
                raw, generation, sample_start, sample_end = self.receiver.latest_window(
                    last_generation=last_generation,
                    timeout_s=float(args.snapshot_timeout_s),
                    include_sample_range=True,
                )
                if raw is None:
                    continue

                last_generation = generation
                windows += 1
                raw_levels = np.asarray(
                    [core.rms_db(raw[m]) for m in range(nch)],
                    dtype=np.float32,
                )

                # Publish raw activity immediately, before synchronization.
                self.latest.update(
                    raw_sequence=windows,
                    windows=windows,
                    raw_rms_db=raw_levels,
                    startup_stage="RAW ACTIVITY LIVE",
                    detail=f"windows={windows} locks={locks} rejected={rejected}",
                )

                primary_index = int(args.primary_index)
                sync_order = [primary_index]
                if bool(args.sync_fallback_channels) and nch > 1:
                    remaining = [m for m in range(nch) if m != primary_index]
                    # Try the strongest raw-RMS alternatives first. Expected PCI
                    # validation remains active, so fallback does not accept an
                    # unrelated cell merely because it is stronger.
                    remaining.sort(key=lambda m: float(raw_levels[m]), reverse=True)
                    sync_order.extend(remaining)

                result = None
                reason = "no synchronization attempt"
                sync_source_index: Optional[int] = None
                attempt_reasons = []
                for candidate_index in sync_order:
                    candidate_iq = core.prepare_iq(
                        raw[candidate_index],
                        self.receiver.actual_rates[candidate_index],
                        args.detector_rate,
                        normalize_rms=True,
                    )
                    try:
                        candidate_result, candidate_reason = step2.process_one_burst(
                            step1, sync, candidate_iq, args
                        )
                    except Exception as exc:
                        candidate_result = None
                        candidate_reason = f"sync exception: {exc}"

                    if candidate_result is not None and candidate_result.accepted:
                        result = candidate_result
                        reason = candidate_reason
                        sync_source_index = int(candidate_index)
                        break
                    attempt_reasons.append(f"{labels[candidate_index]}: {candidate_reason}")

                if result is None or not result.accepted:
                    reason = " | ".join(attempt_reasons) or reason
                    rejected += 1
                    decision = lock_continuity.rejected()
                    if decision.reset_previous_channel:
                        # Do not compare a newly reacquired channel against a stale
                        # pre-dropout channel. That comparison can create a false
                        # motion spike after a long synchronization gap.
                        previous_sensing = None
                        previous_spatial = None
                    if not args.quiet_search and rejected % max(1, args.search_print_every) == 0:
                        print(
                            f"[SEARCH] rejected={rejected} "
                            f"consecutive={decision.consecutive_rejects} "
                            f"health={decision.lock_health} reason={reason}"
                        )
                    age_text = (
                        "never locked"
                        if decision.seconds_since_lock is None
                        else f"last_lock={decision.seconds_since_lock:.2f}s ago"
                    )
                    self.latest.update(
                        rejected=rejected,
                        status=decision.status,
                        startup_stage=decision.startup_stage,
                        lock_health=decision.lock_health,
                        consecutive_rejects=decision.consecutive_rejects,
                        lock_duty_cycle_pct=lock_continuity.duty_cycle_pct,
                        seconds_since_lock=decision.seconds_since_lock,
                        last_rejection_reason=str(reason),
                        sync_fallback_uses=sync_fallback_uses,
                        detail=(
                            f"windows={windows} locks={locks} rejected={rejected} "
                            f"{age_text}; reason={reason}"
                        ),
                    )
                    rejected_snapshot = self.latest.get()
                    self._record_dataset_window(
                        labels=labels,
                        windows=windows,
                        locks=locks,
                        rejected=rejected,
                        generation=generation,
                        sample_start=int(sample_start),
                        sample_end=int(sample_end),
                        raw=raw,
                        raw_levels=raw_levels,
                        snapshot=rejected_snapshot,
                        result=result,
                        accepted=False,
                        rejection_reason=str(reason),
                    )
                    continue

                if sync_source_index is None:
                    raise RuntimeError("Accepted synchronization result has no source channel")
                if sync_source_index != primary_index:
                    sync_fallback_uses += 1

                locks += 1
                sensing = []
                spatial = []
                similarities = []

                for m in range(nch):
                    iq = core.prepare_iq(
                        raw[m],
                        self.receiver.actual_rates[m],
                        args.detector_rate,
                        normalize_rms=False,
                    )

                    if m == int(sync_source_index):
                        grid = result.grid
                    else:
                        grid = core.extract_secondary_grid(
                            step2,
                            sync,
                            iq,
                            result,
                            args.detector_rate,
                            args.scs,
                        )

                    estimate = core.estimate_channel_from_grid(
                        sync,
                        grid,
                        result.nid1,
                        result.nid2,
                    )
                    sensing.append(estimate.sensing_h)
                    spatial.append(estimate.spatial_h)
                    similarities.append(estimate.pss_sss_similarity)

                sensing = np.asarray(sensing, dtype=np.complex64)
                spatial = np.asarray(spatial, dtype=np.complex64)

                provisional = core.provisional_activity(sensing, previous_sensing)

                # Capture the IQ gate state before updating the detector. The
                # final stillness-calibration window establishes thresholds and
                # must not itself be saved as a training IQ sample. Recording
                # therefore begins with the next accepted synchronized window.
                record_iq_this_window = bool(
                    self.dataset_recorder is not None
                    and self.dataset_recorder.iq_recording_enabled
                )

                if not calibration.ready:
                    calibration.add(sensing, spatial)
                    activity = provisional
                    status = f"BASELINE {calibration.count}/{calibration.target}"
                else:
                    activity = core.compute_array_activity(
                        current_sensing=sensing,
                        current_spatial=spatial,
                        baseline=calibration,
                        previous_sensing=previous_sensing,
                        previous_spatial=previous_spatial,
                        positions_m=positions_m,
                        reverse_labels=bool(args.reverse_direction_labels),
                        direction_threshold_rad=float(args.direction_threshold_rad),
                        spatial_weight=float(args.spatial_weight),
                    )
                    status = detector.update(activity.fused_score_db)

                # Enable the recorder once both calibration stages are ready.
                # For continuous mode this opens the gate for the next receiver
                # packet; for processed-window mode the next accepted window is
                # the first one written to the IQ CSV.
                if (
                    self.dataset_recorder is not None
                    and calibration.ready
                    and detector.ready
                    and self.dataset_recorder.enable_iq_recording(
                        start_window_id=windows + 1,
                        timestamp_utc=now_utc(),
                    )
                ):
                    print(
                        "[DATASET] Stillness calibration complete. "
                        "Post-calibration IQ recording is now active."
                    )

                lock_continuity.accepted(status)

                # Apply display policy without changing detector calculations.
                if args.waterfall_display == "triggered" and detector.ready and status == "STILL":
                    waterfall_row = np.zeros(127, dtype=np.float32)
                else:
                    waterfall_row = np.where(
                        np.abs(activity.event_row) >= float(args.event_mask_db),
                        activity.event_row,
                        0.0,
                    ).astype(np.float32)

                snapshot = MonitorSnapshot(
                    raw_sequence=windows,
                    sync_sequence=locks,
                    startup_stage="SYNCHRONIZED ACTIVITY LIVE",
                    status=status,
                    detail=(
                        f"windows={windows} locks={locks} rejected={rejected} "
                        f"similarity={np.mean(similarities):.3f}"
                    ),
                    labels=labels,
                    windows=windows,
                    locks=locks,
                    rejected=rejected,
                    calibration_count=calibration.count,
                    calibration_target=calibration.target,
                    stillness_count=detector.count,
                    stillness_target=detector.calibration_locks,
                    raw_rms_db=raw_levels,
                    amp_profiles=activity.amp_profiles,
                    phase_profiles=activity.phase_profiles,
                    event_row=activity.event_row,
                    antenna_scores_db=activity.antenna_scores_db,
                    spatial_scores_db=activity.spatial_scores_db,
                    fused_score_db=activity.fused_score_db,
                    direction_value_rad=activity.direction_value_rad,
                    direction=activity.direction,
                    pci=result.pci,
                    cfo_hz=result.cfo_hz,
                    pss_corr=result.pss_corr,
                    sss_corr=result.sss_corr,
                    similarity=float(np.mean(similarities)),
                    threshold_on_db=detector.on_threshold_db,
                    threshold_off_db=detector.off_threshold_db,
                    lock_health="LOCKED",
                    consecutive_rejects=0,
                    lock_duty_cycle_pct=lock_continuity.duty_cycle_pct,
                    seconds_since_lock=0.0,
                    last_rejection_reason="",
                    sync_source_index=int(sync_source_index),
                    sync_source_label=labels[int(sync_source_index)],
                    sync_fallback_uses=sync_fallback_uses,
                    waterfall_row=waterfall_row,
                )
                self.latest.update(**snapshot.__dict__)

                if locks % max(1, int(args.print_every)) == 0:
                    score_text = " ".join(
                        f"{labels[m]}={activity.antenna_scores_db[m]:.3f}dB"
                        for m in range(nch)
                    )
                    print(
                        "[ARRAY] "
                        f"lock={locks} status={status} "
                        f"{score_text} "
                        f"fused={activity.fused_score_db:.3f}dB "
                        f"direction=\"{activity.direction}\" "
                        f"trend={activity.direction_value_rad:.3f}rad "
                        f"CFO={result.cfo_hz:.1f}Hz"
                    )

                self._write_csv(snapshot)
                self._record_dataset_window(
                    labels=labels,
                    windows=windows,
                    locks=locks,
                    rejected=rejected,
                    generation=generation,
                    sample_start=int(sample_start),
                    sample_end=int(sample_end),
                    raw=raw,
                    raw_levels=raw_levels,
                    snapshot=snapshot,
                    result=result,
                    accepted=True,
                    rejection_reason="",
                    sensing=sensing,
                    spatial=spatial,
                    similarities=similarities,
                    activity=activity,
                    include_iq=record_iq_this_window,
                )

                previous_sensing = sensing.copy()
                previous_spatial = spatial.copy()

        except Exception as exc:
            self.latest.update(
                startup_stage="ERROR",
                status="ERROR",
                detail=f"{type(exc).__name__}: {exc}",
            )
            print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)

        finally:
            if self.receiver is not None:
                try:
                    self.receiver.stop()
                except Exception:
                    pass

            if self.csv_file is not None:
                try:
                    self.csv_file.flush()
                    self.csv_file.close()
                except Exception:
                    pass

            if self.dataset_recorder is not None:
                try:
                    self.dataset_recorder.close(timeout_s=60.0)
                    stats = self.dataset_recorder.stats
                    print(
                        "[DATASET] Closed: "
                        f"windows={stats.windows}, iq_rows={stats.iq_rows}, "
                        f"dropped_jobs={stats.dropped_jobs}, "
                        f"writer_errors={stats.writer_errors}"
                    )
                except Exception as exc:
                    print(f"[WARN] Dataset recorder close failed: {exc}", file=sys.stderr)

            self.latest.update(startup_stage="STOPPED", status="STOPPED")
            print("[INFO] Low-latency array monitor stopped.")


# ---------------------------------------------------------------------------
# PyQtGraph dashboard
# ---------------------------------------------------------------------------

class CircularWaterfallBuffer:
    """Mirrored circular image history with O(new_rows) insertion.

    The second half mirrors the first, so ``view()`` is always a contiguous
    chronological matrix without shifting the full history on every update.
    """

    def __init__(self, history: int, width: int = 127):
        self.history = max(2, int(history))
        self.width = int(width)
        self._storage = np.zeros((2 * self.history, self.width), dtype=np.float32)
        self._write = 0

    def append(self, rows: np.ndarray) -> None:
        values = np.asarray(rows, dtype=np.float32)
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.size == 0:
            return
        if values.shape[1] != self.width:
            raise ValueError(f"waterfall width must be {self.width}, got {values.shape[1]}")
        if values.shape[0] > self.history:
            values = values[-self.history:]
        idx = (self._write + np.arange(values.shape[0])) % self.history
        self._storage[idx] = values
        self._storage[idx + self.history] = values
        self._write = int((self._write + values.shape[0]) % self.history)

    def view(self) -> np.ndarray:
        return self._storage[self._write:self._write + self.history]


class PyQtGraphDashboard:
    """Render the live branded PyQtGraph monitor in a full-screen window."""

    def __init__(self, args, latest: LatestState, labels: list[str], core: ModuleType):
        """Initialize the PyQtGraphDashboard instance and its runtime state."""
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
            import pyqtgraph as pg
        except Exception as exc:
            raise RuntimeError(
                "PyQtGraph GUI dependencies are missing. Install with:\n"
                "  python3 -m pip install --user pyqtgraph PyQt5\n"
                "or run with --no-gui."
            ) from exc

        self.args = args
        self.latest = latest
        self.labels = labels
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.pg = pg
        self.core = core

        pg.setConfigOptions(antialias=False)

        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        operator_suffix = (
            f" — {args.selected_operator_name} ({args.selected_plmn})"
            if args.selected_operator_name and args.selected_plmn
            else (f" — {args.selected_operator_name}" if args.selected_operator_name else "")
        )
        # A QMainWindow wrapper lets the two dashboards share the same branded
        # white header while the PyQtGraph canvas keeps its efficient renderer.
        self.window = QtWidgets.QMainWindow()
        # Force a normal desktop title bar with minimize, maximize/restore, and close controls.
        self.window.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowTitleHint
            | QtCore.Qt.WindowSystemMenuHint
            | QtCore.Qt.WindowMinimizeButtonHint
            | QtCore.Qt.WindowMaximizeButtonHint
            | QtCore.Qt.WindowCloseButtonHint
        )
        self.window.setWindowTitle(
            "CSICS - Passive ISAC" + operator_suffix
        )
        self.window.resize(1700, 1050)

        central = QtWidgets.QWidget()
        outer_layout = QtWidgets.QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        outer_layout.addWidget(
            core.create_brand_header(
                QtCore,
                QtGui,
                QtWidgets,
                title_html=(
                    "<b>CSICS - Passive ISAC</b>"
                    + operator_suffix
                ),
                left_logo_path=args.left_logo,
                right_logo_path=args.right_logo,
            )
        )

        self.graphics = pg.GraphicsLayoutWidget()
        outer_layout.addWidget(self.graphics, 1)
        self.window.setCentralWidget(central)

        # Keep the original plotting canvas unchanged and place the requested
        # attribution in the main window's narrow bottom status bar.
        footer = QtWidgets.QStatusBar(self.window)
        footer.setSizeGripEnabled(False)
        footer.setFixedHeight(24)
        footer.setStyleSheet(
            "QStatusBar { background: #000000; color: #b8c0c8; "
            "border-top: 1px solid #2b333b; }"
            "QLabel { color: #b8c0c8; font-size: 10px; padding: 0 8px; }"
        )
        footer_left = QtWidgets.QLabel("(Bouk)")
        footer_right = QtWidgets.QLabel(
            "© 2026 CSICS Team. All rights reserved."
        )
        footer.addWidget(footer_left, 1)
        footer.addPermanentWidget(footer_right)
        self.window.setStatusBar(footer)

        self.status = pg.LabelItem(justify="left")
        self.graphics.addItem(self.status, row=0, col=0, colspan=2)

        self.amp_plot = self.graphics.addPlot(row=1, col=0, title="Per-element amplitude activity")
        self.phase_plot = self.graphics.addPlot(row=1, col=1, title="Per-element detrended phase activity")
        self.wf_plot = self.graphics.addPlot(row=2, col=0, colspan=2, title="Residual waterfall (expanded)")
        self.score_plot = self.graphics.addPlot(row=3, col=0, title="Per-element and fused residual scores")
        self.direction_plot = self.graphics.addPlot(row=3, col=1, title="Relative array direction trend")

        # Complete boxed graphs with top and right axis lines.
        plot_border_pen = pg.mkPen("w", width=1)

        for plot in [
            self.amp_plot,
            self.phase_plot,
            self.wf_plot,
            self.score_plot,
            self.direction_plot,
        ]:
            # Full rectangle around the plotting area.
            plot.getViewBox().setBorder(plot_border_pen)

            # Display upper and right axis lines.
            plot.showAxis("top")
            plot.showAxis("right")

            # Hide repeated tick values on the upper and right sides.
            plot.getAxis("top").setStyle(showValues=False)
            plot.getAxis("right").setStyle(showValues=False)
    
        self.amp_plot.setLabel("bottom", "Reference-signal subcarrier")
        self.amp_plot.setLabel("left", "Amplitude", units="dB")
        self.phase_plot.setLabel("bottom", "Reference-signal subcarrier")
        self.phase_plot.setLabel("left", "Phase", units="rad")
        self.wf_plot.setLabel("bottom", "Reference-signal subcarrier")
        self.wf_plot.setLabel("left", "Time (sync windows)")
        self.score_plot.setLabel("bottom", "Recent synchronized lock")
        self.score_plot.setLabel("left", "Residual score", units="dB")
        self.direction_plot.setLabel("bottom", "Recent synchronized lock")
        self.direction_plot.setLabel("left", "Spatial trend", units="rad")

        self.amp_plot.setXRange(0, 126)
        self.amp_plot.setYRange(-args.amp_ylim_db, args.amp_ylim_db)
        self.phase_plot.setXRange(0, 126)
        self.phase_plot.setYRange(-1.0, 1.0)
        self.score_plot.setXRange(0, args.history - 1)
        self.score_plot.setYRange(0, args.score_ylim_db)
        self.direction_plot.setXRange(0, args.history - 1)
        self.direction_plot.setYRange(-args.direction_ylim_rad, args.direction_ylim_rad)

        self.amp_plot.showGrid(x=True, y=True, alpha=0.2)
        self.phase_plot.showGrid(x=True, y=True, alpha=0.2)
        self.wf_plot.showGrid(x=True, y=True, alpha=0.2)
        self.score_plot.showGrid(x=True, y=True, alpha=0.2)
        self.direction_plot.showGrid(x=True, y=True, alpha=0.2)

        # Create legends before plotting curves so named items are registered.
        self.amp_plot.addLegend(offset=(10, 10))
        self.phase_plot.addLegend(offset=(10, 10))
        self.score_plot.addLegend(offset=(10, 10))
        self.direction_plot.addLegend(offset=(10, 10))

        self.colors = [pg.intColor(i, hues=max(4, len(labels) + 1)) for i in range(len(labels) + 1)]
       
        self.amp_curves = [
            self.amp_plot.plot(pen=pg.mkPen(self.colors[i], width=1.5), name=label)
            for i, label in enumerate(labels)
        ]
        self.phase_curves = [
            self.phase_plot.plot(pen=pg.mkPen(self.colors[i], width=1.5), name=label)
            for i, label in enumerate(labels)
        ]
        self.score_curves = [
            self.score_plot.plot(pen=pg.mkPen(self.colors[i], width=1.5), name=label)
            for i, label in enumerate(labels)
        ]
        self.score_curves.append(
            self.score_plot.plot(
                pen=pg.mkPen(self.colors[-1], width=2.5),
                name="fused",
            )
        )
        self.direction_curve = self.direction_plot.plot(
            pen=pg.mkPen(self.colors[-1], width=2.0),
            name="relative direction trend",
        )

        self.waterfall_buffer = CircularWaterfallBuffer(args.history, 127)
        self.wf_image = pg.ImageItem(axisOrder="row-major")
        self.wf_plot.addItem(self.wf_image)

        # Permanently enable a colored waterfall using the turbo colormap. viridis,plasma, inferno, magma
        cmap = pg.colormap.get("viridis")
        self.wf_image.setLookupTable(
            cmap.getLookupTable(
                start=0.0,
                stop=1.0,
                nPts=256,
            )
        )
        
        # PyQtGraph ImageItem.setRect() scales the current image. Initialize
        # the image data first so width() and height() are defined.
        self.wf_image.setImage(
            self.waterfall_buffer.view(),
            autoLevels=False,
            levels=(-float(args.wf_max_db), float(args.wf_max_db)),
        )
        self.wf_image.setRect(0, 0, 127, args.history)

        # Histories are preallocated and updated in place.
        self.score_history = np.zeros((len(labels) + 1, args.history), dtype=np.float32)
        self.direction_history = np.zeros(args.history, dtype=np.float32)

        self.last_raw_sequence = -1
        self.last_sync_sequence = -1
        self.last_status_text = ""
        self.waterfall_rows_dropped = 0
        self.last_waterfall_render = 0.0
        self.last_waterfall_level_update = 0.0
        self.waterfall_limit = float(args.wf_max_db)
        self.waterfall_dirty = False

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(max(1, int(round(1000.0 / float(args.gui_fps)))))

        if args.fullscreen:
            # Maximized retains the native title bar; true full-screen mode hides it.
            self.window.showMaximized()
        else:
            self.window.show()

    def _roll_append(self, matrix: np.ndarray, row: np.ndarray) -> None:
        """Internal helper for roll append."""
        matrix[:, :-1] = matrix[:, 1:]
        matrix[:, -1] = row

    def refresh(self) -> None:
        """Refresh the displayed state from the latest thread-safe snapshot."""
        s = self.latest.get()

        # Synchronized plots update only when a newer lock snapshot exists.
        if s.sync_sequence != self.last_sync_sequence and s.sync_sequence > 0:
            if s.amp_profiles is not None:
                for i, curve in enumerate(self.amp_curves):
                    curve.setData(s.amp_profiles[i])

            if s.phase_profiles is not None:
                for i, curve in enumerate(self.phase_curves):
                    curve.setData(s.phase_profiles[i])

            if s.antenna_scores_db is not None:
                values = list(s.antenna_scores_db) + [float(s.fused_score_db)]
                self.score_history[:, :-1] = self.score_history[:, 1:]
                self.score_history[:, -1] = np.asarray(values, dtype=np.float32)
                for i, curve in enumerate(self.score_curves):
                    curve.setData(self.score_history[i])

            self.direction_history[:-1] = self.direction_history[1:]
            self.direction_history[-1] = float(s.direction_value_rad)
            self.direction_curve.setData(self.direction_history)

            self.last_sync_sequence = s.sync_sequence

        # Drain every sensed waterfall row, independently of the latest-state
        # snapshot. This prevents GUI refresh rate from erasing intermediate
        # sensing windows. The circular image buffer avoids full-history shifts.
        rows, dropped = self.latest.drain_waterfall_rows()
        if dropped:
            self.waterfall_rows_dropped += int(dropped)
        if rows.shape[0]:
            self.waterfall_buffer.append(rows)
            self.waterfall_dirty = True

        now = time.monotonic()
        render_period = 1.0 / max(1.0, float(self.args.waterfall_fps))
        if self.waterfall_dirty and now - self.last_waterfall_render >= render_period:
            matrix = self.waterfall_buffer.view()
            if (
                self.args.adaptive_waterfall
                and now - self.last_waterfall_level_update
                >= max(0.05, float(self.args.waterfall_level_period_s))
            ):
                active = np.abs(matrix[np.nonzero(matrix)])
                if active.size:
                    estimate = float(np.percentile(active, 98.0))
                    self.waterfall_limit = max(
                        float(self.args.wf_min_db),
                        min(float(self.args.wf_max_db), estimate),
                    )
                self.last_waterfall_level_update = now
            elif not self.args.adaptive_waterfall:
                self.waterfall_limit = float(self.args.wf_max_db)

            self.wf_image.setImage(
                matrix,
                autoLevels=False,
                levels=(-self.waterfall_limit, self.waterfall_limit),
            )
            self.last_waterfall_render = now
            self.waterfall_dirty = False

        if s.threshold_on_db is None:
            threshold_text = "threshold=pending"
        else:
            threshold_text = (
                f"threshold_on={s.threshold_on_db:.3f}dB "
                f"threshold_off={s.threshold_off_db:.3f}dB"
            )

        identity_text = (
            f"PLMN={self.args.selected_plmn or 'Unknown'} | "
            f"operator={self.args.selected_operator_name or 'Unknown'} | "
        )
        lock_age_text = (
            "-" if s.seconds_since_lock is None else f"{s.seconds_since_lock:.2f}s"
        )
        status_text = (
            f"<b>{s.status}</b> | {s.startup_stage} | "
            f"{identity_text}"
            f"windows={s.windows} locks={s.locks} rejected={s.rejected} | "
            f"PCI={s.pci if s.pci is not None else '-'} | "
            f"CFO={s.cfo_hz:.1f}Hz | fused={s.fused_score_db:.3f}dB | "
            f"{s.direction} | {threshold_text}<br/>"
            f"sync={s.lock_health} | duty={s.lock_duty_cycle_pct:.1f}% | "
            f"sync source={s.sync_source_label or '-'} | "
            f"fallback uses={s.sync_fallback_uses} | "
            f"consecutive misses={s.consecutive_rejects} | "
            f"waterfall dropped={self.waterfall_rows_dropped} | "
            f"last valid lock={lock_age_text}<br/>"
            f"{s.detail}"
        )
        if status_text != self.last_status_text:
            self.status.setText(status_text)
            self.last_status_text = status_text

    def exec(self) -> int:
        """Handle the exec operation for this OCUDU processing stage."""
        return int(self.app.exec_())

    def close(self) -> None:
        """Close the component and release GUI or worker resources."""
        self.timer.stop()
        self.window.close()


# ---------------------------------------------------------------------------
# CLI and entrypoint
# ---------------------------------------------------------------------------

def build_parser(core: ModuleType) -> argparse.ArgumentParser:
    """Build and return parser."""
    ap = argparse.ArgumentParser(
        description="OCUDU optimized low-latency PyQtGraph flexible array monitor"
    )


    ap.add_argument("--device-args", default=DEFAULT_DEVICE_ARGS,
                    help="UHD device args; default address works with either X410 or N310 when no type is forced.")
    ap.add_argument("--channels", type=core.parse_csv_ints, default=[0, 1])
    ap.add_argument(
        "--element-labels",
        type=core.parse_csv_strings,
        default=None,
        help=(
            "Optional comma-separated physical array labels matching --channels, "
            "for example Top0,Top1,Top2,Top3. Defaults to A0:ch0 style labels."
        ),
    )
    ap.add_argument("--antennas", type=core.parse_csv_strings, default=["auto"],
                    help="Comma-separated RX antenna names, or auto to keep the device default.")
    ap.add_argument("--gains", type=core.parse_csv_floats, default=[30.0])
    ap.add_argument("--primary-index", type=int, default=0)
    ap.add_argument(
        "--sync-fallback-channels",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "If the primary antenna misses the selected PCI, try the remaining "
            "USRP channels in descending raw-RMS order."
        ),
    )
    ap.add_argument("--spacing-m", type=float, default=0.0433)

    ap.add_argument("--dl-arfcn", type=int, default=632628)
    ap.add_argument(
        "--dl-frequency-hz",
        type=float,
        default=None,
        help="Optional known carrier-center frequency; discovery mode normally leaves this unknown.",
    )
    ap.add_argument("--ssb-arfcn", type=int, default=630912)
    ap.add_argument(
        "--ssb-frequency-hz",
        type=float,
        default=None,
        help="Exact SSB center frequency selected by the raster scanner; overrides --ssb-arfcn.",
    )
    ap.add_argument(
        "--selected-gscn",
        type=int,
        default=None,
        help="Optional GSCN metadata shown for scanner-to-monitor handoff.",
    )
    ap.add_argument("--selected-mcc", default="")
    ap.add_argument("--selected-mnc", default="")
    ap.add_argument("--selected-plmn", default="")
    ap.add_argument("--selected-operator-name", default="")
    ap.add_argument("--selected-operator-source", default="")
    ap.add_argument("--pci", type=int, default=1)
    ap.add_argument("--rate", type=float, default=7.68e6)
    ap.add_argument("--detector-rate", type=float, default=7.68e6)
    ap.add_argument("--scs", type=float, default=30e3)
    ap.add_argument("--burst-ms", type=float, default=40.0)
    ap.add_argument("--settle-time", type=float, default=0.5)
    ap.add_argument("--ring-windows", type=int, default=3)
    ap.add_argument("--snapshot-timeout-s", type=float, default=1.0)
    ap.add_argument("--stream-start-delay-s", type=float, default=0.25)
    ap.add_argument(
        "--rx-timeout-s",
        type=float,
        default=1.0,
        help="UHD receive-call timeout. Longer values reduce harmless timeout churn.",
    )
    ap.add_argument(
        "--rx-timeout-warning-interval-s",
        type=float,
        default=5.0,
        help="Minimum interval between summarized RX timeout warnings.",
    )

    ap.add_argument(
        "--lock-grace-windows",
        type=int,
        default=6,
        help=(
            "Keep the last valid STILL/MOVEMENT state across this many "
            "consecutive rejected synchronization windows."
        ),
    )
    ap.add_argument(
        "--lock-grace-seconds",
        type=float,
        default=2.0,
        help="Maximum time to hold the last valid detector state after lock dropout.",
    )
    ap.add_argument(
        "--lock-reference-reset-windows",
        type=int,
        default=15,
        help=(
            "Reset the temporal channel reference after this many consecutive "
            "misses to prevent a false motion spike on reacquisition."
        ),
    )
    ap.add_argument(
        "--lock-reference-reset-seconds",
        type=float,
        default=4.0,
        help="Reset the temporal channel reference after this lock-loss duration.",
    )
    ap.add_argument(
        "--lock-health-window",
        type=int,
        default=50,
        help="Recent RX-window count used to calculate synchronization duty cycle.",
    )
    ap.add_argument(
        "--tracking-max-pss-candidates",
        type=int,
        default=8,
        help=(
            "Maximum ranked PSS candidates evaluated for the selected PCI in "
            "each antenna/window."
        ),
    )
    ap.add_argument(
        "--tracking-pss-cluster-radius",
        type=int,
        default=64,
        help="Time-sample radius used to merge duplicate PSS matched-filter peaks.",
    )

    sync_defaults = [
        ("threshold-db", float, -14.0),
        ("peak-window", int, 11),
        ("minimum-threshold", float, 1e-8),
        ("guard-samples", int, 512),
        ("min-pss-peak-over-median", float, 100.0),
        ("pss-timing-radius", int, 16),
        ("ssb-bin-radius", int, 4),
        ("cp-timing-radius", int, 8),
        ("sss-timing-radius", int, 32),
        ("sss-bin-radius", int, 4),
        ("min-fine-pss-corr", float, 0.50),
        ("min-sss-corr", float, 0.45),
        ("min-sss-peak-ratio", float, 1.25),
        ("min-cp-metric", float, 0.10),
    ]
    for name, value_type, default in sync_defaults:
        ap.add_argument("--" + name, type=value_type, default=default)

    ap.add_argument("--baseline-locks", type=int, default=100)
    ap.add_argument("--event-calibration-locks", type=int, default=150)
    ap.add_argument("--event-threshold-sigma", type=float, default=4.0)
    ap.add_argument("--event-min-threshold-db", type=float, default=0.05)
    ap.add_argument("--event-off-ratio", type=float, default=0.6)
    ap.add_argument("--event-mask-db", type=float, default=0.02)
    ap.add_argument("--waterfall-display", choices=["all", "triggered"], default="all")
    ap.add_argument("--spatial-weight", type=float, default=1.5)
    ap.add_argument("--direction-threshold-rad", type=float, default=0.08)
    ap.add_argument("--reverse-direction-labels", action="store_true")

    ap.add_argument("--history", type=int, default=180)
    ap.add_argument("--raw-history", type=int, default=160)
    ap.add_argument("--gui-fps", type=float, default=20.0)
    ap.add_argument("--print-every", type=int, default=50)
    ap.add_argument("--search-print-every", type=int, default=100)
    ap.add_argument("--csv-flush-every", type=int, default=20)
    ap.add_argument("--amp-ylim-db", type=float, default=8.0)
    ap.add_argument("--score-ylim-db", type=float, default=1.5)
    ap.add_argument("--wf-min-db", type=float, default=0.05)
    ap.add_argument("--wf-max-db", type=float, default=0.5)
    ap.add_argument("--adaptive-waterfall", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument(
        "--waterfall-fps", type=float, default=10.0,
        help="Maximum image-render rate. Sensing continues at full rate between renders.",
    )
    ap.add_argument(
        "--waterfall-level-period-s", type=float, default=0.5,
        help="Minimum interval between adaptive waterfall percentile recalculations.",
    )
    ap.add_argument(
        "--waterfall-queue-rows", type=int, default=512,
        help="Bounded number of sensed rows retained between GUI refreshes.",
    )
    ap.add_argument("--direction-ylim-rad", type=float, default=1.0)

    ap.add_argument("--quiet-search", action="store_true")
    ap.add_argument("--save-metrics", default=None)

    # AI/ML dataset recording.  These options do not alter any graph or plot.
    ap.add_argument(
        "--record-dataset",
        default=None,
        metavar="DIRECTORY",
        help=(
            "Write related AI/ML CSV tables to this directory. The dataset "
            "includes every processed window, rejected-window labels, per-channel "
            "features, and per-subcarrier complex channel features."
        ),
    )
    ap.add_argument(
        "--record-iq-csv",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Record raw I/Q values for every selected channel after baseline "
            "and stillness calibration complete. All IQ rows are appended to "
            "one session file: iq/iq_samples.csv."
        ),
    )
    ap.add_argument(
        "--iq-record-source",
        choices=["processed-windows", "continuous"],
        default="processed-windows",
        help=(
            "processed-windows records every sample in accepted analyzed RF "
            "windows after calibration; continuous records receiver packets "
            "only after the same calibration gate opens."
        ),
    )
    ap.add_argument(
        "--iq-csv-decimation",
        type=int,
        default=1,
        help="Write every Nth IQ sample. Use 1 to retain every sample.",
    )
    ap.add_argument(
        "--iq-csv-rows-per-file",
        type=int,
        default=500000,
        help=(
            "Deprecated compatibility option. IQ rotation is disabled and all "
            "rows are stored in one session-level CSV file."
        ),
    )
    ap.add_argument(
        "--iq-csv-gzip",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Compress rotated IQ files as .csv.gz; this reduces storage but increases CPU use.",
    )
    ap.add_argument(
        "--record-queue-size",
        type=int,
        default=8,
        help="Maximum queued dataset write jobs before the overflow policy is applied.",
    )
    ap.add_argument(
        "--record-overflow-policy",
        choices=["block", "drop"],
        default="block",
        help=(
            "block prioritizes dataset completeness but can slow RF capture; drop "
            "prioritizes live processing and reports every dropped recorder job."
        ),
    )
    ap.add_argument(
        "--dataset-label",
        default="",
        help="Ground-truth class stored with every row, such as empty_room, walking, or sitting.",
    )
    ap.add_argument(
        "--subject-id",
        default="",
        help="Optional anonymized participant or target identifier.",
    )
    ap.add_argument(
        "--scenario",
        default="",
        help="Optional experiment/scenario name stored with every feature row.",
    )
    ap.add_argument(
        "--session-notes",
        default="",
        help="Free-text notes stored once in session_metadata.csv.",
    )
    ap.add_argument(
        "--left-logo",
        default="assets/csics_odu_logo_white_bg.png",
        help="Project-relative or absolute path to the upper-left CSICS/ODU logo.",
    )
    ap.add_argument(
        "--right-logo",
        default="assets/odu_logo_white_bg.png",
        help="Project-relative or absolute path to the upper-right ODU logo.",
    )
    ap.add_argument(
        "--fullscreen",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Open the dashboard maximized while retaining title-bar controls; use --no-fullscreen for a normal window.",
    )
    ap.add_argument("--no-gui", action="store_true")
    ap.add_argument("--gui-smoke-test", action="store_true",
                    help="Open the PyQtGraph dashboard without UHD and close automatically.")
    ap.add_argument("--smoke-test-seconds", type=float, default=1.0,
                    help="Duration of --gui-smoke-test before automatic close.")
    ap.add_argument("--self-test", action="store_true")

    return ap


def normalize_selected_values(args, core: ModuleType) -> None:
    """Normalize selected values into the expected representation."""
    if float(args.waterfall_fps) <= 0:
        raise ValueError("--waterfall-fps must be positive")
    if float(args.waterfall_level_period_s) <= 0:
        raise ValueError("--waterfall-level-period-s must be positive")
    if int(args.waterfall_queue_rows) < 8:
        raise ValueError("--waterfall-queue-rows must be at least 8")
    nch = len(args.channels)
    if args.element_labels is None:
        args.element_labels = [
            f"A{array_index}:ch{channel}"
            for array_index, channel in enumerate(args.channels)
        ]
    elif len(args.element_labels) != nch:
        raise ValueError(
            f"--element-labels must contain exactly {nch} labels to match "
            f"--channels; received {args.element_labels}"
        )
    args.antennas = core.expand_values(args.antennas, nch, "--antennas")
    args.gains = [
        float(v)
        for v in core.expand_values(args.gains, nch, "--gains")
    ]

    if args.primary_index < 0 or args.primary_index >= nch:
        raise ValueError(
            f"--primary-index must be in range 0..{nch - 1} for "
            f"{nch} selected channels."
        )
    if float(args.burst_ms) <= 0.0:
        raise ValueError("--burst-ms must be positive")
    if int(args.lock_grace_windows) < 0:
        raise ValueError("--lock-grace-windows cannot be negative")
    if float(args.lock_grace_seconds) < 0.0:
        raise ValueError("--lock-grace-seconds cannot be negative")
    if int(args.lock_reference_reset_windows) <= int(args.lock_grace_windows):
        raise ValueError(
            "--lock-reference-reset-windows must be greater than --lock-grace-windows"
        )
    if float(args.lock_reference_reset_seconds) < float(args.lock_grace_seconds):
        raise ValueError(
            "--lock-reference-reset-seconds must be at least --lock-grace-seconds"
        )
    if int(args.lock_health_window) < 1:
        raise ValueError("--lock-health-window must be at least 1")
    if int(args.tracking_max_pss_candidates) < 1:
        raise ValueError("--tracking-max-pss-candidates must be at least 1")
    if int(args.tracking_pss_cluster_radius) < 0:
        raise ValueError("--tracking-pss-cluster-radius cannot be negative")


def run_gui_smoke_test(args, labels: list[str], core: ModuleType) -> int:
    """Open and exercise the dashboard without UHD, then close automatically."""
    latest = LatestState(labels, waterfall_capacity=args.waterfall_queue_rows)
    dashboard = PyQtGraphDashboard(args, latest, labels, core)

    nch = len(labels)
    x = np.linspace(0.0, 1.0, 127, dtype=np.float32)
    raw = np.linspace(-42.0, -36.0, nch, dtype=np.float32)
    amp = np.stack(
        [0.4 * np.sin(2.0 * np.pi * (i + 1) * x) for i in range(nch)],
        axis=0,
    ).astype(np.float32)
    phase = np.stack(
        [0.1 * np.cos(2.0 * np.pi * (i + 1) * x) for i in range(nch)],
        axis=0,
    ).astype(np.float32)
    row = (0.15 * np.sin(2.0 * np.pi * 5.0 * x)).astype(np.float32)
    scores = np.linspace(0.08, 0.18, nch, dtype=np.float32)

    latest.update(
        raw_sequence=1,
        sync_sequence=1,
        startup_stage="GUI SMOKE TEST",
        status="SMOKE TEST ACTIVE",
        detail="Synthetic display data; UHD is not opened.",
        windows=1,
        locks=1,
        rejected=0,
        raw_rms_db=raw,
        amp_profiles=amp,
        phase_profiles=phase,
        antenna_scores_db=scores,
        spatial_scores_db=np.zeros(nch, dtype=np.float32),
        fused_score_db=float(np.median(scores)),
        direction_value_rad=0.12 if nch >= 2 else 0.0,
        direction="RIGHT TREND" if nch >= 2 else "DIRECTION REQUIRES >=2 ANTENNAS",
        pci=1,
        cfo_hz=1600.0,
        pss_corr=0.98,
        sss_corr=0.98,
        similarity=1.0,
        threshold_on_db=0.12,
        threshold_off_db=0.08,
        waterfall_row=row,
    )

    dashboard.refresh()

    def close_test():
        """Handle the close test operation for this OCUDU processing stage."""
        print("[GUI-SMOKE-TEST] PASS: dashboard initialized and waterfall rendered.")
        dashboard.close()
        dashboard.app.quit()

    dashboard.QtCore.QTimer.singleShot(
        max(50, int(float(args.smoke_test_seconds) * 1000.0)),
        close_test,
    )
    return dashboard.exec()


def run_terminal(args, latest: LatestState, worker: ProcessingWorker) -> int:
    """Run terminal and return its result."""
    print("[INFO] Terminal-only mode. Press Ctrl+C to stop.")
    last_sequence = -1

    try:
        while worker.is_alive():
            time.sleep(0.25)
            s = latest.get()
            if s.sequence == last_sequence:
                continue
            last_sequence = s.sequence

            if s.locks > 0:
                print(
                    "[LIVE] "
                    f"status={s.status} windows={s.windows} locks={s.locks} "
                    f"PCI={s.pci} fused={s.fused_score_db:.3f}dB "
                    f"direction=\"{s.direction}\" CFO={s.cfo_hz:.1f}Hz"
                )
            else:
                print(
                    "[LIVE] "
                    f"stage={s.startup_stage} status={s.status} "
                    f"windows={s.windows} locks={s.locks} rejected={s.rejected}"
                )

    except KeyboardInterrupt:
        pass
    finally:
        worker.request_stop()
        worker.join(timeout=3.0)

    return 0


def main() -> int:
    # Load processing core before parsing because CLI validators are reused.
    """Run the command-line entry point and return its process exit status."""
    core = default_core
    parser = build_parser(core)
    args = parser.parse_args()

    if args.self_test:
        return int(core.synthetic_self_test())

    normalize_selected_values(args, core)

    labels = list(args.element_labels)

    if args.gui_smoke_test:
        return run_gui_smoke_test(args, labels, core)

    latest = LatestState(labels, waterfall_capacity=args.waterfall_queue_rows)
    worker = ProcessingWorker(
        args, latest, core, default_step1, default_step2, default_sync
    )

    if args.no_gui:
        worker.start()
        return run_terminal(args, latest, worker)

    dashboard = PyQtGraphDashboard(args, latest, labels, core)

    def shutdown(*_):
        """Handle the shutdown operation for this OCUDU processing stage."""
        worker.request_stop()
        dashboard.close()
        dashboard.app.quit()

    signal.signal(signal.SIGINT, shutdown)
    dashboard.app.aboutToQuit.connect(worker.request_stop)

    worker.start()

    # Periodic no-op Python callback keeps Ctrl+C handling responsive inside Qt.
    heartbeat = dashboard.QtCore.QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)

    try:
        return dashboard.exec()
    finally:
        worker.request_stop()
        worker.join(timeout=3.0)
        dashboard.close()


if __name__ == "__main__":
    raise SystemExit(main())

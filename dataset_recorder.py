#!/usr/bin/env python3
"""Non-blocking dataset recorder for the CSICS Passive ISAC monitor.

The monitor records compact feature tables directly as CSV. Raw multi-channel IQ
is different: converting millions of complex samples into text while the X410 is
streaming can monopolize Python's GIL, fill the recorder queue, and starve the
UHD receive thread. That failure mode appears as a frozen dashboard followed by
repeated ``ERROR_CODE_TIMEOUT`` warnings.

To keep live capture responsive, post-calibration IQ is first appended to a fast
binary spool by the background writer. When capture stops normally, the spool is
converted into exactly one ``iq/iq_samples.csv`` (or ``.csv.gz``) file and then
removed. The final dataset format remains CSV, but expensive text conversion no
longer competes with real-time RF capture.

The recorder stores:

* one metadata row for every processed RF window, including rejected windows;
* one feature row per channel and processed window;
* one feature row per channel and reference-signal subcarrier after a valid lock;
* optional wide-format IQ rows containing aligned I/Q values for every channel.

Raw IQ recording is gated. The processing worker enables it only after both the
array baseline and stillness-threshold calibrations have completed. Overlapping
processed windows are de-duplicated by global sample index so each available IQ
sample is written at most once.
"""

from __future__ import annotations

import csv
import gzip
import math
import queue
import re
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Optional, TextIO

import numpy as np


def utc_now() -> str:
    """Return a timezone-aware ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    """Convert a channel label into a stable CSV-column identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_")
    return cleaned or "channel"


def _timestamp_to_ns(value: str) -> int:
    """Convert an ISO-8601 timestamp to integer nanoseconds since Unix epoch."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(round(parsed.timestamp() * 1_000_000_000))
    except Exception:
        return int(round(datetime.now(timezone.utc).timestamp() * 1_000_000_000))


def _ns_to_timestamp(value: int) -> str:
    """Convert integer Unix nanoseconds back to an ISO-8601 UTC timestamp."""
    return datetime.fromtimestamp(float(value) / 1_000_000_000.0, timezone.utc).isoformat()


@dataclass
class RecorderStats:
    """Runtime counters used to audit dataset completeness."""

    queued_jobs: int = 0
    written_jobs: int = 0
    dropped_jobs: int = 0
    iq_rows: int = 0
    windows: int = 0
    accepted_windows: int = 0
    rejected_windows: int = 0
    writer_errors: int = 0
    iq_jobs_skipped_before_calibration: int = 0
    duplicate_iq_rows_skipped: int = 0


class MLDatasetRecorder:
    """Write synchronized Passive-ISAC training data to related CSV tables.

    Feature CSVs are written by a background thread during capture. IQ is spooled
    as interleaved ``complex64`` blocks and converted to one CSV after streaming
    stops. Tables can be joined using ``session_id`` and ``window_id``.
    """

    WINDOW_HEADER = [
        "session_id",
        "window_id",
        "timestamp_utc",
        "capture_generation",
        "sample_start",
        "sample_end_exclusive",
        "samples_per_channel",
        "sample_rate_hz",
        "iq_record_source",
        "dataset_label",
        "subject_id",
        "scenario",
        "accepted",
        "status",
        "startup_stage",
        "lock_health",
        "windows_seen",
        "locks_seen",
        "rejected_seen",
        "expected_pci",
        "detected_pci",
        "nid1",
        "nid2",
        "cfo_hz",
        "pss_corr",
        "sss_corr",
        "sss_peak_ratio",
        "cp_metric",
        "coarse_time_ms",
        "coarse_peak_over_threshold",
        "coarse_peak_over_median",
        "pss_useful_start",
        "sss_useful_start",
        "sss_timing_delta",
        "ssb_bin_offset",
        "sync_source_index",
        "sync_source_label",
        "sync_fallback_uses",
        "similarity_mean",
        "fused_score_db",
        "direction",
        "direction_value_rad",
        "threshold_on_db",
        "threshold_off_db",
        "calibration_count",
        "calibration_target",
        "stillness_count",
        "stillness_target",
        "lock_duty_cycle_pct",
        "consecutive_rejects",
        "seconds_since_lock",
        "rejection_reason",
        "record_queue_dropped_jobs",
    ]

    CHANNEL_HEADER = [
        "session_id",
        "window_id",
        "timestamp_utc",
        "dataset_label",
        "subject_id",
        "scenario",
        "channel_array_index",
        "hardware_channel",
        "channel_label",
        "antenna",
        "gain_db",
        "center_frequency_hz",
        "sample_rate_hz",
        "is_sync_source",
        "raw_rms_db",
        "event_score_db",
        "spatial_score_db",
        "pss_sss_similarity",
        "sensing_magnitude_mean",
        "sensing_magnitude_std",
        "sensing_phase_mean_rad",
        "spatial_magnitude_mean",
        "spatial_magnitude_std",
        "spatial_phase_mean_rad",
    ]

    SUBCARRIER_HEADER = [
        "session_id",
        "window_id",
        "timestamp_utc",
        "dataset_label",
        "subject_id",
        "scenario",
        "channel_array_index",
        "hardware_channel",
        "channel_label",
        "subcarrier_index",
        "sensing_i",
        "sensing_q",
        "spatial_i",
        "spatial_q",
        "amplitude_profile_db",
        "phase_profile_rad",
        "spatial_phase_profile_rad",
        "fused_event_residual_db",
    ]

    # Binary block header fields:
    # magic, source code, window id, chunk id, sample start, sample step,
    # number of rows, timestamp in Unix nanoseconds.
    _IQ_MAGIC = b"IQ01"
    _IQ_HEADER = struct.Struct("<4sB3xqqqiiq")
    _SOURCE_TO_CODE = {"processed-window": 1, "continuous": 2}
    _CODE_TO_SOURCE = {1: "processed-window", 2: "continuous"}

    def __init__(
        self,
        *,
        output_dir: str | Path,
        labels: list[str],
        hardware_channels: list[int],
        antennas: list[str],
        gains_db: list[float],
        rates_hz: list[float],
        frequencies_hz: list[float],
        static_metadata: dict[str, Any],
        record_iq_csv: bool,
        iq_record_source: str,
        iq_decimation: int,
        iq_rows_per_file: int,
        iq_gzip: bool,
        queue_size: int,
        overflow_policy: str,
    ) -> None:
        """Create dataset tables, the IQ spool, and the background writer."""
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.iq_dir = self.output_dir / "iq"
        self.iq_dir.mkdir(parents=True, exist_ok=True)

        self.labels = list(labels)
        self.safe_labels = [_safe_name(label) for label in labels]
        self.hardware_channels = [int(v) for v in hardware_channels]
        self.antennas = list(antennas)
        self.gains_db = [float(v) for v in gains_db]
        self.rates_hz = [float(v) for v in rates_hz]
        self.frequencies_hz = [float(v) for v in frequencies_hz]
        self.nch = len(self.labels)
        self.record_iq_csv = bool(record_iq_csv)
        self.iq_record_source = str(iq_record_source)
        self.iq_decimation = max(1, int(iq_decimation))
        self.iq_rows_per_file = max(1, int(iq_rows_per_file))  # Compatibility only.
        self.iq_gzip = bool(iq_gzip)
        self.overflow_policy = str(overflow_policy)
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")

        self.stats = RecorderStats()
        self._stats_lock = threading.Lock()
        self._queue: queue.Queue[Optional[tuple[str, Any]]] = queue.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._stop_event = threading.Event()
        self._writer_error: Optional[str] = None

        self._windows_file = (self.output_dir / "windows.csv").open(
            "w", newline="", encoding="utf-8"
        )
        self._channel_file = (self.output_dir / "channel_features.csv").open(
            "w", newline="", encoding="utf-8"
        )
        self._subcarrier_file = (self.output_dir / "subcarrier_features.csv").open(
            "w", newline="", encoding="utf-8"
        )
        self._windows_writer = csv.DictWriter(
            self._windows_file, fieldnames=self.WINDOW_HEADER, extrasaction="ignore"
        )
        self._channel_writer = csv.DictWriter(
            self._channel_file, fieldnames=self.CHANNEL_HEADER, extrasaction="ignore"
        )
        self._subcarrier_writer = csv.DictWriter(
            self._subcarrier_file,
            fieldnames=self.SUBCARRIER_HEADER,
            extrasaction="ignore",
        )
        self._windows_writer.writeheader()
        self._channel_writer.writeheader()
        self._subcarrier_writer.writeheader()

        self._iq_enabled = False
        self._iq_enabled_lock = threading.Lock()
        self._iq_start_window_id: Optional[int] = None
        self._iq_start_timestamp_utc: Optional[str] = None
        self._last_iq_global_index: Optional[int] = None
        self._iq_spool_path = self.iq_dir / ".iq_samples_spool.bin"
        self._iq_spool_file: Optional[BinaryIO] = None
        self._iq_csv_finalized = False
        self._iq_header = [
            "session_id",
            "source",
            "window_id",
            "chunk_id",
            "timestamp_utc",
            "global_sample_index",
            "sample_offset_s",
        ]
        for name in self.safe_labels:
            self._iq_header.extend([f"{name}_i", f"{name}_q"])

        self._write_session_metadata(static_metadata)

        self._thread = threading.Thread(
            target=self._writer_loop,
            name="csics-ml-spool-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def writer_error(self) -> Optional[str]:
        """Return the first background writer or finalization error."""
        return self._writer_error

    def _write_session_metadata(self, metadata: dict[str, Any]) -> None:
        """Write static configuration as a simple key/value table."""
        path = self.output_dir / "session_metadata.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["session_id", "key", "value"])
            core = {
                "created_utc": utc_now(),
                "record_iq_csv": self.record_iq_csv,
                "iq_record_source": self.iq_record_source,
                "iq_decimation": self.iq_decimation,
                "iq_rows_per_file": self.iq_rows_per_file,
                "iq_file_mode": "single-session-file",
                "iq_runtime_write_mode": "binary-spool-then-csv-finalize",
                "iq_start_policy": "after-array-and-stillness-calibration",
                "iq_overlap_policy": "deduplicate-by-global-sample-index",
                "iq_gzip": self.iq_gzip,
                "overflow_policy": self.overflow_policy,
                "channel_labels": "|".join(self.labels),
                "hardware_channels": "|".join(map(str, self.hardware_channels)),
                "antennas": "|".join(self.antennas),
                "gains_db": "|".join(map(str, self.gains_db)),
                "rates_hz": "|".join(map(str, self.rates_hz)),
                "frequencies_hz": "|".join(map(str, self.frequencies_hz)),
            }
            core.update(metadata)
            for key, value in core.items():
                writer.writerow([self.session_id, key, "" if value is None else value])

    def _append_final_metadata(self) -> None:
        """Append completion counters for later data-quality checks."""
        path = self.output_dir / "session_metadata.csv"
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            values = {
                "closed_utc": utc_now(),
                "queued_jobs": self.stats.queued_jobs,
                "written_jobs": self.stats.written_jobs,
                "dropped_jobs": self.stats.dropped_jobs,
                "iq_rows": self.stats.iq_rows,
                "duplicate_iq_rows_skipped": self.stats.duplicate_iq_rows_skipped,
                "windows": self.stats.windows,
                "accepted_windows": self.stats.accepted_windows,
                "rejected_windows": self.stats.rejected_windows,
                "writer_errors": self.stats.writer_errors,
                "writer_error_message": self._writer_error or "",
                "iq_jobs_skipped_before_calibration": (
                    self.stats.iq_jobs_skipped_before_calibration
                ),
                "iq_recording_start_window_id": (
                    "" if self._iq_start_window_id is None else self._iq_start_window_id
                ),
                "iq_recording_started_utc": self._iq_start_timestamp_utc or "",
                "iq_csv_finalized": self._iq_csv_finalized,
            }
            for key, value in values.items():
                writer.writerow([self.session_id, key, value])

    def _mark_writer_error(self, exc: Exception | str) -> None:
        """Store one recorder error and unblock producers waiting on the queue."""
        message = str(exc) if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        if self._writer_error is None:
            self._writer_error = message
        with self._stats_lock:
            self.stats.writer_errors += 1

    def _enqueue(self, kind: str, payload: Any) -> bool:
        """Queue one write without deadlocking if the writer has failed."""
        if self._stop_event.is_set() or self._writer_error is not None:
            return False

        try:
            if self.overflow_policy == "block":
                # Use short timed waits rather than an unbounded put(). If the
                # writer fails or shutdown begins, the processing thread can
                # escape instead of appearing permanently hung.
                while not self._stop_event.is_set() and self._writer_error is None:
                    try:
                        self._queue.put((kind, payload), timeout=0.25)
                        break
                    except queue.Full:
                        continue
                else:
                    return False
            else:
                self._queue.put_nowait((kind, payload))
        except queue.Full:
            with self._stats_lock:
                self.stats.dropped_jobs += 1
            return False

        with self._stats_lock:
            self.stats.queued_jobs += 1
        return True

    def enqueue_continuous_iq(
        self,
        samples: np.ndarray,
        *,
        sample_start: int,
        timestamp_utc: Optional[str] = None,
        chunk_id: int = 0,
    ) -> bool:
        """Queue one aligned receiver packet after the calibration gate opens."""
        if not self.record_iq_csv or self.iq_record_source != "continuous":
            return False
        if not self.iq_recording_enabled:
            with self._stats_lock:
                self.stats.iq_jobs_skipped_before_calibration += 1
            return False

        array = np.asarray(samples, dtype=np.complex64)
        if array.ndim != 2 or array.shape[0] != self.nch:
            raise ValueError("Continuous IQ must have shape [channels, samples].")
        payload = {
            "samples": array[:, :: self.iq_decimation].copy(),
            "sample_start": int(sample_start),
            "sample_step": int(self.iq_decimation),
            "timestamp_utc": timestamp_utc or utc_now(),
            "chunk_id": int(chunk_id),
            "window_id": -1,
            "source": "continuous",
        }
        return self._enqueue("iq", payload)

    def record_window(
        self,
        *,
        window_row: dict[str, Any],
        raw_iq: Optional[np.ndarray],
        channel_rows: list[dict[str, Any]],
        subcarrier_rows: list[dict[str, Any]],
        include_iq: bool = False,
    ) -> bool:
        """Queue one processed window and its related feature tables.

        ``include_iq`` is true only for accepted synchronized windows observed
        after stillness calibration completed. Earlier and rejected windows keep
        their metadata/features, but their raw IQ is excluded.
        """
        payload = {
            "window_row": dict(window_row),
            "channel_rows": [dict(row) for row in channel_rows],
            "subcarrier_rows": [dict(row) for row in subcarrier_rows],
            "raw_iq": None,
        }
        if (
            self.record_iq_csv
            and self.iq_record_source == "processed-windows"
            and bool(include_iq)
            and self.iq_recording_enabled
            and raw_iq is not None
        ):
            payload["raw_iq"] = np.asarray(raw_iq, dtype=np.complex64)[
                :, :: self.iq_decimation
            ]
        return self._enqueue("window", payload)

    @property
    def iq_recording_enabled(self) -> bool:
        """Return whether post-calibration IQ recording is enabled."""
        with self._iq_enabled_lock:
            return bool(self._iq_enabled)

    def enable_iq_recording(
        self,
        *,
        start_window_id: Optional[int] = None,
        timestamp_utc: Optional[str] = None,
    ) -> bool:
        """Open the IQ gate exactly once after all calibration windows."""
        if not self.record_iq_csv:
            return False
        with self._iq_enabled_lock:
            if self._iq_enabled:
                return False
            self._iq_enabled = True
            self._iq_start_window_id = (
                None if start_window_id is None else int(start_window_id)
            )
            self._iq_start_timestamp_utc = timestamp_utc or utc_now()
            return True

    def _open_iq_spool(self) -> None:
        """Open the temporary binary spool lazily on the first IQ block."""
        if self._iq_spool_file is None:
            self._iq_spool_file = self._iq_spool_path.open("wb", buffering=8 * 1024 * 1024)

    def _spool_iq_payload(self, payload: dict[str, Any]) -> None:
        """Append one IQ block to the fast binary spool.

        The block is de-duplicated against the last global sample index. This is
        especially important for sliding processed windows, which can otherwise
        write the same RF samples many times and overwhelm storage.
        """
        samples = np.asarray(payload["samples"], dtype=np.complex64)
        if samples.ndim != 2 or samples.shape[0] != self.nch:
            raise ValueError("IQ payload must have shape [channels, samples].")

        count = int(samples.shape[1])
        if count <= 0:
            return
        start = int(payload["sample_start"])
        step = max(1, int(payload.get("sample_step", 1)))

        skipped = 0
        if self._last_iq_global_index is not None and start <= self._last_iq_global_index:
            skipped = int((self._last_iq_global_index - start) // step + 1)
            skipped = min(skipped, count)
            if skipped >= count:
                with self._stats_lock:
                    self.stats.duplicate_iq_rows_skipped += count
                return
            samples = samples[:, skipped:]
            start += skipped * step
            count -= skipped

        source = str(payload.get("source", "processed-window"))
        source_code = int(self._SOURCE_TO_CODE.get(source, 1))
        window_id_value = payload.get("window_id", -1)
        chunk_id_value = payload.get("chunk_id", -1)
        window_id = -1 if window_id_value in (None, "") else int(window_id_value)
        chunk_id = -1 if chunk_id_value in (None, "") else int(chunk_id_value)
        timestamp_ns = _timestamp_to_ns(str(payload.get("timestamp_utc", utc_now())))

        self._open_iq_spool()
        assert self._iq_spool_file is not None
        self._iq_spool_file.write(
            self._IQ_HEADER.pack(
                self._IQ_MAGIC,
                source_code,
                window_id,
                chunk_id,
                start,
                step,
                count,
                timestamp_ns,
            )
        )

        # Store samples in row-major [sample, channel] order. The contiguous
        # memory write is implemented in C and does not execute a Python loop
        # for every complex value.
        interleaved = np.ascontiguousarray(samples.T, dtype=np.complex64)
        self._iq_spool_file.write(memoryview(interleaved).cast("B"))
        self._last_iq_global_index = start + (count - 1) * step

        with self._stats_lock:
            self.stats.iq_rows += count
            self.stats.duplicate_iq_rows_skipped += skipped

    def _write_window_payload(self, payload: dict[str, Any]) -> None:
        """Write one feature bundle and spool its optional raw IQ block."""
        row = dict(payload["window_row"])
        row["session_id"] = self.session_id
        row["record_queue_dropped_jobs"] = self.stats.dropped_jobs
        self._windows_writer.writerow(row)

        for channel_row in payload["channel_rows"]:
            channel_row["session_id"] = self.session_id
            self._channel_writer.writerow(channel_row)
        for subcarrier_row in payload["subcarrier_rows"]:
            subcarrier_row["session_id"] = self.session_id
            self._subcarrier_writer.writerow(subcarrier_row)

        raw_iq = payload.get("raw_iq")
        if raw_iq is not None:
            self._spool_iq_payload(
                {
                    "samples": raw_iq,
                    "sample_start": int(row["sample_start"]),
                    "sample_step": int(self.iq_decimation),
                    "timestamp_utc": row["timestamp_utc"],
                    "chunk_id": row["window_id"],
                    "window_id": row["window_id"],
                    "source": "processed-window",
                }
            )

        with self._stats_lock:
            self.stats.windows += 1
            if bool(row.get("accepted")):
                self.stats.accepted_windows += 1
            else:
                self.stats.rejected_windows += 1

    def _writer_loop(self) -> None:
        """Drain queued feature and IQ-spool jobs until the sentinel arrives."""
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is None:
                        return
                    kind, payload = item
                    if kind == "iq":
                        self._spool_iq_payload(payload)
                    elif kind == "window":
                        self._write_window_payload(payload)
                    else:
                        raise ValueError(f"Unknown recorder job type: {kind}")
                    with self._stats_lock:
                        self.stats.written_jobs += 1
                finally:
                    self._queue.task_done()
        except Exception as exc:
            self._mark_writer_error(exc)

    def _open_final_iq_text(self) -> tuple[TextIO, Path]:
        """Open the final single IQ CSV or CSV.GZ output file."""
        suffix = ".csv.gz" if self.iq_gzip else ".csv"
        path = self.iq_dir / f"iq_samples{suffix}"
        if self.iq_gzip:
            return gzip.open(path, "wt", newline="", encoding="utf-8"), path
        return path.open("w", newline="", encoding="utf-8", buffering=8 * 1024 * 1024), path

    def _finalize_iq_csv(self) -> None:
        """Convert the binary spool into one wide-format IQ CSV after capture."""
        if not self.record_iq_csv:
            return

        output_handle, output_path = self._open_final_iq_text()
        converted = 0
        try:
            writer = csv.writer(output_handle)
            writer.writerow(self._iq_header)

            if not self._iq_spool_path.exists():
                self._iq_csv_finalized = True
                return

            print(
                "[DATASET] RF capture stopped. Finalizing post-calibration IQ "
                f"to one CSV: {output_path}"
            )
            rate = float(self.rates_hz[0]) if self.rates_hz else 1.0
            header_size = self._IQ_HEADER.size

            with self._iq_spool_path.open("rb") as source_file:
                while True:
                    header = source_file.read(header_size)
                    if not header:
                        break
                    if len(header) != header_size:
                        raise IOError("Truncated IQ spool block header.")

                    (
                        magic,
                        source_code,
                        window_id,
                        chunk_id,
                        sample_start,
                        sample_step,
                        count,
                        timestamp_ns,
                    ) = self._IQ_HEADER.unpack(header)
                    if magic != self._IQ_MAGIC:
                        raise IOError("Invalid IQ spool block marker.")
                    if count < 0:
                        raise IOError("Invalid negative IQ spool row count.")

                    byte_count = int(count) * self.nch * np.dtype(np.complex64).itemsize
                    raw_bytes = source_file.read(byte_count)
                    if len(raw_bytes) != byte_count:
                        raise IOError("Truncated IQ spool sample block.")
                    values = np.frombuffer(raw_bytes, dtype=np.complex64).reshape(
                        int(count), self.nch
                    )

                    source_name = self._CODE_TO_SOURCE.get(int(source_code), "unknown")
                    timestamp = _ns_to_timestamp(int(timestamp_ns))
                    window_text: Any = "" if int(window_id) < 0 else int(window_id)
                    chunk_text: Any = "" if int(chunk_id) < 0 else int(chunk_id)

                    # Conversion happens only after UHD streaming has stopped,
                    # so a Python CSV loop can no longer starve the RX thread.
                    def rows():
                        for local_index, sample_values in enumerate(values):
                            global_index = int(sample_start) + local_index * int(sample_step)
                            row: list[Any] = [
                                self.session_id,
                                source_name,
                                window_text,
                                chunk_text,
                                timestamp,
                                global_index,
                                f"{global_index / rate:.12f}",
                            ]
                            for value in sample_values:
                                row.extend(
                                    [
                                        f"{float(value.real):.9g}",
                                        f"{float(value.imag):.9g}",
                                    ]
                                )
                            yield row

                    writer.writerows(rows())
                    converted += int(count)
                    if converted and converted % 1_000_000 < int(count):
                        print(f"[DATASET] Finalized {converted:,} IQ rows...")

            self._iq_csv_finalized = True
            print(f"[DATASET] IQ CSV complete: {converted:,} rows")
        finally:
            output_handle.flush()
            output_handle.close()

        if self._iq_csv_finalized:
            try:
                self._iq_spool_path.unlink(missing_ok=True)
            except Exception:
                pass

    def close(self, timeout_s: float = 30.0) -> None:
        """Drain live writes, stop the writer, and finalize the single IQ CSV."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()

        # Place the sentinel even if the queue is temporarily full. The writer
        # now performs only fast feature writes and binary spooling, so it should
        # drain quickly. A timeout still prevents shutdown from deadlocking.
        deadline_wait = max(0.1, float(timeout_s))
        try:
            self._queue.put(None, timeout=min(10.0, deadline_wait))
        except queue.Full:
            self._mark_writer_error("Recorder queue remained full during shutdown.")

        self._thread.join(timeout=deadline_wait)
        if self._thread.is_alive():
            self._mark_writer_error("Recorder writer thread did not stop before timeout.")

        if self._iq_spool_file is not None:
            try:
                self._iq_spool_file.flush()
                self._iq_spool_file.close()
            except Exception as exc:
                self._mark_writer_error(exc)
            self._iq_spool_file = None

        for handle in [self._windows_file, self._channel_file, self._subcarrier_file]:
            try:
                handle.flush()
                handle.close()
            except Exception as exc:
                self._mark_writer_error(exc)

        try:
            self._finalize_iq_csv()
        except Exception as exc:
            # Keep the binary spool for recovery if CSV finalization fails.
            self._mark_writer_error(exc)
            print(
                "[WARN] IQ CSV finalization failed; recoverable binary spool kept at "
                f"{self._iq_spool_path}: {self._writer_error}"
            )

        self._append_final_metadata()


def mean_angle(values: np.ndarray) -> float:
    """Return a circular mean angle, or NaN for an empty vector."""
    values = np.asarray(values)
    if values.size == 0:
        return math.nan
    return float(np.angle(np.mean(np.exp(1j * np.angle(values)))))

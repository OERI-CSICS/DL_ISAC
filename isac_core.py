#!/usr/bin/env python3
"""
OCUDU STEP 4 v3 — Flexible Multi-Antenna Spatial Activity Monitor.

Key capabilities
----------------
- Select 1, 2, 3, or 4 RX channels on a compatible X410 or N310.
- Schedule one aligned timed UHD stream for all selected channels.
- Show raw RF activity immediately, before the first PCI lock.
- Show synchronized residual activity during baseline and stillness calibration.
- Use the first selected array element, or --primary-index, for PSS/SSS sync.
- Apply the recovered timing and CFO correction to every selected RX channel.
- Estimate one 127-subcarrier PSS/SSS channel response per array element.
- Fuse per-channel frame-to-frame activity scores.
- Calibrate inter-channel phase relative to the primary element.
- Provide a relative spatial direction trend for arrays with >=2 elements.
- Preserve compact metrics and expose aligned sample ranges for optional AI/ML recording.

The monitor is passive receive-only. Use only in an authorized lab/testbed.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Deque, Optional

import numpy as np

from usrp_io import (DEFAULT_DEVICE_ARGS, configure_rx_channel, device_label, open_multi_usrp, validate_rx_channels)

PROJECT_DIR = Path(__file__).resolve().parent

try:
    from scipy.signal import resample_poly
except Exception as exc:
    raise RuntimeError(
        "SciPy is required. Install it with: python3 -m pip install scipy"
    ) from exc


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def resolve_project_path(path: str | Path) -> Path:
    """Resolve a project-relative resource path to an absolute path.

    Dashboard assets and helper modules can be supplied as absolute paths.
    Relative paths are resolved from this source directory so launch behavior
    does not depend on the caller's current working directory.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    return candidate.resolve()


def create_logo_label(
    QtCore,
    QtGui,
    QtWidgets,
    image_path: str | Path,
    *,
    maximum_width: int,
    maximum_height: int,
):
    """Create a small white-background QLabel containing a scaled logo.

    The function keeps the artwork's aspect ratio and raises a clear error if
    an expected branding asset is missing or cannot be decoded by Qt.
    """
    path = resolve_project_path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Dashboard logo not found: {path}")

    pixmap = QtGui.QPixmap(str(path))
    if pixmap.isNull():
        raise RuntimeError(f"Qt could not load dashboard logo: {path}")

    scaled = pixmap.scaled(
        int(maximum_width),
        int(maximum_height),
        QtCore.Qt.KeepAspectRatio,
        QtCore.Qt.SmoothTransformation,
    )
    label = QtWidgets.QLabel()
    label.setPixmap(scaled)
    label.setAlignment(QtCore.Qt.AlignCenter)
    label.setFixedSize(int(maximum_width), int(maximum_height))
    label.setStyleSheet("background: white; border: none; padding: 2px;")
    return label


def create_brand_header(
    QtCore,
    QtGui,
    QtWidgets,
    *,
    title_html: str,
    left_logo_path: str | Path | None = None,
    right_logo_path: str | Path | None = None,
):
    """Build the shared CSICS/ODU white dashboard header with corner logos.

    The package ships with the standard CSICS/ODU branding assets while still
    allowing --left-logo/--right-logo to override them at runtime.
    """
    header = QtWidgets.QFrame()
    header.setObjectName("ocuduBrandHeader")
    header.setStyleSheet(
        "QFrame#ocuduBrandHeader { background: white; border-bottom: 1px solid #d9e0e5; }"
    )
    header.setFixedHeight(92)

    layout = QtWidgets.QHBoxLayout(header)
    layout.setContentsMargins(12, 6, 12, 6)
    layout.setSpacing(12)

    left_logo = None
    right_logo = None
    if left_logo_path:
        left_logo = create_logo_label(
            QtCore, QtGui, QtWidgets, left_logo_path,
            maximum_width=320, maximum_height=76,
        )
    if right_logo_path:
        right_logo = create_logo_label(
            QtCore, QtGui, QtWidgets, right_logo_path,
            maximum_width=92, maximum_height=76,
        )

    title = QtWidgets.QLabel(title_html)
    title.setAlignment(QtCore.Qt.AlignCenter)
    title.setWordWrap(True)
    title.setStyleSheet(
        "background: white; color: #043657; font-size: 17px; padding: 2px 8px;"
    )

    if left_logo is not None:
        layout.addWidget(left_logo, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    else:
        layout.addStretch(1)
    layout.addWidget(title, 2)
    if right_logo is not None:
        layout.addWidget(right_logo, 0, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    else:
        layout.addStretch(1)
    return header


def load_module(path: str | Path, name: str) -> ModuleType:
    """Load a Python helper module from an absolute or project-relative path."""
    p = resolve_project_path(path)
    if not p.exists():
        raise FileNotFoundError(f"Required helper file not found: {p}")
    spec = importlib.util.spec_from_file_location(name, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import helper file: {p}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def parse_csv_ints(value: str) -> list[int]:
    """Parse csv ints from the supplied input."""
    try:
        items = [int(v.strip()) for v in value.split(",") if v.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected comma-separated integers.") from exc
    if not 1 <= len(items) <= 4:
        raise argparse.ArgumentTypeError(
            "--channels must contain 1 to 4 entries, e.g. 0 or 0,1 or 0,1,2,3"
        )
    if len(set(items)) != len(items):
        raise argparse.ArgumentTypeError("--channels must contain distinct channel indices.")
    if any(ch < 0 or ch > 3 for ch in items):
        raise argparse.ArgumentTypeError("RX channel indices must be in the range 0..3 for the supported X410/N310 configuration.")
    return items


def parse_csv_strings(value: str) -> list[str]:
    """Parse csv strings from the supplied input."""
    items = [v.strip() for v in value.split(",") if v.strip()]
    if not items:
        raise argparse.ArgumentTypeError("Expected at least one comma-separated value.")
    return items


def parse_csv_floats(value: str) -> list[float]:
    """Parse csv floats from the supplied input."""
    try:
        items = [float(v.strip()) for v in value.split(",") if v.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected comma-separated numeric values.") from exc
    if not items:
        raise argparse.ArgumentTypeError("Expected at least one numeric value.")
    return items


def expand_values(values: list, count: int, name: str) -> list:
    """Broadcast a single value or require one value per selected channel."""
    if len(values) == 1:
        return values * count
    if len(values) != count:
        raise ValueError(
            f"{name} must contain either one value or {count} values to match "
            f"the selected channels. Received: {values}"
        )
    return values


def normalize_common_phase(h: np.ndarray) -> np.ndarray:
    """Normalize common phase into the expected representation."""
    h = np.asarray(h, dtype=np.complex64)
    cpe = float(np.angle(np.mean(h) + 1e-12))
    return (h * np.exp(-1j * cpe)).astype(np.complex64)


def align_to_reference(h: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Handle the align to reference operation for this OCUDU processing stage."""
    h = np.asarray(h, dtype=np.complex64)
    reference = np.asarray(reference, dtype=np.complex64)
    common = float(np.angle(np.vdot(reference, h) + 1e-12))
    return (h * np.exp(-1j * common)).astype(np.complex64)


def amplitude_db(h: np.ndarray) -> np.ndarray:
    """Handle the amplitude db operation for this OCUDU processing stage."""
    return (20.0 * np.log10(np.abs(h) + 1e-12)).astype(np.float32)


def rms_db(iq: np.ndarray) -> float:
    """Handle the rms db operation for this OCUDU processing stage."""
    return float(20.0 * np.log10(np.sqrt(np.mean(np.abs(iq) ** 2)) + 1e-12))


def remove_common_mode(values: np.ndarray) -> np.ndarray:
    """Handle the remove common mode operation for this OCUDU processing stage."""
    x = np.asarray(values, dtype=np.float32)
    return (x - np.median(x)).astype(np.float32)


def detrend_phase_profile(phase: np.ndarray) -> np.ndarray:
    """Handle the detrend phase profile operation for this OCUDU processing stage."""
    y = np.unwrap(np.asarray(phase, dtype=np.float64))
    x = np.arange(len(y), dtype=np.float64)
    if len(y) >= 2:
        slope, intercept = np.polyfit(x, y, 1)
        y -= slope * x + intercept
    y -= np.median(y)
    return y.astype(np.float32)


def circular_mean(values: np.ndarray, axis=None) -> np.ndarray:
    """Handle the circular mean operation for this OCUDU processing stage."""
    return np.angle(np.mean(np.exp(1j * np.asarray(values)), axis=axis))


def robust_threshold(values: list[float], sigma: float, minimum: float) -> float:
    """Handle the robust threshold operation for this OCUDU processing stage."""
    if not values:
        return float(minimum)
    arr = np.asarray(values, dtype=np.float64)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    return float(max(float(minimum), median + float(sigma) * 1.4826 * mad))


def robust_limit(matrix: np.ndarray, minimum: float, maximum: float) -> float:
    """Handle the robust limit operation for this OCUDU processing stage."""
    arr = np.abs(np.asarray(matrix, dtype=np.float32))
    value = float(np.percentile(arr, 98.0)) if arr.size else minimum
    return float(np.clip(value, minimum, maximum))


# ---------------------------------------------------------------------------
# Event detector and channel estimate
# ---------------------------------------------------------------------------

@dataclass
class EventDetector:
    """Represent event detector state and behavior used by the OCUDU pipeline."""
    calibration_locks: int
    threshold_sigma: float
    minimum_threshold_db: float
    off_ratio: float
    scores: list[float] = field(default_factory=list)
    on_threshold_db: Optional[float] = None
    off_threshold_db: Optional[float] = None
    movement: bool = False

    def reset(self) -> None:
        """Reset the component to its initial calibration state."""
        self.scores.clear()
        self.on_threshold_db = None
        self.off_threshold_db = None
        self.movement = False

    @property
    def ready(self) -> bool:
        """Handle the ready operation for this OCUDU processing stage."""
        return self.on_threshold_db is not None

    @property
    def count(self) -> int:
        """Handle the count operation for this OCUDU processing stage."""
        return len(self.scores)

    def update(self, score_db: float) -> str:
        """Update the object with the supplied state changes."""
        score_db = float(score_db)
        if not self.ready:
            self.scores.append(score_db)
            if len(self.scores) >= int(self.calibration_locks):
                self.on_threshold_db = robust_threshold(
                    self.scores,
                    sigma=float(self.threshold_sigma),
                    minimum=float(self.minimum_threshold_db),
                )
                self.off_threshold_db = float(self.on_threshold_db) * float(self.off_ratio)
            return "CALIBRATING STILLNESS"

        if self.movement:
            if score_db < float(self.off_threshold_db):
                self.movement = False
        else:
            if score_db > float(self.on_threshold_db):
                self.movement = True

        return "MOVEMENT" if self.movement else "STILL"


@dataclass
class ChannelEstimate:
    """Represent channel estimate state and behavior used by the OCUDU pipeline."""
    spatial_h: np.ndarray
    sensing_h: np.ndarray
    pss_sss_similarity: float


def estimate_channel_from_grid(
    sync: ModuleType,
    grid: np.ndarray,
    nid1: int,
    nid2: int,
) -> ChannelEstimate:
    """Estimate channel from grid from the supplied signal data."""
    y_pss = np.asarray(grid[0, 56:183], dtype=np.complex64)
    y_sss = np.asarray(grid[2, 56:183], dtype=np.complex64)
    x_pss = np.asarray(sync.pss_sequence(int(nid2)), dtype=np.complex64)
    x_sss = np.asarray(sync.sss_sequence(int(nid1), int(nid2)), dtype=np.complex64)

    h_pss = y_pss / (x_pss + 1e-12)
    h_sss = y_sss / (x_sss + 1e-12)
    h_sss_aligned = align_to_reference(h_sss, h_pss)

    spatial_h = (0.5 * (h_pss + h_sss_aligned)).astype(np.complex64)
    sensing_h = normalize_common_phase(spatial_h)

    similarity = float(
        np.abs(np.vdot(normalize_common_phase(h_pss), normalize_common_phase(h_sss_aligned)))
        / ((np.linalg.norm(h_pss) * np.linalg.norm(h_sss_aligned)) + 1e-12)
    )

    return ChannelEstimate(
        spatial_h=spatial_h,
        sensing_h=sensing_h,
        pss_sss_similarity=similarity,
    )


# ---------------------------------------------------------------------------
# Generic 1..4 channel continuous receiver
# ---------------------------------------------------------------------------

class MultiChannelContinuousX410Receiver:
    """One simultaneous timed X410/N310 stream feeding a rolling ring buffer.

    The historical class name is retained for compatibility with existing scripts.
    """

    def __init__(
        self,
        step1,
        device_args: str,
        rate: float,
        freq: float,
        gains: list[float],
        antennas: list[str],
        channels: list[int],
        settle_time: float,
        window_samples: int,
        ring_windows: int = 3,
        stream_start_delay_s: float = 0.25,
        recv_timeout_s: float = 1.0,
        timeout_warning_interval_s: float = 5.0,
    ):
        """Initialize the MultiChannelContinuousX410Receiver instance and its runtime state."""
        self.uhd = step1.uhd
        if self.uhd is None:
            raise RuntimeError("UHD Python module not available.")

        self.channels = [int(ch) for ch in channels]
        self.nch = len(self.channels)
        self.window_samples = int(window_samples)
        self.capacity = int(max(2, ring_windows) * self.window_samples)
        self.ring = np.zeros((self.nch, self.capacity), dtype=np.complex64)
        self.write_pos = 0
        self.total_samples = 0
        self.generation = 0
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.error = None
        self.stream_start_delay_s = float(stream_start_delay_s)
        self.recv_timeout_s = max(0.1, float(recv_timeout_s))
        self.timeout_warning_interval_s = max(1.0, float(timeout_warning_interval_s))

        # Optional callback used by the ML dataset recorder.  The callback is
        # invoked with each aligned multi-channel packet after it is copied to
        # the ring buffer.  It is disabled by default, so normal monitoring and
        # all existing plots retain their original behavior.
        self.chunk_callback = None
        self.capture_chunk_id = 0

        self.usrp = open_multi_usrp(self.uhd, device_args)
        validate_rx_channels(self.usrp, self.channels)

        for array_index, channel in enumerate(self.channels):
            configure_rx_channel(
                self.usrp, self.uhd, channel, rate, freq, gains[array_index], antennas[array_index]
            )

        time.sleep(float(settle_time))

        self.actual_rates = [float(self.usrp.get_rx_rate(ch)) for ch in self.channels]
        self.actual_freqs = [float(self.usrp.get_rx_freq(ch)) for ch in self.channels]
        self.actual_gains = [float(self.usrp.get_rx_gain(ch)) for ch in self.channels]
        self.actual_antennas = [self.usrp.get_rx_antenna(ch) for ch in self.channels]

        stream_args = self.uhd.usrp.StreamArgs("fc32", "sc16")
        stream_args.channels = self.channels
        self.streamer = self.usrp.get_rx_stream(stream_args)

        self.md = self.uhd.types.RXMetadata()
        self.max_packet = int(self.streamer.get_max_num_samps())
        self.buff = np.zeros((self.nch, self.max_packet), dtype=np.complex64)

        self.thread = threading.Thread(
            target=self._capture_loop,
            name=f"usrp-rx-{','.join(str(c) for c in self.channels)}",
            daemon=True,
        )

    def start(self) -> None:
        """Start the component and allocate the required runtime resources."""
        cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.start_cont)

        # A shared future timestamp is safe for one or multiple channels and
        # guarantees aligned start boundaries when multiple channels are used.
        cmd.stream_now = False
        now = self.usrp.get_time_now()
        try:
            now_seconds = float(now.get_real_secs())
        except AttributeError:
            now_seconds = float(now)

        cmd.time_spec = self.uhd.types.TimeSpec(
            now_seconds + float(self.stream_start_delay_s)
        )

        print(
            "[INFO] Scheduling aligned RX stream for "
            f"{self.nch} channel(s) {self.stream_start_delay_s:.3f} s in the future."
        )
        self.streamer.issue_stream_cmd(cmd)
        self.thread.start()

    def _append(self, samples: np.ndarray) -> None:
        """Append an aligned packet and optionally forward it to a recorder."""
        samples = np.asarray(samples, dtype=np.complex64)
        n = samples.shape[1]

        if n >= self.capacity:
            samples = samples[:, -self.capacity:]
            n = samples.shape[1]

        with self.lock:
            sample_start = int(self.total_samples)
            first = min(n, self.capacity - self.write_pos)
            self.ring[:, self.write_pos:self.write_pos + first] = samples[:, :first]

            remaining = n - first
            if remaining:
                self.ring[:, :remaining] = samples[:, first:]

            self.write_pos = (self.write_pos + n) % self.capacity
            self.total_samples += n
            self.generation += 1
            chunk_id = int(self.capture_chunk_id)
            self.capture_chunk_id += 1

            if self.total_samples >= self.window_samples:
                self.ready.set()

        # Call outside the ring-buffer lock.  The recorder makes its own copy,
        # preventing disk I/O from exposing mutable UHD receive buffers.
        callback = self.chunk_callback
        if callback is not None:
            callback(
                samples,
                sample_start=sample_start,
                chunk_id=chunk_id,
            )

    def _capture_loop(self) -> None:
        """Receive aligned packets while rate-limiting repeated timeout logs.

        A timeout is not treated as a fatal error because short scheduler or
        network stalls can recover. Repeating the warning for every receive call
        can flood the terminal and further reduce responsiveness, so timeout
        messages are summarized at a fixed interval.
        """
        timeout_code = getattr(self.uhd.types.RXMetadataErrorCode, "timeout", None)
        timeout_count = 0
        last_timeout_report = 0.0
        try:
            while not self.stop_event.is_set():
                received = int(
                    self.streamer.recv(self.buff, self.md, self.recv_timeout_s)
                )

                if self.md.error_code != self.uhd.types.RXMetadataErrorCode.none:
                    if self.stop_event.is_set():
                        break

                    is_timeout = (
                        timeout_code is not None and self.md.error_code == timeout_code
                    )
                    if is_timeout:
                        timeout_count += 1
                        now = time.monotonic()
                        if (
                            timeout_count == 1
                            or now - last_timeout_report
                            >= self.timeout_warning_interval_s
                        ):
                            print(
                                "[WARN] RX metadata timeout "
                                f"({timeout_count} timeout(s) since last report). "
                                "Capture remains active; check network, CPU, and disk load.",
                                file=sys.stderr,
                            )
                            timeout_count = 0
                            last_timeout_report = now
                    else:
                        print(
                            f"[WARN] RX metadata: {self.md.strerror()}",
                            file=sys.stderr,
                        )
                    if received <= 0:
                        continue

                if received > 0:
                    self._append(self.buff[:, :received])

        except Exception as exc:
            self.error = exc
            self.ready.set()

    def latest_window(
        self,
        last_generation: int,
        timeout_s: float = 1.0,
        include_sample_range: bool = False,
    ):
        """Return the newest aligned window and, optionally, global sample bounds.

        Existing callers receive the original two-value result.  Dataset-aware
        callers request ``include_sample_range=True`` and receive
        ``(samples, generation, sample_start, sample_end_exclusive)``.
        """
        if not self.ready.wait(timeout=float(timeout_s)):
            if self.error is not None:
                raise RuntimeError(f"Continuous capture failed: {self.error}")
            if include_sample_range:
                return None, last_generation, None, None
            return None, last_generation

        with self.lock:
            generation = int(self.generation)
            if generation == int(last_generation) or self.total_samples < self.window_samples:
                if include_sample_range:
                    return None, last_generation, None, None
                return None, last_generation

            end = int(self.write_pos)
            start = (end - self.window_samples) % self.capacity
            sample_end = int(self.total_samples)
            sample_start = sample_end - int(self.window_samples)

            if start < end:
                out = self.ring[:, start:end].copy()
            else:
                out = np.concatenate(
                    (self.ring[:, start:], self.ring[:, :end]),
                    axis=1,
                ).copy()

            if include_sample_range:
                return out, generation, sample_start, sample_end
            return out, generation

    def stop(self) -> None:
        """Stop the component and release its runtime resources."""
        self.stop_event.set()
        try:
            cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.stop_cont)
            self.streamer.issue_stream_cmd(cmd)
        except Exception:
            pass

        if self.thread.is_alive():
            self.thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Synchronization helpers
# ---------------------------------------------------------------------------

def prepare_iq(
    raw: np.ndarray,
    rx_rate: float,
    detector_rate: float,
    normalize_rms: bool,
) -> np.ndarray:
    """Prepare iq for the next processing stage."""
    # Work on an independent array.  The previous in-place subtraction could
    # modify the receiver-owned raw window and corrupt both later channels and
    # the raw IQ dataset written for AI/ML training.
    x = np.array(raw, dtype=np.complex64, copy=True)
    x -= np.mean(x)

    if abs(float(rx_rate) - float(detector_rate)) <= 1.0:
        y = x
    else:
        ratio = Fraction(float(detector_rate) / float(rx_rate)).limit_denominator(512)
        y = resample_poly(x, up=ratio.numerator, down=ratio.denominator)

    y = np.asarray(y, dtype=np.complex64)
    y -= np.mean(y)

    if normalize_rms:
        y /= np.sqrt(np.mean(np.abs(y) ** 2)) + 1e-12

    return y.astype(np.complex64)


def extract_secondary_grid(
    step2,
    sync,
    raw: np.ndarray,
    result,
    fs: float,
    scs: float,
) -> np.ndarray:
    """Extract secondary grid from the supplied samples."""
    corrected = sync.apply_cfo_correction(raw, fs, float(result.cfo_hz))
    nfft = sync.nfft_from_rate(fs, scs)
    short_cp, _ = sync.normal_cp_lengths_samples(fs, scs)
    stride = nfft + int(short_cp)

    return step2.extract_grid_deterministic(
        sync=sync,
        iq=corrected,
        fs=fs,
        scs=scs,
        pss_useful_start=int(result.pss_useful_start),
        ssb_bin_offset=int(result.ssb_bin_offset),
        stride=int(stride),
    )


# ---------------------------------------------------------------------------
# Array calibration and activity metrics
# ---------------------------------------------------------------------------

@dataclass
class Calibration:
    """Represent calibration state and behavior used by the OCUDU pipeline."""
    target: int
    nch: int
    primary_index: int
    pending_sensing: list[np.ndarray] = field(default_factory=list)
    pending_spatial: list[np.ndarray] = field(default_factory=list)
    baseline_sensing: Optional[np.ndarray] = None
    baseline_spatial_ratio: Optional[np.ndarray] = None

    @property
    def ready(self) -> bool:
        """Handle the ready operation for this OCUDU processing stage."""
        return (
            self.baseline_sensing is not None
            and self.baseline_spatial_ratio is not None
        )

    @property
    def count(self) -> int:
        """Handle the count operation for this OCUDU processing stage."""
        return len(self.pending_sensing)

    def reset(self) -> None:
        """Reset the component to its initial calibration state."""
        self.pending_sensing.clear()
        self.pending_spatial.clear()
        self.baseline_sensing = None
        self.baseline_spatial_ratio = None

    def add(self, sensing_h: np.ndarray, spatial_h: np.ndarray) -> None:
        """Add a new observation to the aggregate catalog."""
        if self.ready:
            return

        self.pending_sensing.append(np.asarray(sensing_h, dtype=np.complex64))
        self.pending_spatial.append(np.asarray(spatial_h, dtype=np.complex64))

        if len(self.pending_sensing) < int(self.target):
            return

        sensing_stack = np.stack(self.pending_sensing, axis=0)
        spatial_stack = np.stack(self.pending_spatial, axis=0)

        self.baseline_sensing = np.asarray(
            [
                normalize_common_phase(np.mean(sensing_stack[:, m, :], axis=0))
                for m in range(self.nch)
            ],
            dtype=np.complex64,
        )

        primary = spatial_stack[:, self.primary_index, :]
        ratios = np.empty_like(spatial_stack, dtype=np.complex64)

        for m in range(self.nch):
            ratio = spatial_stack[:, m, :] * np.conj(primary)
            ratios[:, m, :] = ratio / (np.abs(ratio) + 1e-12)

        self.baseline_spatial_ratio = np.mean(ratios, axis=0).astype(np.complex64)

        self.pending_sensing.clear()
        self.pending_spatial.clear()


@dataclass
class ArrayActivity:
    """Represent array activity state and behavior used by the OCUDU pipeline."""
    antenna_scores_db: np.ndarray
    spatial_scores_db: np.ndarray
    fused_score_db: float
    event_row: np.ndarray
    amp_profiles: np.ndarray
    phase_profiles: np.ndarray
    spatial_phase_profiles: np.ndarray
    direction_value_rad: float
    direction: str


def provisional_activity(
    current_sensing: np.ndarray,
    previous_sensing: Optional[np.ndarray],
) -> ArrayActivity:
    """Handle the provisional activity operation for this OCUDU processing stage."""
    nch = current_sensing.shape[0]
    amp_profiles = np.asarray(
        [
            amplitude_db(current_sensing[m]) - np.median(amplitude_db(current_sensing[m]))
            for m in range(nch)
        ],
        dtype=np.float32,
    )
    phase_profiles = np.asarray(
        [detrend_phase_profile(np.angle(current_sensing[m])) for m in range(nch)],
        dtype=np.float32,
    )

    if previous_sensing is None:
        residual_rows = np.zeros((nch, 127), dtype=np.float32)
    else:
        rows = []
        for m in range(nch):
            aligned = align_to_reference(current_sensing[m], previous_sensing[m])
            rows.append(
                remove_common_mode(
                    amplitude_db(aligned) - amplitude_db(previous_sensing[m])
                )
            )
        residual_rows = np.asarray(rows, dtype=np.float32)

    antenna_scores = np.mean(np.abs(residual_rows), axis=1)
    event_row = np.mean(residual_rows, axis=0)

    return ArrayActivity(
        antenna_scores_db=antenna_scores,
        spatial_scores_db=np.zeros(nch, dtype=np.float32),
        fused_score_db=float(np.median(antenna_scores)),
        event_row=event_row.astype(np.float32),
        amp_profiles=amp_profiles,
        phase_profiles=phase_profiles,
        spatial_phase_profiles=np.zeros((nch, 127), dtype=np.float32),
        direction_value_rad=0.0,
        direction="CALIBRATING",
    )


def compute_array_activity(
    current_sensing: np.ndarray,
    current_spatial: np.ndarray,
    baseline: Calibration,
    previous_sensing: Optional[np.ndarray],
    previous_spatial: Optional[np.ndarray],
    positions_m: np.ndarray,
    reverse_labels: bool,
    direction_threshold_rad: float,
    spatial_weight: float,
) -> ArrayActivity:
    """Compute array activity from the supplied measurements."""
    nch = current_sensing.shape[0]
    primary_index = int(baseline.primary_index)

    amp_profiles = []
    phase_profiles = []
    residual_rows = []
    antenna_scores = []

    for m in range(nch):
        aligned = align_to_reference(current_sensing[m], baseline.baseline_sensing[m])

        amp_profiles.append(
            amplitude_db(aligned) - amplitude_db(baseline.baseline_sensing[m])
        )
        phase_profiles.append(
            detrend_phase_profile(
                np.angle(aligned * np.conj(baseline.baseline_sensing[m]))
            )
        )

        if previous_sensing is None:
            residual = np.zeros(127, dtype=np.float32)
        else:
            previous_aligned = align_to_reference(
                current_sensing[m],
                previous_sensing[m],
            )
            residual = remove_common_mode(
                amplitude_db(previous_aligned) - amplitude_db(previous_sensing[m])
            )

        residual_rows.append(residual)
        antenna_scores.append(float(np.mean(np.abs(residual))))

    residual_rows = np.asarray(residual_rows, dtype=np.float32)
    antenna_scores = np.asarray(antenna_scores, dtype=np.float32)
    event_row = np.mean(residual_rows, axis=0).astype(np.float32)

    spatial_phase_profiles = np.zeros((nch, 127), dtype=np.float32)
    spatial_scores = np.zeros(nch, dtype=np.float32)
    phase_means = np.zeros(nch, dtype=np.float32)

    primary_now = current_spatial[primary_index]

    for m in range(nch):
        ratio_now = current_spatial[m] * np.conj(primary_now)
        delta = np.angle(
            ratio_now * np.conj(baseline.baseline_spatial_ratio[m])
        ).astype(np.float32)

        spatial_phase_profiles[m] = detrend_phase_profile(delta)
        phase_means[m] = float(circular_mean(delta))

        if previous_spatial is not None:
            ratio_previous = (
                previous_spatial[m] * np.conj(previous_spatial[primary_index])
            )
            spatial_scores[m] = float(
                np.mean(
                    np.abs(
                        remove_common_mode(
                            amplitude_db(ratio_now) - amplitude_db(ratio_previous)
                        )
                    )
                )
            )

    valid_pair_scores = np.delete(spatial_scores, primary_index)
    spatial_score = (
        float(np.median(valid_pair_scores))
        if valid_pair_scores.size
        else 0.0
    )

    fused = float(
        max(
            float(np.median(antenna_scores)),
            float(spatial_weight) * spatial_score,
        )
    )

    if nch < 2:
        direction_value = 0.0
        direction = "DIRECTION REQUIRES >=2 ANTENNAS"
    else:
        # Use the selected array-element order as the physical line-array order.
        # Improve accuracy by using weighted least squares based on signal quality.
        positions = np.asarray(positions_m, dtype=np.float32)

        # Compute quality weights from spatial_scores (higher quality = higher weight)
        signal_quality = np.abs(spatial_scores).astype(np.float32)
        weights = signal_quality / (np.sum(signal_quality) + 1e-12)
        weights = np.maximum(weights, 0.1 / len(signal_quality))

        # Use weighted polyfit for more accurate direction estimation
        poly_coeffs = np.polyfit(positions, phase_means, 1, w=weights)
        slope = float(poly_coeffs[0])
        aperture = float(np.max(positions) - np.min(positions))
        direction_value = slope * aperture
        if reverse_labels:
            direction_value *= -1.0

        # More sensitive direction detection with movement presence indicator
        threshold = float(direction_threshold_rad)
        movement_presence = np.mean(signal_quality)

        if movement_presence > 0.15:
            if direction_value > threshold * 1.2:
                direction = "RIGHT MOVEMENT DETECTED"
            elif direction_value < -threshold * 1.2:
                direction = "LEFT MOVEMENT DETECTED"
            elif direction_value > threshold * 0.5:
                direction = "RIGHT TREND (WEAK)"
            elif direction_value < -threshold * 0.5:
                direction = "LEFT TREND (WEAK)"
            else:
                direction = "MOTION PRESENT / CENTER"
        else:
            if direction_value > threshold:
                direction = "RIGHT TREND"
            elif direction_value < -threshold:
                direction = "LEFT TREND"
            else:
                direction = "CENTER / UNCERTAIN"

    return ArrayActivity(
        antenna_scores_db=antenna_scores,
        spatial_scores_db=spatial_scores,
        fused_score_db=fused,
        event_row=event_row,
        amp_profiles=np.asarray(amp_profiles, dtype=np.float32),
        phase_profiles=np.asarray(phase_profiles, dtype=np.float32),
        spatial_phase_profiles=spatial_phase_profiles,
        direction_value_rad=float(direction_value),
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Dashboard: visible from startup, even before PCI lock
# ---------------------------------------------------------------------------

def setup_dashboard(args, labels: list[str], calibration: Calibration, detector: EventDetector):
    """Configure and return dashboard."""
    import matplotlib.pyplot as plt

    plt.ion()
    fig, axes = plt.subplots(3, 2, figsize=(16, 11))
    ax_amp, ax_phase, ax_waterfall, ax_score, ax_raw, ax_direction = axes.ravel()

    x = np.arange(127)
    history_x = np.arange(args.history)

    amp_lines = [
        ax_amp.plot(x, np.zeros(127), label=label)[0]
        for label in labels
    ]
    phase_lines = [
        ax_phase.plot(x, np.zeros(127), label=label)[0]
        for label in labels
    ]

    waterfall_history: Deque[np.ndarray] = deque(
        [np.zeros(127, dtype=np.float32) for _ in range(args.history)],
        maxlen=args.history,
    )
    image = ax_waterfall.imshow(
        np.vstack(waterfall_history),
        aspect="auto",
        origin="lower",
        extent=[0, 126, 0, args.history - 1],
        vmin=-args.wf_max_db,
        vmax=args.wf_max_db,
    )
    fig.colorbar(image, ax=ax_waterfall, label="Residual activity [dB]")

    score_history = [
        deque([0.0] * args.history, maxlen=args.history)
        for _ in range(len(labels) + 1)
    ]
    score_lines = [
        ax_score.plot(history_x, np.zeros(args.history), label=label)[0]
        for label in labels + ["fused"]
    ]

    raw_history = [
        deque([0.0] * args.history, maxlen=args.history)
        for _ in labels
    ]
    raw_lines = [
        ax_raw.plot(history_x, np.zeros(args.history), label=label)[0]
        for label in labels
    ]

    direction_history = deque([0.0] * args.history, maxlen=args.history)
    direction_line = ax_direction.plot(
        history_x,
        np.zeros(args.history),
        label="relative spatial phase trend",
    )[0]

    ax_amp.set(
        title="Per-element channel amplitude activity",
        xlabel="Reference-signal subcarrier index",
        ylabel="Amplitude [dB]",
        xlim=(0, 126),
        ylim=(-args.amp_ylim_db, args.amp_ylim_db),
    )
    ax_amp.grid(True)
    ax_amp.legend(loc="upper right")

    ax_phase.set(
        title="Per-element detrended phase activity",
        xlabel="Reference-signal subcarrier index",
        ylabel="Phase [rad]",
        xlim=(0, 126),
        ylim=(-1.0, 1.0),
    )
    ax_phase.grid(True)
    ax_phase.legend(loc="upper right")

    ax_waterfall.set(
        title="Live synchronized residual waterfall",
        xlabel="Reference-signal subcarrier index",
        ylabel="Recent valid PCI lock",
    )

    ax_score.set(
        title="Per-element and fused residual scores",
        xlabel="Recent valid PCI lock",
        ylabel="Residual score [dB]",
        xlim=(0, args.history - 1),
        ylim=(0, args.score_ylim_db),
    )
    ax_score.grid(True)
    ax_score.legend(loc="upper right")

    ax_raw.set(
        title="Raw RF RMS activity: visible before the first PCI lock",
        xlabel="Recent RX window",
        ylabel="RMS level [dBFS-like relative scale]",
        xlim=(0, args.history - 1),
        ylim=(-80, 10),
    )
    ax_raw.grid(True)
    ax_raw.legend(loc="upper right")

    ax_direction.set(
        title="Relative array direction trend",
        xlabel="Recent valid PCI lock",
        ylabel="Spatial phase trend [rad]",
        xlim=(0, args.history - 1),
        ylim=(-args.direction_ylim_rad, args.direction_ylim_rad),
    )
    ax_direction.grid(True)
    ax_direction.legend(loc="upper right")

    holder = {"previous_sensing": None, "previous_spatial": None}

    def on_key(event):
        """Handle the on key operation for this OCUDU processing stage."""
        if event.key == "r":
            calibration.reset()
            detector.reset()
            holder["previous_sensing"] = None
            holder["previous_spatial"] = None
            print("[CAL] Reset requested. Remain still during recalibration.")
        elif event.key == "d":
            args.reverse_direction_labels = not args.reverse_direction_labels
            print(f"[DIRECTION] reverse labels = {args.reverse_direction_labels}")

    fig.canvas.mpl_connect("key_press_event", on_key)
    fig.suptitle("OCUDU Flexible Array Monitor | WAITING FOR FIRST RX WINDOW")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.canvas.draw()
    fig.canvas.flush_events()

    return {
        "plt": plt,
        "fig": fig,
        "amp_lines": amp_lines,
        "phase_lines": phase_lines,
        "waterfall_history": waterfall_history,
        "image": image,
        "score_history": score_history,
        "score_lines": score_lines,
        "raw_history": raw_history,
        "raw_lines": raw_lines,
        "direction_history": direction_history,
        "direction_line": direction_line,
        "holder": holder,
    }


def update_raw_dashboard(state, args, raw_rms: np.ndarray, waiting_text: str) -> None:
    """Update raw dashboard with the newest state."""
    for m, value in enumerate(raw_rms):
        state["raw_history"][m].append(float(value))
        state["raw_lines"][m].set_ydata(np.asarray(state["raw_history"][m]))

    state["fig"].suptitle(
        "OCUDU Flexible Array Monitor | "
        f"{waiting_text} | raw activity is live"
    )
    state["fig"].canvas.draw_idle()
    state["fig"].canvas.flush_events()
    state["plt"].pause(args.pause_s)


def update_sync_dashboard(
    state,
    args,
    lock_index: int,
    result,
    activity: ArrayActivity,
    status: str,
    detector: EventDetector,
    calibration: Calibration,
    raw_rms: np.ndarray,
) -> None:
    """Update sync dashboard with the newest state."""
    for m in range(activity.amp_profiles.shape[0]):
        state["amp_lines"][m].set_ydata(activity.amp_profiles[m])
        state["phase_lines"][m].set_ydata(activity.phase_profiles[m])

    # Default behavior: always show activity from the first synchronized lock.
    # Triggered mode is still available when a quiet stillness display is desired.
    if args.waterfall_display == "triggered" and detector.ready and status == "STILL":
        waterfall_row = np.zeros(127, dtype=np.float32)
    else:
        waterfall_row = np.where(
            np.abs(activity.event_row) >= float(args.event_mask_db),
            activity.event_row,
            0.0,
        ).astype(np.float32)

    state["waterfall_history"].append(waterfall_row)
    matrix = np.vstack(state["waterfall_history"])
    state["image"].set_data(matrix)
    limit = robust_limit(matrix, args.wf_min_db, args.wf_max_db)
    state["image"].set_clim(-limit, limit)

    scores = list(activity.antenna_scores_db) + [activity.fused_score_db]
    for m, value in enumerate(scores):
        state["score_history"][m].append(float(value))
        state["score_lines"][m].set_ydata(np.asarray(state["score_history"][m]))

    for m, value in enumerate(raw_rms):
        state["raw_history"][m].append(float(value))
        state["raw_lines"][m].set_ydata(np.asarray(state["raw_history"][m]))

    state["direction_history"].append(float(activity.direction_value_rad))
    state["direction_line"].set_ydata(np.asarray(state["direction_history"]))

    if not calibration.ready:
        stage = f"BASELINE {calibration.count}/{calibration.target}"
    elif not detector.ready:
        stage = f"CALIBRATING STILLNESS {detector.count}/{detector.calibration_locks}"
    else:
        stage = status

    if detector.ready:
        threshold = (
            f"on={detector.on_threshold_db:.3f} dB "
            f"off={detector.off_threshold_db:.3f} dB"
        )
    else:
        threshold = "threshold pending"

    state["fig"].suptitle(
        "OCUDU Flexible Array Monitor | "
        f"{stage} | {activity.direction} | "
        f"fused={activity.fused_score_db:.3f} dB | "
        f"{threshold} | PCI={result.pci} | lock={lock_index}"
    )
    state["fig"].canvas.draw_idle()
    state["fig"].canvas.flush_events()
    state["plt"].pause(args.pause_s)


# ---------------------------------------------------------------------------
# Hardware-free self-test
# ---------------------------------------------------------------------------

def synthetic_self_test() -> int:
    """Run hardware-free regression checks for this processing component."""
    rng = np.random.default_rng(12)
    x = np.linspace(0.0, 1.0, 127)

    for nch in (1, 2, 4):
        primary_index = 0
        positions = np.arange(nch, dtype=float) * 0.0433

        baseline_spatial = []
        for m in range(nch):
            h = (
                (1.0 + 0.08 * np.cos(2 * np.pi * (2 + m) * x))
                * np.exp(1j * (0.10 * np.sin(2 * np.pi * x) + 0.20 * m))
            )
            baseline_spatial.append(h.astype(np.complex64))

        baseline_spatial = np.asarray(baseline_spatial)
        baseline_sensing = np.asarray(
            [normalize_common_phase(h) for h in baseline_spatial]
        )

        calibration = Calibration(target=8, nch=nch, primary_index=primary_index)
        for _ in range(8):
            calibration.add(baseline_sensing, baseline_spatial)

        detector = EventDetector(8, 4.0, 0.03, 0.65)
        previous_sensing = baseline_sensing.copy()
        previous_spatial = baseline_spatial.copy()

        for _ in range(8):
            still = compute_array_activity(
                baseline_sensing,
                baseline_spatial,
                calibration,
                previous_sensing,
                previous_spatial,
                positions,
                False,
                0.05,
                1.0,
            )
            detector.update(still.fused_score_db)

        moved_spatial = baseline_spatial.copy()
        for m in range(nch):
            moved_spatial[m] *= (
                1.0
                - (0.12 + 0.03 * m)
                * np.exp(-((x - (0.40 + 0.06 * m)) / 0.14) ** 2)
            )
            moved_spatial[m] *= np.exp(1j * (0.08 + 0.05 * m))

        moved_sensing = np.asarray(
            [normalize_common_phase(h) for h in moved_spatial]
        )

        moved = compute_array_activity(
            moved_sensing,
            moved_spatial,
            calibration,
            previous_sensing,
            previous_spatial,
            positions,
            False,
            0.05,
            1.0,
        )
        status = detector.update(moved.fused_score_db)

        print(f"[SELF-TEST] array elements={nch}")
        print(f"  fused score: {moved.fused_score_db:.6f} dB")
        print(f"  direction:   {moved.direction}")
        print(f"  status:      {status}")

        if not calibration.ready or status != "MOVEMENT":
            print("[SELF-TEST] FAIL")
            return 1

    print("[SELF-TEST] PASS")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the command-line entry point and return its process exit status."""
    ap = argparse.ArgumentParser(
        description="OCUDU Step 4 v3: flexible 1..4 antenna spatial activity monitor"
    )

    ap.add_argument("--step1-helper", default="nr_pss.py")
    ap.add_argument("--step2-helper", default="nr_cell_sync.py")
    ap.add_argument("--sync-helper", default="nr_sync.py")

    ap.add_argument("--device-args", default=DEFAULT_DEVICE_ARGS,
                    help="UHD device args; type is auto-detected for X410/N310 compatibility.")
    ap.add_argument("--channels", type=parse_csv_ints, default=[0, 1])
    ap.add_argument("--antennas", type=parse_csv_strings, default=["auto"],
                    help="Comma-separated RX antenna names, or auto to use each device default.")
    ap.add_argument("--gains", type=parse_csv_floats, default=[30.0])
    ap.add_argument("--primary-index", type=int, default=0)
    ap.add_argument("--spacing-m", type=float, default=0.0433)

    ap.add_argument("--dl-arfcn", type=int, default=632628)
    ap.add_argument("--ssb-arfcn", type=int, default=630912)
    ap.add_argument("--pci", type=int, default=1)
    ap.add_argument("--rate", type=float, default=7.68e6)
    ap.add_argument("--detector-rate", type=float, default=7.68e6)
    ap.add_argument("--scs", type=float, default=30e3)
    ap.add_argument("--burst-ms", type=float, default=12.0)
    ap.add_argument("--settle-time", type=float, default=0.5)
    ap.add_argument("--ring-windows", type=int, default=3)
    ap.add_argument("--snapshot-timeout-s", type=float, default=1.0)
    ap.add_argument("--stream-start-delay-s", type=float, default=0.25)

    # Synchronization thresholds inherited from the proven Step 2 pipeline.
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
    ap.add_argument("--event-threshold-sigma", type=float, default=6.0)
    ap.add_argument("--event-min-threshold-db", type=float, default=0.08)
    ap.add_argument("--event-off-ratio", type=float, default=0.65)
    ap.add_argument("--event-mask-db", type=float, default=0.03)
    ap.add_argument("--waterfall-display", choices=["all", "triggered"], default="all")
    ap.add_argument("--spatial-weight", type=float, default=1.0)
    ap.add_argument("--direction-threshold-rad", type=float, default=0.10)
    ap.add_argument("--reverse-direction-labels", action="store_true")

    ap.add_argument("--history", type=int, default=180)
    ap.add_argument("--plot-every", type=int, default=2)
    ap.add_argument("--raw-plot-every", type=int, default=5)
    ap.add_argument("--print-every", type=int, default=25)
    ap.add_argument("--pause-s", type=float, default=0.001)
    ap.add_argument("--amp-ylim-db", type=float, default=8.0)
    ap.add_argument("--score-ylim-db", type=float, default=1.5)
    ap.add_argument("--wf-min-db", type=float, default=0.08)
    ap.add_argument("--wf-max-db", type=float, default=2.0)
    ap.add_argument("--direction-ylim-rad", type=float, default=1.0)

    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--quiet-search", action="store_true")
    ap.add_argument("--save-metrics", default=None)
    ap.add_argument("--self-test", action="store_true")

    args = ap.parse_args()

    if args.self_test:
        raise SystemExit(synthetic_self_test())

    nch = len(args.channels)
    args.antennas = expand_values(args.antennas, nch, "--antennas")
    args.gains = [float(x) for x in expand_values(args.gains, nch, "--gains")]

    if args.primary_index < 0 or args.primary_index >= nch:
        raise ValueError(
            f"--primary-index must be in the range 0..{nch - 1} for "
            f"{nch} selected channels."
        )

    positions_m = np.arange(nch, dtype=np.float64) * float(args.spacing_m)
    labels = [
        f"A{array_index}:ch{channel}"
        for array_index, channel in enumerate(args.channels)
    ]

    step1 = load_module(args.step1_helper, "step1")
    step2 = load_module(args.step2_helper, "step2")
    sync = load_module(args.sync_helper, "sync")

    dl_freq = step1.nr_arfcn_to_freq_hz(args.dl_arfcn)
    ssb_freq = step1.nr_arfcn_to_freq_hz(args.ssb_arfcn)
    window_samples = int(round(args.burst_ms * 1e-3 * args.rate))

    calibration = Calibration(
        target=int(args.baseline_locks),
        nch=nch,
        primary_index=int(args.primary_index),
    )
    detector = EventDetector(
        calibration_locks=int(args.event_calibration_locks),
        threshold_sigma=float(args.event_threshold_sigma),
        minimum_threshold_db=float(args.event_min_threshold_db),
        off_ratio=float(args.event_off_ratio),
    )

    dashboard = (
        None
        if args.no_plot
        else setup_dashboard(args, labels, calibration, detector)
    )

    receiver = MultiChannelContinuousX410Receiver(
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
    )

    print("========== OCUDU Step 4 v3: Flexible Array Monitor ==========")
    print(f"Connected USRP:          {device_label(receiver.usrp, args.channels[0])}")
    print(f"DL carrier center:       {dl_freq / 1e6:.6f} MHz")
    print(f"SSB center:              {ssb_freq / 1e6:.6f} MHz")
    print(f"Selected channels:       {args.channels}")
    print(f"Array labels:            {labels}")
    print(f"Antennas:                {args.antennas}")
    print(f"Gains:                   {args.gains} dB")
    print(f"Primary sync element:    array index {args.primary_index} ({labels[args.primary_index]})")
    print(f"Array spacing:           {args.spacing_m:.4f} m")
    print(f"Waterfall display:       {args.waterfall_display}")
    print("Raw activity:            visible immediately after first RX window")
    print("Synchronized activity:   visible during baseline and stillness calibration")
    print("Keyboard:                r reset calibration, d reverse direction labels")
    print("Disk output:             disabled unless --save-metrics is supplied")
    print("Press Ctrl+C to stop.")

    receiver.start()

    print(f"Actual RX rates:         {[r / 1e6 for r in receiver.actual_rates]} MS/s")
    print(f"Actual antennas:         {receiver.actual_antennas}")

    csv_file = None
    csv_writer = None

    if args.save_metrics:
        csv_path = Path(args.save_metrics).expanduser().resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_file = csv_path.open("w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)

        header = [
            "timestamp_utc",
            "lock",
            "pci",
            "cfo_hz",
            "status",
            "direction",
            "direction_value_rad",
            "fused_score_db",
        ]
        header += [f"{label}_raw_rms_db" for label in labels]
        header += [f"{label}_event_score_db" for label in labels]
        header += [f"{label}_spatial_score_db" for label in labels]
        csv_writer.writerow(header)
        print(f"[CSV] Saving metrics to: {csv_path}")

    last_generation = -1
    locks = 0
    rejected = 0
    windows = 0
    last_plot_lock = 0
    previous_sensing = None
    previous_spatial = None

    try:
        while True:
            raw, generation = receiver.latest_window(
                last_generation=last_generation,
                timeout_s=float(args.snapshot_timeout_s),
            )
            if raw is None:
                continue

            last_generation = generation
            windows += 1
            raw_levels = np.asarray([rms_db(raw[m]) for m in range(nch)])

            # Make raw activity visible before synchronization succeeds.
            if (
                dashboard is not None
                and windows % max(1, int(args.raw_plot_every)) == 0
            ):
                update_raw_dashboard(
                    dashboard,
                    args,
                    raw_levels,
                    waiting_text=(
                        f"RX windows={windows} | PCI locks={locks} | "
                        "searching or calibrating"
                    ),
                )

            primary_raw = raw[int(args.primary_index)]
            sync_iq = prepare_iq(
                primary_raw,
                receiver.actual_rates[int(args.primary_index)],
                args.detector_rate,
                normalize_rms=True,
            )

            try:
                result, reason = step2.process_one_burst(step1, sync, sync_iq, args)
            except Exception as exc:
                result = None
                reason = f"sync exception: {exc}"

            if result is None or not result.accepted:
                rejected += 1
                if not args.quiet_search and rejected % 50 == 0:
                    print(f"[SEARCH] rejected={rejected} reason={reason}")
                continue

            locks += 1
            sensing = []
            spatial = []
            similarities = []

            for m in range(nch):
                iq = prepare_iq(
                    raw[m],
                    receiver.actual_rates[m],
                    args.detector_rate,
                    normalize_rms=False,
                )
                grid = extract_secondary_grid(
                    step2,
                    sync,
                    iq,
                    result,
                    args.detector_rate,
                    args.scs,
                )
                estimate = estimate_channel_from_grid(
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

            provisional = provisional_activity(sensing, previous_sensing)

            if not calibration.ready:
                calibration.add(sensing, spatial)
                activity = provisional
                status = f"BASELINE {calibration.count}/{calibration.target}"

                if locks == 1 or calibration.count % 20 == 0:
                    print(
                        "[BASELINE] "
                        f"collecting={calibration.count}/{calibration.target} "
                        f"lock={locks} similarity={np.mean(similarities):.3f} "
                        f"live_fused={activity.fused_score_db:.3f}dB"
                    )
            else:
                activity = compute_array_activity(
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

            if (
                dashboard is not None
                and (
                    locks == 1
                    or locks - last_plot_lock >= max(1, int(args.plot_every))
                )
            ):
                update_sync_dashboard(
                    dashboard,
                    args,
                    locks,
                    result,
                    activity,
                    status,
                    detector,
                    calibration,
                    raw_levels,
                )
                last_plot_lock = locks

            if csv_writer is not None:
                row = [
                    datetime.now(timezone.utc).isoformat(),
                    locks,
                    result.pci,
                    f"{result.cfo_hz:.6f}",
                    status,
                    activity.direction,
                    f"{activity.direction_value_rad:.6f}",
                    f"{activity.fused_score_db:.6f}",
                ]
                row += [f"{value:.6f}" for value in raw_levels]
                row += [f"{value:.6f}" for value in activity.antenna_scores_db]
                row += [f"{value:.6f}" for value in activity.spatial_scores_db]
                csv_writer.writerow(row)
                csv_file.flush()

            previous_sensing = sensing.copy()
            previous_spatial = spatial.copy()

            if dashboard is not None:
                dashboard["plt"].pause(args.pause_s)

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C received. Stopping flexible array monitor...")

    finally:
        receiver.stop()

        if dashboard is not None:
            try:
                dashboard["plt"].ioff()
                dashboard["plt"].close(dashboard["fig"])
            except Exception:
                pass

        if csv_file is not None:
            csv_file.close()

        print("[INFO] Flexible array monitor stopped.")


if __name__ == "__main__":
    main()

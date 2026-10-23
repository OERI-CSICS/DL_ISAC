#!/usr/bin/env python3
"""
OCUDU Step 1: over-the-air NR PSS detector for a passive USRP (X410 or N310).

This first-stage detector follows the MathWorks-style idea:
  * capture full-band IQ from the selected USRP at 122.88 MS/s;
  * digitally move the known OCUDU SSB center to DC;
  * decimate by 16 to 7.68 MS/s;
  * correlate with the three possible NR PSS templates;
  * compare correlation power against an adaptive energy threshold;
  * report N_ID2 and coarse SSB timing.

It does not decode SSS, PBCH, MIB, PDCCH, or CSI-RS.
It is passive receive-only. Use only in an authorized lab/testbed.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

from usrp_io import (DEFAULT_DEVICE_ARGS, configure_rx_channel, device_label, open_multi_usrp)

import numpy as np
from scipy.signal import find_peaks, lfilter, resample_poly

try:
    import uhd
except Exception:
    uhd = None

# Three persistent workers for the three independent PSS hypotheses.
_PSS_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="pss")


def _parallel_pss_corr(job):
    """Internal helper for parallel pss corr."""
    nid2, det_iq, nfft = job
    h = np.conj(pss_template(nid2, nfft)[::-1])
    corr = lfilter(h, [1.0], det_iq)
    return int(nid2), np.abs(corr) ** 2


def nr_arfcn_to_freq_hz(n_ref: int) -> float:
    """Handle the nr arfcn to freq hz operation for this OCUDU processing stage."""
    if 0 <= n_ref <= 599999:
        f_mhz = 0.005 * n_ref
    elif 600000 <= n_ref <= 2016666:
        f_mhz = 3000.0 + 0.015 * (n_ref - 600000)
    elif 2016667 <= n_ref <= 3279165:
        f_mhz = 24250.08 + 0.06 * (n_ref - 2016667)
    else:
        raise ValueError(f"Unsupported NR-ARFCN: {n_ref}")
    return f_mhz * 1e6


def pss_sequence(nid2: int) -> np.ndarray:
    """Handle the pss sequence operation for this OCUDU processing stage."""
    if nid2 not in (0, 1, 2):
        raise ValueError("N_ID2 must be 0, 1, or 2")
    x = np.zeros(134, dtype=np.int8)
    x[:7] = [0, 1, 1, 0, 1, 1, 1]
    for n in range(127):
        x[n + 7] = (x[n + 4] + x[n]) % 2
    n = np.arange(127)
    return (1 - 2 * x[(n + 43 * nid2) % 127]).astype(np.complex64)


def pss_template(nid2: int, nfft: int = 256) -> np.ndarray:
    """Handle the pss template operation for this OCUDU processing stage."""
    freq = np.zeros(nfft, dtype=np.complex64)
    start = nfft // 2 - 64
    freq[start:start + 127] = pss_sequence(nid2)
    td = np.fft.ifft(np.fft.ifftshift(freq))
    return (td / (np.linalg.norm(td) + 1e-12)).astype(np.complex64)


@dataclass
class Detection:
    """Represent detection state and behavior used by the OCUDU pipeline."""
    nid2: int
    sample_index_7p68: int
    time_ms: float
    peak_over_threshold: float
    peak_over_median: float
    peak_power: float
    threshold_power: float


class X410Receiver:
    """Reuse one UHD streamer for repeated finite bursts on X410 or N310.

    The historical class name is retained for compatibility with existing scripts.
    """
    def __init__(self, device_args: str, rate: float, freq: float, gain: float,
                 antenna: str, channel: int, settle_time: float) -> None:
        """Initialize the X410Receiver instance and its runtime state."""
        if uhd is None:
            raise RuntimeError(
                "UHD Python module is missing. Test with: "
                "python3 -c 'import uhd; print(uhd.__file__)'"
            )
        self.channel = int(channel)
        self.usrp = open_multi_usrp(uhd, device_args)
        configure_rx_channel(self.usrp, uhd, self.channel, rate, freq, gain, antenna)
        time.sleep(float(settle_time))

        self.actual_rate = float(self.usrp.get_rx_rate(self.channel))
        self.actual_freq = float(self.usrp.get_rx_freq(self.channel))
        self.actual_gain = float(self.usrp.get_rx_gain(self.channel))
        self.actual_antenna = self.usrp.get_rx_antenna(self.channel)

        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [self.channel]
        self.streamer = self.usrp.get_rx_stream(st_args)
        self.md = uhd.types.RXMetadata()
        self.max_packet = int(self.streamer.get_max_num_samps())
        self.buff = np.zeros((1, self.max_packet), dtype=np.complex64)

    def capture(self, duration_s: float) -> np.ndarray:
        """Capture one aligned block of receive samples."""
        num_samps = int(round(duration_s * self.actual_rate))
        out = np.zeros(num_samps, dtype=np.complex64)
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        cmd.num_samps = num_samps
        cmd.stream_now = True
        self.streamer.issue_stream_cmd(cmd)

        total = 0
        while total < num_samps:
            got = int(self.streamer.recv(self.buff, self.md, 5.0))
            if self.md.error_code != uhd.types.RXMetadataErrorCode.none:
                print(f"[WARN] RX metadata: {self.md.strerror()}", file=sys.stderr)
                if got <= 0:
                    continue
            n = min(got, num_samps - total)
            out[total:total+n] = self.buff[0, :n]
            total += n
        return out


def shift_decimate(iq: np.ndarray, fs_in: float, offset_hz: float) -> np.ndarray:
    """Handle the shift decimate operation for this OCUDU processing stage."""
    n = np.arange(len(iq), dtype=np.float64)
    mixed = iq * np.exp(-1j * 2 * np.pi * offset_hz * n / fs_in)
    reduced = resample_poly(mixed, up=1, down=16)
    reduced = np.asarray(reduced, dtype=np.complex64)
    reduced -= np.mean(reduced)
    reduced /= np.sqrt(np.mean(np.abs(reduced) ** 2)) + 1e-12
    return reduced


def detect_all(
    det_iq: np.ndarray,
    threshold_db: float,
    peak_window: int,
    minimum_threshold: float,
    guard_samples: int = 512,
    max_candidates: int = 24,
    cluster_radius_samples: int = 64,
    nfft: int = 256,
    sample_rate: float = 7.68e6,
) -> Tuple[list[Detection], np.ndarray, np.ndarray, np.ndarray]:
    """Return ranked PSS candidates for all three N_ID2 hypotheses.

    Candidates are sorted by ``peak_over_threshold`` and clustered in time so
    that one physical PSS event is not returned multiple times from nearby
    matched-filter peaks.  This API is used by the FR1 raster scanner; the
    legacy :func:`detect` wrapper below still returns only the strongest item.
    """
    nfft = int(nfft)
    if nfft <= 0:
        raise ValueError("nfft must be positive")
    corr_power = np.zeros((len(det_iq), 3), dtype=np.float64)

    jobs = [(nid2, det_iq, nfft) for nid2 in (0, 1, 2)]
    for nid2, power in _PSS_EXECUTOR.map(_parallel_pss_corr, jobs):
        corr_power[:, nid2] = power

    sig_energy = lfilter(np.ones(nfft), [1.0], np.abs(det_iq) ** 2)
    threshold = np.maximum(
        sig_energy * (10 ** (threshold_db / 10.0)),
        minimum_threshold,
    )

    valid = np.ones(len(det_iq), dtype=bool)
    guard = max(0, int(guard_samples))
    if guard > 0 and len(valid) > 2 * guard:
        valid[:guard] = False
        valid[-guard:] = False

    found: list[Detection] = []
    for nid2 in (0, 1, 2):
        masked = corr_power[:, nid2].copy()
        masked[~valid] = 0.0
        height = threshold.copy()
        height[~valid] = np.inf

        locs, _ = find_peaks(
            masked,
            height=height,
            distance=max(1, int(peak_window)),
        )

        med = float(np.median(corr_power[valid, nid2]) + 1e-12)
        for idx in locs:
            peak = float(corr_power[idx, nid2])
            thr = float(threshold[idx])
            found.append(Detection(
                nid2=nid2,
                sample_index_7p68=int(idx),
                time_ms=float(idx / float(sample_rate) * 1e3),
                peak_over_threshold=float(peak / (thr + 1e-12)),
                peak_over_median=float(peak / med),
                peak_power=peak,
                threshold_power=thr,
            ))

    max_ratios = np.zeros(3, dtype=np.float64)
    for nid2 in (0, 1, 2):
        ratios = corr_power[valid, nid2] / (threshold[valid] + 1e-12)
        max_ratios[nid2] = float(np.max(ratios)) if ratios.size else 0.0

    ranked = sorted(found, key=lambda d: d.peak_over_threshold, reverse=True)
    selected: list[Detection] = []
    radius = max(0, int(cluster_radius_samples))
    for candidate in ranked:
        # Peaks with the same N_ID2 and nearly the same timing represent one
        # matched-filter event.  Keep the strongest representative.
        duplicate = any(
            candidate.nid2 == prior.nid2
            and abs(candidate.sample_index_7p68 - prior.sample_index_7p68) <= radius
            for prior in selected
        )
        if duplicate:
            continue
        selected.append(candidate)
        if len(selected) >= max(1, int(max_candidates)):
            break

    return selected, corr_power, threshold, max_ratios


def detect(det_iq: np.ndarray, threshold_db: float, peak_window: int,
           minimum_threshold: float, guard_samples: int = 512,
           nfft: int = 256, sample_rate: float = 7.68e6
           ) -> Tuple[Optional[Detection], np.ndarray, np.ndarray, np.ndarray]:
    """Backward-compatible strongest-candidate PSS detector."""
    candidates, corr_power, threshold, max_ratios = detect_all(
        det_iq=det_iq,
        threshold_db=threshold_db,
        peak_window=peak_window,
        minimum_threshold=minimum_threshold,
        guard_samples=guard_samples,
        max_candidates=1,
        nfft=int(nfft),
        sample_rate=float(sample_rate),
    )
    strongest = candidates[0] if candidates else None
    return strongest, corr_power, threshold, max_ratios

def _relative_db(values: np.ndarray, reference: float) -> np.ndarray:
    """Internal helper for relative db."""
    return 10.0 * np.log10(np.maximum(values, 1e-12) / max(reference, 1e-12))


def setup_plot(detector_samples: int, y_min_db: float, y_max_db: float,
               zoom_radius: int):
    """Create a fixed-axis two-panel diagnostic display."""
    import matplotlib.pyplot as plt

    plt.ion()
    fig, (ax_full, ax_zoom) = plt.subplots(2, 1, figsize=(13, 8))

    full_lines = [
        ax_full.plot([], [], label=f"PSS {i}")[0]
        for i in (0, 1, 2)
    ]
    full_threshold = ax_full.plot([], [], label="Adaptive threshold")[0]

    zoom_lines = [
        ax_zoom.plot([], [], label=f"PSS {i}")[0]
        for i in (0, 1, 2)
    ]
    zoom_threshold = ax_zoom.plot([], [], label="Adaptive threshold")[0]

    ax_full.set_title("OCUDU Step 1 OTA PSS Detector — Fixed Full Window")
    ax_full.set_xlabel("Detector sample index at 7.68 MS/s")
    ax_full.set_ylabel("Power relative to median threshold [dB]")
    ax_full.set_xlim(0, max(1, detector_samples - 1))
    ax_full.set_ylim(float(y_min_db), float(y_max_db))
    ax_full.grid(True)
    ax_full.legend(loc="upper right")

    ax_zoom.set_title("Strongest Correlation Neighborhood")
    ax_zoom.set_xlabel("Samples relative to strongest correlation location")
    ax_zoom.set_ylabel("Power relative to median threshold [dB]")
    ax_zoom.set_xlim(-int(zoom_radius), int(zoom_radius))
    ax_zoom.set_ylim(float(y_min_db), float(y_max_db))
    ax_zoom.grid(True)
    ax_zoom.legend(loc="upper right")

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.canvas.draw()
    fig.canvas.flush_events()

    return (
        plt, fig, ax_full, ax_zoom,
        full_lines, full_threshold,
        zoom_lines, zoom_threshold,
        int(zoom_radius),
    )


def update_plot(state, corr_power, threshold, det, burst, max_ratios):
    """Update plot with the newest state."""
    (
        plt, fig, ax_full, ax_zoom,
        full_lines, full_threshold,
        zoom_lines, zoom_threshold,
        zoom_radius,
    ) = state

    reference = float(np.median(threshold) + 1e-12)

    # Fixed full-window axis. Only the samples change.
    step = max(1, len(threshold) // 6000)
    x = np.arange(0, len(threshold), step)
    corr_db = _relative_db(corr_power, reference)
    threshold_db = _relative_db(threshold, reference)

    for i, line in enumerate(full_lines):
        line.set_data(x, corr_db[::step, i])
    full_threshold.set_data(x, threshold_db[::step])

    # Zoom around the detector result, or around the strongest raw correlation
    # if there is no threshold-crossing result yet.
    if det is not None:
        center = int(det.sample_index_7p68)
    else:
        center = int(np.unravel_index(np.argmax(corr_power), corr_power.shape)[0])

    left = max(0, center - zoom_radius)
    right = min(len(threshold), center + zoom_radius + 1)
    rel_x = np.arange(left, right) - center

    for i, line in enumerate(zoom_lines):
        line.set_data(rel_x, corr_db[left:right, i])
    zoom_threshold.set_data(rel_x, threshold_db[left:right])

    if det is None:
        fig.suptitle(
            "OCUDU OTA PSS Detector | "
            f"burst={burst} | searching | "
            f"max corr/thr: PSS0={max_ratios[0]:.3f}, "
            f"PSS1={max_ratios[1]:.3f}, PSS2={max_ratios[2]:.3f}"
        )
    else:
        fig.suptitle(
            "OCUDU OTA PSS Detector | "
            f"burst={burst} | N_ID2={det.nid2} | "
            f"time={det.time_ms:.4f} ms | "
            f"peak/thr={det.peak_over_threshold:.3f}"
        )

    fig.canvas.draw_idle()
    fig.canvas.flush_events()
    plt.pause(0.03)


def self_test() -> int:
    """Run hardware-free regression checks for this processing component."""
    rng = np.random.default_rng(1234)
    signal = (0.03*rng.standard_normal(12000) + 1j*0.03*rng.standard_normal(12000)).astype(np.complex64)
    insertion = 4200
    signal[insertion:insertion+256] += 12.0 * pss_template(1)
    det, _, _, _ = detect(signal, -6.0, 11, 1e-8, guard_samples=128)
    if det is None or det.nid2 != 1:
        print("[SELF-TEST] FAIL")
        return 1
    print("[SELF-TEST] PASS")
    print(f"Recovered N_ID2={det.nid2}, sample={det.sample_index_7p68}, peak/thr={det.peak_over_threshold:.3f}")
    return 0


def main() -> None:
    """Run the command-line entry point and return its process exit status."""
    ap = argparse.ArgumentParser(description="OCUDU Step 1 OTA PSS detector")
    ap.add_argument("--device-args", default=DEFAULT_DEVICE_ARGS,
                    help="UHD device args. Default omits type so either X410 or N310 can be discovered by address.")
    ap.add_argument("--dl-arfcn", type=int, default=632628)
    ap.add_argument("--ssb-arfcn", type=int, default=630912)
    ap.add_argument("--pci", type=int, default=1)
    ap.add_argument("--rate", type=float, default=122.88e6)
    ap.add_argument("--gain", type=float, default=30.0)
    ap.add_argument("--antenna", default="auto",
                    help="RX antenna name, or auto to keep the connected USRP default.")
    ap.add_argument("--channel", type=int, default=0)
    ap.add_argument("--burst-ms", type=float, default=20.0)
    ap.add_argument("--threshold-db", type=float, default=-6.0)
    ap.add_argument("--peak-window", type=int, default=11)
    ap.add_argument("--minimum-threshold", type=float, default=1e-8)
    ap.add_argument("--settle-time", type=float, default=0.5)
    ap.add_argument("--pause-s", type=float, default=0.08)
    ap.add_argument("--guard-samples", type=int, default=512,
                    help="Ignore detector-rate edge samples after finite-burst resampling.")
    ap.add_argument("--plot-y-min-db", type=float, default=-30.0)
    ap.add_argument("--plot-y-max-db", type=float, default=30.0)
    ap.add_argument("--zoom-radius", type=int, default=500)
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        raise SystemExit(self_test())

    if abs(args.rate / 16 - 7.68e6) > 1.0:
        raise ValueError("Use --rate 122.88e6 for the Step 1 detector.")

    dl_freq = nr_arfcn_to_freq_hz(args.dl_arfcn)
    ssb_freq = nr_arfcn_to_freq_hz(args.ssb_arfcn)
    ssb_offset = ssb_freq - dl_freq
    expected_nid2 = args.pci % 3

    rx = X410Receiver(args.device_args, args.rate, dl_freq, args.gain,
                      args.antenna, args.channel, args.settle_time)

    print("========== OCUDU Step 1: OTA PSS Detector ==========")
    print(f"DL center frequency:     {dl_freq/1e6:.6f} MHz")
    print(f"SSB center frequency:    {ssb_freq/1e6:.6f} MHz")
    print(f"Digital SSB offset:      {ssb_offset/1e6:.6f} MHz")
    print(f"Expected PCI:            {args.pci}")
    print(f"Expected N_ID2:          {expected_nid2}")
    print(f"Connected USRP:          {device_label(rx.usrp, rx.channel)}")
    print(f"Actual USRP RX rate:     {rx.actual_rate/1e6:.6f} MS/s")
    print("Detector sample rate:    7.680000 MS/s")
    print("Detector NFFT:           256")
    print(f"Adaptive threshold:      {args.threshold_db:.2f} dB")
    print("Disk output:             disabled")
    print("Press Ctrl+C to stop.")

    detector_samples = int(round(args.burst_ms * 1e-3 * 7.68e6))
    plot_state = None if args.no_plot else setup_plot(
        detector_samples=detector_samples,
        y_min_db=args.plot_y_min_db,
        y_max_db=args.plot_y_max_db,
        zoom_radius=args.zoom_radius,
    )
    burst = 0
    matches = 0
    try:
        while True:
            burst += 1
            iq = rx.capture(args.burst_ms * 1e-3)
            det_iq = shift_decimate(iq, rx.actual_rate, ssb_offset)
            det, corr_power, threshold, max_ratios = detect(
                det_iq,
                args.threshold_db,
                args.peak_window,
                args.minimum_threshold,
                guard_samples=args.guard_samples,
            )
            if det is None:
                print(
                    f"[SEARCH] burst={burst} no PSS peak above threshold "
                    f"max_corr/thr: PSS0={max_ratios[0]:.3f} "
                    f"PSS1={max_ratios[1]:.3f} PSS2={max_ratios[2]:.3f}"
                )
            else:
                status = "MATCH" if det.nid2 == expected_nid2 else "REJECT"
                if status == "MATCH":
                    matches += 1
                full_rate_approx = det.sample_index_7p68 * 16
                print(
                    f"[{status}] burst={burst} N_ID2={det.nid2} expected={expected_nid2} "
                    f"time={det.time_ms:.6f}ms full_rate_sample≈{full_rate_approx} "
                    f"peak/thr={det.peak_over_threshold:.3f} "
                    f"peak/median={det.peak_over_median:.3f} matches={matches}"
                )
            if plot_state is not None:
                update_plot(plot_state, corr_power, threshold, det, burst, max_ratios)
            else:
                time.sleep(args.pause_s)
    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C received. Stopping Step 1 detector...")
    finally:
        if plot_state is not None:
            try:
                plot_state[0].close(plot_state[1])
            except Exception:
                pass
        print("[INFO] Step 1 detector stopped. No files were written.")


if __name__ == "__main__":
    main()

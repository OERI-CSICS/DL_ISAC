#!/usr/bin/env python3
"""
Standards-aware NR downlink synchronization stage for passive USRP captures.

This module is the next stage after isac_jcas04_fast.py. It consumes:
  1. a saved complex IQ capture (.npy or complex64 binary), and
  2. the PSS-candidate JSON produced by the first-stage script.

It performs:
  - local fine PSS timing and frequency-bin refinement,
  - CP-correlation carrier-frequency-offset (CFO) estimation,
  - CFO correction,
  - CP-aware extraction of the four SS/PBCH-block OFDM symbols,
  - standards-based SSS generation and detection,
  - physical-cell-ID recovery: N_ID_cell = 3*N_ID1 + N_ID2,
  - standards-mapped SS/PBCH resource masks,
  - JSON, NumPy, and plot outputs for the next PBCH-DMRS/channel stage.

Scope:
  - passive receive-side analysis only;
  - normal cyclic prefix;
  - tested design target: 30 kHz SCS and 122.88 MS/s (NFFT=4096);
  - this module does not decode PBCH/MIB yet.

References:
  - 3GPP TS 38.211 clauses 7.4.2 and 7.4.3.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Basic file helpers
# ---------------------------------------------------------------------------

def load_iq(path: str | Path) -> np.ndarray:
    """Load iq from the configured source."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"IQ file not found: {p}")
    if p.suffix == ".npy":
        x = np.load(p)
    else:
        x = np.fromfile(p, dtype=np.complex64)
    if x.ndim == 2:
        # The previous stage stores one row per channel. This stage currently
        # synchronizes one channel at a time.
        x = x[0]
    if x.ndim != 1:
        raise ValueError(f"Expected one complex IQ vector, got shape={x.shape}")
    return np.asarray(x, dtype=np.complex64)


def save_json(path: str | Path, obj) -> None:
    """Save json to the requested output path."""
    Path(path).write_text(json.dumps(obj, indent=2), encoding="utf-8")


def infer_sample_rate(input_path: str | Path, explicit_rate: float | None) -> float:
    """Handle the infer sample rate operation for this OCUDU processing stage."""
    if explicit_rate is not None:
        return float(explicit_rate)

    p = Path(input_path)
    meta_path = Path(str(p) + ".json")
    if not meta_path.exists():
        raise ValueError(
            "Sample rate was not provided and capture metadata was not found. "
            "Pass --rate, for example: --rate 122.88e6"
        )

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    actual = meta.get("actual_uhd_settings", {})
    rates = actual.get("rx_rate_hz", [])
    if rates:
        return float(rates[0])
    if "sample_rate_actual_hz" in meta:
        return float(meta["sample_rate_actual_hz"])
    raise ValueError(f"Could not infer sample rate from {meta_path}")


def default_detection_path(input_path: str | Path) -> Path:
    """Handle the default detection path operation for this OCUDU processing stage."""
    p = Path(input_path)
    return p.with_suffix("").with_name(p.stem + "_pss_detections.json")


def preprocess_iq(iq: np.ndarray) -> np.ndarray:
    """Remove complex DC offset and normalize RMS only for analysis."""
    x = np.asarray(iq, dtype=np.complex64).copy()
    x -= np.mean(x)
    x /= np.sqrt(np.mean(np.abs(x) ** 2)) + 1e-12
    return x.astype(np.complex64)


# ---------------------------------------------------------------------------
# NR numerology and exact CP candidates
# ---------------------------------------------------------------------------

def nfft_from_rate(fs: float, scs: float) -> int:
    """Handle the nfft from rate operation for this OCUDU processing stage."""
    val = float(fs) / float(scs)
    nfft = int(round(val))
    if abs(val - nfft) > 1e-6:
        raise ValueError(
            f"Sample rate must be an integer multiple of SCS for this module: "
            f"fs/scs={val:.9f}"
        )
    return nfft


def numerology_from_scs(scs: float) -> int:
    """Handle the numerology from scs operation for this OCUDU processing stage."""
    ratio = float(scs) / 15e3
    mu = int(round(math.log2(ratio)))
    if abs(ratio - 2**mu) > 1e-9:
        raise ValueError("SCS must equal 15 kHz * 2^mu")
    return mu


def normal_cp_lengths_samples(fs: float, scs: float) -> Tuple[int, int]:
    """Return short and long normal-CP lengths in samples.

    The formulas are evaluated in the receiver sampling domain. For the
    intended 30 kHz / 122.88 MS/s case they return:
      short CP = 288 samples
      long CP  = 352 samples

    The long CP absorbs the normal-CP timing adjustment required by the NR
    slot/subframe structure. The synchronization search tests both candidates
    locally because the absolute SS/PBCH-block symbol number is not known yet.
    """
    nfft = nfft_from_rate(fs, scs)
    mu = numerology_from_scs(scs)

    # Short CP is the scaled 144/2048 useful-symbol proportion.
    short_cp = int(round(nfft * 144 / 2048))

    # A slot has 14 normal-CP symbols and duration 1 ms / 2^mu.
    slot_samples = int(round(fs * 1e-3 / (2**mu)))
    slot_cp_budget = slot_samples - 14 * nfft

    # For mu >= 1, each slot begins with one adjusted CP. This is the relevant
    # configuration for the intended 30 kHz SCS case.
    if mu >= 1:
        long_cp = int(slot_cp_budget - 13 * short_cp)
    else:
        # At 15 kHz, the 1 ms slot contains two adjusted CP positions.
        # Solve the per-adjusted-symbol length from the slot budget.
        long_cp = int(round((slot_cp_budget - 12 * short_cp) / 2))

    if short_cp <= 0 or long_cp < short_cp:
        raise ValueError(
            f"Invalid CP lengths derived from fs={fs}, scs={scs}: "
            f"short={short_cp}, long={long_cp}"
        )
    return short_cp, long_cp


# ---------------------------------------------------------------------------
# 3GPP TS 38.211 synchronization sequences
# ---------------------------------------------------------------------------

def pss_sequence(nid2: int) -> np.ndarray:
    """Generate the 127-element NR PSS for N_ID2 in {0,1,2}."""
    if nid2 not in (0, 1, 2):
        raise ValueError("N_ID2 must be 0, 1, or 2")
    x = np.zeros(134, dtype=np.int8)
    x[:7] = [0, 1, 1, 0, 1, 1, 1]
    for i in range(127):
        x[i + 7] = (x[i + 4] + x[i]) % 2
    n = np.arange(127)
    m = (n + 43 * int(nid2)) % 127
    return (1 - 2 * x[m]).astype(np.complex64)


def sss_sequence(nid1: int, nid2: int) -> np.ndarray:
    """Generate the 127-element NR SSS for N_ID1 and N_ID2.

    3GPP TS 38.211 clause 7.4.2.3.1:
      m0 = 15*floor(N_ID1/112) + 5*N_ID2
      m1 = N_ID1 mod 112
    """
    if not 0 <= nid1 <= 335:
        raise ValueError("N_ID1 must be in [0, 335]")
    if nid2 not in (0, 1, 2):
        raise ValueError("N_ID2 must be 0, 1, or 2")

    x0 = np.zeros(134, dtype=np.int8)
    x1 = np.zeros(134, dtype=np.int8)
    x0[0] = 1
    x1[0] = 1
    for i in range(127):
        x0[i + 7] = (x0[i + 4] + x0[i]) % 2
        x1[i + 7] = (x1[i + 1] + x1[i]) % 2

    n = np.arange(127)
    m0 = 15 * (int(nid1) // 112) + 5 * int(nid2)
    m1 = int(nid1) % 112
    a = 1 - 2 * x0[(n + m0) % 127]
    b = 1 - 2 * x1[(n + m1) % 127]
    return (a * b).astype(np.complex64)


# ---------------------------------------------------------------------------
# Local PSS refinement and CP-based CFO estimation
# ---------------------------------------------------------------------------

def normalized_corr(a: np.ndarray, b: np.ndarray) -> float:
    """Handle the normalized corr operation for this OCUDU processing stage."""
    den = float(np.linalg.norm(a) * np.linalg.norm(b) + 1e-12)
    return float(abs(np.vdot(a, b)) / den)


def extract_ofdm_fft(iq: np.ndarray, useful_start: int, nfft: int) -> np.ndarray:
    """Extract ofdm fft from the supplied samples."""
    if useful_start < 0 or useful_start + nfft > len(iq):
        raise ValueError(
            f"OFDM symbol outside capture: start={useful_start}, "
            f"nfft={nfft}, len={len(iq)}"
        )
    return np.fft.fftshift(np.fft.fft(iq[useful_start : useful_start + nfft]))


@dataclass
class PSSRefinement:
    """Represent p s s refinement state and behavior used by the OCUDU pipeline."""
    nid2: int
    useful_start: int
    ssb_bin_offset: int
    pss_corr: float


def refine_pss_local(
    iq: np.ndarray,
    nfft: int,
    coarse_start: int,
    coarse_offset: int,
    coarse_nid2: int | None,
    timing_radius: int,
    bin_radius: int,
) -> PSSRefinement:
    """Fine-search PSS timing and SSB center offset around a coarse candidate."""
    best: PSSRefinement | None = None
    nid2_values: Sequence[int] = (coarse_nid2,) if coarse_nid2 in (0, 1, 2) else (0, 1, 2)

    lo_t = max(0, int(coarse_start) - int(timing_radius))
    hi_t = min(len(iq) - nfft, int(coarse_start) + int(timing_radius))

    for useful_start in range(lo_t, hi_t + 1):
        freq = extract_ofdm_fft(iq, useful_start, nfft)
        for off in range(int(coarse_offset) - int(bin_radius), int(coarse_offset) + int(bin_radius) + 1):
            # Interpret off as the SS/PBCH-block center anchor. Per
            # TS 38.211 Table 7.4.3.1-1, PSS occupies SSB subcarriers
            # k=56..182 within the 240-subcarrier block. With the block
            # extracted as center-120:center+120, that maps to
            # center-64:center+63 (Python end-exclusive).
            center = nfft // 2 + off
            ssb_left = center - 120
            left = ssb_left + 56
            right = ssb_left + 183
            if ssb_left < 0 or ssb_left + 240 > nfft:
                continue
            observed = freq[left:right]
            for nid2 in nid2_values:
                score = normalized_corr(pss_sequence(int(nid2)), observed)
                candidate = PSSRefinement(
                    nid2=int(nid2),
                    useful_start=int(useful_start),
                    ssb_bin_offset=int(off),
                    pss_corr=float(score),
                )
                if best is None or candidate.pss_corr > best.pss_corr:
                    best = candidate

    if best is None:
        raise RuntimeError("Local PSS refinement found no valid candidate.")
    return best


@dataclass
class CPEstimate:
    """Represent c p estimate state and behavior used by the OCUDU pipeline."""
    cp_len: int
    useful_start: int
    metric: float
    cfo_hz: float


def cp_correlation_at(
    iq: np.ndarray,
    useful_start: int,
    nfft: int,
    fs: float,
    cp_len: int,
) -> CPEstimate | None:
    """Estimate residual CFO from the repeated CP/tail samples of one symbol."""
    cp_start = int(useful_start) - int(cp_len)
    tail_start = int(useful_start) + int(nfft) - int(cp_len)
    if cp_start < 0 or tail_start + cp_len > len(iq):
        return None

    cp = iq[cp_start : cp_start + cp_len]
    tail = iq[tail_start : tail_start + cp_len]
    z = np.vdot(cp, tail)
    metric = float(abs(z) / ((np.linalg.norm(cp) * np.linalg.norm(tail)) + 1e-12))
    cfo_hz = float(np.angle(z) * fs / (2 * np.pi * nfft))
    return CPEstimate(
        cp_len=int(cp_len),
        useful_start=int(useful_start),
        metric=metric,
        cfo_hz=cfo_hz,
    )


def estimate_cfo_from_pss_cp(
    iq: np.ndarray,
    useful_start: int,
    nfft: int,
    fs: float,
    cp_candidates: Sequence[int],
    timing_radius: int,
) -> CPEstimate:
    """Search a small timing neighborhood and select the strongest CP metric."""
    best: CPEstimate | None = None
    for start in range(int(useful_start) - timing_radius, int(useful_start) + timing_radius + 1):
        for cp_len in cp_candidates:
            candidate = cp_correlation_at(iq, start, nfft, fs, int(cp_len))
            if candidate is not None and (best is None or candidate.metric > best.metric):
                best = candidate
    if best is None:
        raise RuntimeError("Could not estimate CFO from cyclic-prefix correlation.")
    return best


def apply_cfo_correction(iq: np.ndarray, fs: float, cfo_hz: float) -> np.ndarray:
    """Handle the apply cfo correction operation for this OCUDU processing stage."""
    n = np.arange(len(iq), dtype=np.float64)
    rot = np.exp(-1j * 2 * np.pi * float(cfo_hz) * n / float(fs))
    return (iq * rot).astype(np.complex64)


# ---------------------------------------------------------------------------
# CP-aware SS/PBCH-block extraction
# ---------------------------------------------------------------------------

@dataclass
class SymbolTiming:
    """Represent symbol timing state and behavior used by the OCUDU pipeline."""
    symbol_in_ssb: int
    useful_start: int
    cp_len: int
    cp_metric: float


def locate_next_symbol(
    iq: np.ndarray,
    previous_useful_start: int,
    nfft: int,
    fs: float,
    cp_candidates: Sequence[int],
    search_radius: int,
    symbol_in_ssb: int,
) -> SymbolTiming:
    """Locate the next OFDM symbol using local CP correlation."""
    expected = int(previous_useful_start) + int(nfft) + int(min(cp_candidates))
    best: CPEstimate | None = None

    for start in range(expected - search_radius, expected + search_radius + 1):
        for cp_len in cp_candidates:
            candidate = cp_correlation_at(iq, start, nfft, fs, int(cp_len))
            if candidate is not None and (best is None or candidate.metric > best.metric):
                best = candidate

    if best is None:
        raise RuntimeError(f"Could not locate SS/PBCH symbol {symbol_in_ssb}.")
    return SymbolTiming(
        symbol_in_ssb=int(symbol_in_ssb),
        useful_start=int(best.useful_start),
        cp_len=int(best.cp_len),
        cp_metric=float(best.metric),
    )


def extract_ssb_grid_cp_aware(
    iq: np.ndarray,
    fs: float,
    scs: float,
    pss_useful_start: int,
    ssb_bin_offset: int,
    pss_cp_len: int,
    pss_cp_metric: float,
    symbol_search_radius: int,
) -> Tuple[np.ndarray, List[SymbolTiming]]:
    """Extract ssb grid cp aware from the supplied samples."""
    nfft = nfft_from_rate(fs, scs)
    short_cp, long_cp = normal_cp_lengths_samples(fs, scs)
    cp_candidates = sorted(set([short_cp, long_cp]))

    timings: List[SymbolTiming] = [
        SymbolTiming(
            symbol_in_ssb=0,
            useful_start=int(pss_useful_start),
            cp_len=int(pss_cp_len),
            cp_metric=float(pss_cp_metric),
        )
    ]

    for k in range(1, 4):
        timings.append(
            locate_next_symbol(
                iq=iq,
                previous_useful_start=timings[-1].useful_start,
                nfft=nfft,
                fs=fs,
                cp_candidates=cp_candidates,
                search_radius=int(symbol_search_radius),
                symbol_in_ssb=k,
            )
        )

    center_bin = nfft // 2 + int(ssb_bin_offset)
    left = center_bin - 120
    right = center_bin + 120
    if left < 0 or right > nfft:
        raise ValueError(
            f"SSB extraction outside FFT: center_bin={center_bin}, left={left}, "
            f"right={right}, nfft={nfft}"
        )

    grid = []
    for timing in timings:
        freq = extract_ofdm_fft(iq, timing.useful_start, nfft)
        grid.append(freq[left:right])
    return np.asarray(grid, dtype=np.complex64), timings


# ---------------------------------------------------------------------------
# SSS / PCI recovery and standards-mapped resource masks
# ---------------------------------------------------------------------------

@dataclass
class SSSDetection:
    """Represent s s s detection state and behavior used by the OCUDU pipeline."""
    nid1: int
    nid2: int
    pci: int
    sss_corr: float
    runner_up_corr: float
    peak_ratio: float


def detect_sss(ssb_grid: np.ndarray, nid2: int) -> SSSDetection:
    """Detect sss in the supplied signal data."""
    if ssb_grid.shape != (4, 240):
        raise ValueError(f"Expected a 4x240 SSB grid, got shape={ssb_grid.shape}")

    observed = ssb_grid[2, 56:183]
    scored: List[Tuple[float, int]] = []
    for nid1 in range(336):
        score = normalized_corr(sss_sequence(nid1, nid2), observed)
        scored.append((score, nid1))
    scored.sort(reverse=True)

    best_score, best_nid1 = scored[0]
    runner_score = scored[1][0]
    return SSSDetection(
        nid1=int(best_nid1),
        nid2=int(nid2),
        pci=int(3 * best_nid1 + nid2),
        sss_corr=float(best_score),
        runner_up_corr=float(runner_score),
        peak_ratio=float(best_score / (runner_score + 1e-12)),
    )


def build_ssb_resource_masks(pci: int) -> Dict[str, np.ndarray]:
    """Build 4x240 masks from TS 38.211 Table 7.4.3.1-1."""
    masks = {
        "pss": np.zeros((4, 240), dtype=bool),
        "sss": np.zeros((4, 240), dtype=bool),
        "pbch_dmrs": np.zeros((4, 240), dtype=bool),
        "pbch_candidate": np.zeros((4, 240), dtype=bool),
        "set_to_zero": np.zeros((4, 240), dtype=bool),
    }

    masks["pss"][0, 56:183] = True
    masks["sss"][2, 56:183] = True

    v = int(pci) % 4
    masks["pbch_dmrs"][1, v:240:4] = True
    masks["pbch_dmrs"][3, v:240:4] = True
    masks["pbch_dmrs"][2, v:48:4] = True
    masks["pbch_dmrs"][2, 192 + v : 240 : 4] = True

    masks["pbch_candidate"][1, :] = True
    masks["pbch_candidate"][2, :48] = True
    masks["pbch_candidate"][2, 192:] = True
    masks["pbch_candidate"][3, :] = True
    masks["pbch_candidate"] &= ~masks["pbch_dmrs"]

    masks["set_to_zero"][0, :56] = True
    masks["set_to_zero"][0, 183:] = True
    masks["set_to_zero"][2, 48:56] = True
    masks["set_to_zero"][2, 183:192] = True
    return masks


# ---------------------------------------------------------------------------
# Output plotting
# ---------------------------------------------------------------------------

def save_sync_plots(
    prefix: str,
    ssb_grid: np.ndarray,
    masks: Dict[str, np.ndarray],
    timings: Sequence[SymbolTiming],
) -> None:
    """Save sync plots to the requested output path."""
    import matplotlib.pyplot as plt

    power_db = 20 * np.log10(np.abs(ssb_grid) + 1e-12)
    plt.figure(figsize=(12, 4))
    plt.imshow(power_db, aspect="auto", origin="lower", extent=[0, 239, 0, 3])
    plt.colorbar(label="Magnitude (dB)")
    plt.title("CP-Aware SS/PBCH Block Grid: 4 OFDM Symbols x 240 Subcarriers")
    plt.xlabel("SS/PBCH-block subcarrier index")
    plt.ylabel("SS/PBCH-block OFDM symbol index")
    plt.tight_layout()
    plt.savefig(f"{prefix}_nr_sync_ssb_grid.png", dpi=180)
    plt.close()

    label_grid = np.zeros((4, 240), dtype=np.int16)
    label_grid[masks["pbch_candidate"]] = 1
    label_grid[masks["pbch_dmrs"]] = 2
    label_grid[masks["pss"]] = 3
    label_grid[masks["sss"]] = 4
    label_grid[masks["set_to_zero"]] = 5

    plt.figure(figsize=(12, 4))
    image = plt.imshow(label_grid, aspect="auto", origin="lower", extent=[0, 239, 0, 3])
    cbar = plt.colorbar(image, ticks=[0, 1, 2, 3, 4, 5])
    cbar.ax.set_yticklabels(["unused", "PBCH", "PBCH DM-RS", "PSS", "SSS", "set to zero"])
    plt.title("Standards-Mapped SS/PBCH Resource Regions")
    plt.xlabel("SS/PBCH-block subcarrier index")
    plt.ylabel("SS/PBCH-block OFDM symbol index")
    plt.tight_layout()
    plt.savefig(f"{prefix}_nr_sync_resource_map.png", dpi=180)
    plt.close()

    starts = [t.useful_start for t in timings]
    metrics = [t.cp_metric for t in timings]
    plt.figure(figsize=(8, 4))
    plt.plot(range(4), metrics, marker="o")
    plt.title("CP-Correlation Metric for Extracted SS/PBCH Symbols")
    plt.xlabel("SS/PBCH-block OFDM symbol index")
    plt.ylabel("Normalized CP correlation")
    plt.xticks(range(4))
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(f"{prefix}_nr_sync_cp_metrics.png", dpi=180)
    plt.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def read_detection_candidate(
    detection_path: str | Path,
    rank: int,
    override_nid2: int | None,
    override_start: int | None,
    override_offset: int | None,
) -> Dict:
    """Read and return detection candidate."""
    p = Path(detection_path)
    if not p.exists():
        raise FileNotFoundError(
            f"PSS detection JSON not found: {p}\n"
            "Run isac_jcas04_fast.py first, or pass explicit "
            "--nid2, --pss-start, and --ssb-bin-offset values."
        )

    detections = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(detections, list) or not detections:
        raise ValueError(f"No PSS candidates found in {p}")
    if not 0 <= rank < len(detections):
        raise ValueError(f"--detection-rank must be in [0, {len(detections)-1}]")

    candidate = dict(detections[rank])
    if override_nid2 is not None:
        candidate["nid2"] = int(override_nid2)
    if override_start is not None:
        candidate["sample_index"] = int(override_start)
    if override_offset is not None:
        candidate["ssb_bin_offset"] = int(override_offset)

    for key in ("nid2", "sample_index", "ssb_bin_offset"):
        if key not in candidate:
            raise ValueError(f"Detection candidate is missing required field: {key}")
    return candidate


def main() -> None:
    """Run the command-line entry point and return its process exit status."""
    ap = argparse.ArgumentParser(
        description="Standards-aware NR SS/PBCH synchronization stage for passive USRP IQ captures"
    )
    ap.add_argument("--input", required=True, help="Saved .npy IQ capture from isac_jcas04_fast.py")
    ap.add_argument("--rate", type=float, default=None, help="Actual sample rate; inferred from metadata when omitted")
    ap.add_argument("--scs", type=float, default=30e3, help="SS/PBCH subcarrier spacing in Hz")
    ap.add_argument("--detections", default="", help="PSS detections JSON from the first stage")
    ap.add_argument("--detection-rank", type=int, default=0, help="Candidate rank from the PSS detections JSON")
    ap.add_argument("--nid2", type=int, default=None, help="Override coarse N_ID2")
    ap.add_argument("--pss-start", type=int, default=None, help="Override coarse PSS useful-symbol start")
    ap.add_argument("--ssb-bin-offset", type=int, default=None, help="Override coarse SSB center-bin offset")
    ap.add_argument("--timing-radius", type=int, default=192, help="Local PSS timing-search radius in samples")
    ap.add_argument("--bin-radius", type=int, default=24, help="Local SSB center-bin refinement radius")
    ap.add_argument("--cp-timing-radius", type=int, default=32, help="Local CP timing-search radius for the PSS")
    ap.add_argument("--symbol-search-radius", type=int, default=96, help="CP timing-search radius for later SSB symbols")
    ap.add_argument("--no-preprocess", action="store_true", help="Disable DC removal and RMS normalization")
    ap.add_argument("--no-plots", action="store_true", help="Skip PNG outputs")
    args = ap.parse_args()

    input_path = Path(args.input)
    fs = infer_sample_rate(input_path, args.rate)
    scs = float(args.scs)
    nfft = nfft_from_rate(fs, scs)
    short_cp, long_cp = normal_cp_lengths_samples(fs, scs)
    cp_candidates = sorted(set([short_cp, long_cp]))

    detection_path = Path(args.detections) if args.detections else default_detection_path(input_path)
    coarse = read_detection_candidate(
        detection_path=detection_path,
        rank=args.detection_rank,
        override_nid2=args.nid2,
        override_start=args.pss_start,
        override_offset=args.ssb_bin_offset,
    )

    raw_iq = load_iq(input_path)
    iq = raw_iq if args.no_preprocess else preprocess_iq(raw_iq)

    print("========== Standards-Aware NR Synchronization ==========")
    print(f"Input IQ:                 {input_path}")
    print(f"Detection JSON:           {detection_path}")
    print(f"Sample rate:              {fs/1e6:.6f} MS/s")
    print(f"SS/PBCH SCS:              {scs/1e3:.1f} kHz")
    print(f"NFFT:                     {nfft}")
    print(f"Normal CP candidates:     short={short_cp}, long={long_cp} samples")
    print(f"Coarse candidate:         {json.dumps(coarse, indent=2)}")

    refined_pre = refine_pss_local(
        iq=iq,
        nfft=nfft,
        coarse_start=int(coarse["sample_index"]),
        coarse_offset=int(coarse["ssb_bin_offset"]),
        coarse_nid2=int(coarse["nid2"]),
        timing_radius=int(args.timing_radius),
        bin_radius=int(args.bin_radius),
    )
    print("Fine PSS before CFO correction:")
    print(json.dumps(asdict(refined_pre), indent=2))

    cp_est = estimate_cfo_from_pss_cp(
        iq=iq,
        useful_start=refined_pre.useful_start,
        nfft=nfft,
        fs=fs,
        cp_candidates=cp_candidates,
        timing_radius=int(args.cp_timing_radius),
    )
    print("CP-based CFO estimate:")
    print(json.dumps(asdict(cp_est), indent=2))

    corrected = apply_cfo_correction(iq, fs, cp_est.cfo_hz)

    # Refine again after CFO correction using the locally CP-aligned timing.
    refined_post = refine_pss_local(
        iq=corrected,
        nfft=nfft,
        coarse_start=cp_est.useful_start,
        coarse_offset=refined_pre.ssb_bin_offset,
        coarse_nid2=refined_pre.nid2,
        timing_radius=int(args.cp_timing_radius),
        bin_radius=4,
    )
    print("Fine PSS after CFO correction:")
    print(json.dumps(asdict(refined_post), indent=2))

    # Re-evaluate the corrected PSS CP metric at the refined timing.
    corrected_cp_options = [
        cp_correlation_at(corrected, refined_post.useful_start, nfft, fs, cp_len)
        for cp_len in cp_candidates
    ]
    corrected_cp_options = [x for x in corrected_cp_options if x is not None]
    if not corrected_cp_options:
        raise RuntimeError("Could not evaluate corrected CP metric.")
    corrected_cp = max(corrected_cp_options, key=lambda x: x.metric)

    ssb_grid, timings = extract_ssb_grid_cp_aware(
        iq=corrected,
        fs=fs,
        scs=scs,
        pss_useful_start=refined_post.useful_start,
        ssb_bin_offset=refined_post.ssb_bin_offset,
        pss_cp_len=corrected_cp.cp_len,
        pss_cp_metric=corrected_cp.metric,
        symbol_search_radius=int(args.symbol_search_radius),
    )

    sss = detect_sss(ssb_grid, refined_post.nid2)
    masks = build_ssb_resource_masks(sss.pci)

    prefix = input_path.with_suffix("").as_posix()
    grid_path = f"{prefix}_nr_sync_ssb_grid.npy"
    mask_path = f"{prefix}_nr_sync_resource_masks.npz"
    summary_path = f"{prefix}_nr_sync_summary.json"

    np.save(grid_path, ssb_grid)
    np.savez(mask_path, **masks)

    summary = {
        "module": "standards-aware NR synchronization stage",
        "input_iq": str(input_path),
        "detection_json": str(detection_path),
        "sample_rate_hz": float(fs),
        "scs_hz": float(scs),
        "nfft": int(nfft),
        "normal_cp_candidates_samples": {
            "short": int(short_cp),
            "long": int(long_cp),
        },
        "coarse_candidate": coarse,
        "fine_pss_before_cfo": asdict(refined_pre),
        "cp_based_cfo_estimate": asdict(cp_est),
        "fine_pss_after_cfo": asdict(refined_post),
        "symbol_timings": [asdict(t) for t in timings],
        "sss_detection": asdict(sss),
        "ssb_center_frequency_offset_hz": float(refined_post.ssb_bin_offset * scs),
        "outputs": {
            "ssb_grid_npy": grid_path,
            "resource_masks_npz": mask_path,
        },
        "next_stage": (
            "Validate PBCH DM-RS hypotheses across candidate SSB indices, "
            "estimate the reference channel, then decode PBCH/MIB."
        ),
    }
    save_json(summary_path, summary)

    if not args.no_plots:
        save_sync_plots(prefix, ssb_grid, masks, timings)

    print("Recovered NR synchronization result:")
    print(json.dumps(asdict(sss), indent=2))
    print(f"Residual CFO corrected:      {cp_est.cfo_hz:.3f} Hz")
    print(f"SSB center offset:           {refined_post.ssb_bin_offset} bins "
          f"({refined_post.ssb_bin_offset * scs / 1e6:.6f} MHz)")
    print(f"Saved CP-aware SSB grid:     {grid_path}")
    print(f"Saved resource masks:        {mask_path}")
    print(f"Saved synchronization JSON:  {summary_path}")
    if not args.no_plots:
        print(f"Saved plot:                  {prefix}_nr_sync_ssb_grid.png")
        print(f"Saved plot:                  {prefix}_nr_sync_resource_map.png")
        print(f"Saved plot:                  {prefix}_nr_sync_cp_metrics.png")


if __name__ == "__main__":
    main()

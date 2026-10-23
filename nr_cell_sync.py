#!/usr/bin/env python3
"""
STEP 2 v5 CONTINUOUS-MULTICORE — OCUDU OTA SSS / PCI synchronization viewer for a passive USRP (X410 or N310).

This revision keeps the successful synchronization logic and removes the full-band Python resampling bottleneck:
  * use PSS for coarse timing;
  * estimate and correct residual CFO from the PSS CP;
  * refine the PSS useful-symbol start locally;
  * for 30-kHz FR1 SSB Case B, use the deterministic short-CP stride between
    the four consecutive SS/PBCH symbols;
  * search the SSS symbol locally around its expected symbol-2 location;
  * recover N_ID1 and validate PCI=1;
  * show a fixed SSB-centered spectrum and the best recovered 4x240 grid.

The script is passive receive-only and writes no files by default.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Optional, Tuple
from functools import lru_cache
from fractions import Fraction

import numpy as np

from usrp_io import (DEFAULT_DEVICE_ARGS, configure_rx_channel, device_label, open_multi_usrp)

try:
    from scipy.signal import resample_poly
except Exception as exc:
    raise RuntimeError(
        "SciPy is required. Install it with: python3 -m pip install scipy"
    ) from exc


def load_module(path: str | Path, name: str) -> ModuleType:
    """Load module from the configured source."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Required helper file not found: {p}")
    spec = importlib.util.spec_from_file_location(name, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import helper file: {p}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class SyncResult:
    """Represent sync result state and behavior used by the OCUDU pipeline."""
    grid: np.ndarray
    nid1: int
    nid2: int
    pci: int
    pss_corr: float
    sss_corr: float
    sss_peak_ratio: float
    cp_metric: float
    cfo_hz: float
    coarse_time_ms: float
    coarse_peak_over_threshold: float
    coarse_peak_over_median: float
    pss_useful_start: int
    sss_useful_start: int
    sss_timing_delta: int
    ssb_bin_offset: int
    accepted: bool
    rejection_reason: str


@lru_cache(maxsize=3)
def _sss_bank(sync_module_name: str, nid2: int):
    """Precompute all 336 SSS sequences for a given N_ID2."""
    sync = sys.modules[sync_module_name]
    bank = np.stack(
        [sync.sss_sequence(nid1, int(nid2)) for nid1 in range(336)],
        axis=0,
    ).astype(np.complex64)
    norms = np.linalg.norm(bank, axis=1) + 1e-12
    return bank, norms


def score_sss_symbol(sync, freq: np.ndarray, ssb_bin_offset: int, nid2: int):
    """Recover N_ID1 with one vectorized matrix-vector correlation."""
    nfft = len(freq)
    center = nfft // 2 + int(ssb_bin_offset)
    left = center - 120 + 56
    right = center - 120 + 183
    if left < 0 or right > nfft:
        return None

    observed = np.asarray(freq[left:right], dtype=np.complex64)
    observed_norm = float(np.linalg.norm(observed) + 1e-12)
    bank, norms = _sss_bank(sync.__name__, int(nid2))

    corr = np.abs(bank.conj() @ observed) / (norms * observed_norm)
    order = np.argsort(corr)
    best_nid1 = int(order[-1])
    best_corr = float(corr[best_nid1])
    runner = float(corr[int(order[-2])])

    return {
        'nid1': best_nid1,
        'nid2': int(nid2),
        'pci': int(3 * best_nid1 + int(nid2)),
        'sss_corr': best_corr,
        'runner_up_corr': runner,
        'peak_ratio': float(best_corr / (runner + 1e-12)),
    }


def extract_grid_deterministic(sync, iq: np.ndarray, fs: float, scs: float,
                               pss_useful_start: int, ssb_bin_offset: int,
                               stride: int) -> np.ndarray:
    """Extract four consecutive SSB symbols using the selected deterministic stride."""
    nfft = sync.nfft_from_rate(fs, scs)
    center = nfft // 2 + int(ssb_bin_offset)
    left = center - 120
    right = center + 120
    if left < 0 or right > nfft:
        raise ValueError('SSB extraction lies outside FFT bounds.')
    rows = []
    for k in range(4):
        start = int(pss_useful_start) + k * int(stride)
        freq = sync.extract_ofdm_fft(iq, start, nfft)
        rows.append(freq[left:right])
    return np.asarray(rows, dtype=np.complex64)


def estimate_pss_cp(sync, iq: np.ndarray, useful_start: int, nfft: int,
                    fs: float, short_cp: int, radius: int):
    """Use only the short CP expected for the Case-B SS/PBCH symbols."""
    best = None
    for start in range(int(useful_start) - int(radius), int(useful_start) + int(radius) + 1):
        item = sync.cp_correlation_at(iq, start, nfft, fs, int(short_cp))
        if item is not None and (best is None or item.metric > best.metric):
            best = item
    if best is None:
        raise RuntimeError('Could not estimate CFO from the short PSS cyclic prefix.')
    return best


def search_sss_locally(sync, corrected: np.ndarray, fs: float, scs: float,
                       refined_pss_start: int, refined_bin_offset: int, nid2: int,
                       short_cp: int, timing_radius: int, bin_radius: int):
    """Vectorized search around the deterministic PSS-to-SSS position.

    Earlier revisions performed one 336-sequence matrix-vector multiplication
    for every timing/bin hypothesis.  This version stacks all observed SSS
    vectors and evaluates the complete hypothesis set with one matrix-matrix
    multiplication, which is substantially faster for raster scanning.
    """
    nfft = sync.nfft_from_rate(fs, scs)
    stride = nfft + int(short_cp)
    expected_sss_start = int(refined_pss_start) + 2 * stride

    observed_vectors = []
    metadata = []
    for delta in range(-int(timing_radius), int(timing_radius) + 1):
        sss_start = expected_sss_start + delta
        if sss_start < 0 or sss_start + nfft > len(corrected):
            continue
        freq = sync.extract_ofdm_fft(corrected, sss_start, nfft)
        for off in range(int(refined_bin_offset) - int(bin_radius),
                         int(refined_bin_offset) + int(bin_radius) + 1):
            center = nfft // 2 + int(off)
            left = center - 120 + 56
            right = center - 120 + 183
            if left < 0 or right > nfft:
                continue
            observed_vectors.append(np.asarray(freq[left:right], dtype=np.complex64))
            metadata.append((int(sss_start), int(delta), int(off)))

    if not observed_vectors:
        raise RuntimeError('Local SSS timing search found no valid candidate.')

    observed = np.stack(observed_vectors, axis=0).astype(np.complex64)
    observed_norms = np.linalg.norm(observed, axis=1) + 1e-12
    bank, bank_norms = _sss_bank(sync.__name__, int(nid2))

    # Shape: [336 possible N_ID1, number of timing/bin hypotheses].
    corr = np.abs(bank.conj() @ observed.T)
    corr /= bank_norms[:, None] * observed_norms[None, :]

    best_nid1_per_hyp = np.argmax(corr, axis=0)
    best_corr_per_hyp = corr[best_nid1_per_hyp, np.arange(corr.shape[1])]
    best_hyp = int(np.argmax(best_corr_per_hyp))
    best_nid1 = int(best_nid1_per_hyp[best_hyp])
    best_corr = float(best_corr_per_hyp[best_hyp])

    column = corr[:, best_hyp]
    if len(column) >= 2:
        runner = float(np.partition(column, -2)[-2])
    else:
        runner = 0.0
    sss_start, delta, off = metadata[best_hyp]

    return {
        'nid1': best_nid1,
        'nid2': int(nid2),
        'pci': int(3 * best_nid1 + int(nid2)),
        'sss_corr': best_corr,
        'runner_up_corr': runner,
        'peak_ratio': float(best_corr / (runner + 1e-12)),
        'sss_useful_start': int(sss_start),
        'sss_timing_delta': int(delta),
        'ssb_bin_offset': int(off),
        'stride': int(stride),
    }

def _process_detection(
    step1,
    sync,
    det_iq: np.ndarray,
    args,
    det,
    expected_pci: Optional[int],
) -> Tuple[Optional[SyncResult], str]:
    """Run fine synchronization and SSS recovery for one PSS candidate.

    ``expected_pci=None`` enables discovery mode and accepts any recovered PCI
    that satisfies the configured PSS/SSS/CP quality gates.
    """
    if expected_pci is not None and int(det.nid2) != int(expected_pci) % 3:
        return None, (
            f'wrong N_ID2={det.nid2}; expected={int(expected_pci) % 3}; '
            f'peak/median={det.peak_over_median:.1f}'
        )
    if float(det.peak_over_median) < float(args.min_pss_peak_over_median):
        return None, (
            f'weak PSS peak/median={det.peak_over_median:.1f}; '
            f'minimum={args.min_pss_peak_over_median:.1f}'
        )

    fs = float(args.detector_rate)
    scs = float(args.scs)
    nfft = sync.nfft_from_rate(fs, scs)
    short_cp, _long_cp = sync.normal_cp_lengths_samples(fs, scs)

    coarse_useful_start = int(det.sample_index_7p68) - (nfft - 1)
    refined_pre = sync.refine_pss_local(
        iq=det_iq,
        nfft=nfft,
        coarse_start=coarse_useful_start,
        coarse_offset=0,
        coarse_nid2=int(det.nid2),
        timing_radius=int(args.pss_timing_radius),
        bin_radius=int(args.ssb_bin_radius),
    )

    cp_est = estimate_pss_cp(
        sync=sync,
        iq=det_iq,
        useful_start=int(refined_pre.useful_start),
        nfft=nfft,
        fs=fs,
        short_cp=short_cp,
        radius=int(args.cp_timing_radius),
    )
    corrected = sync.apply_cfo_correction(det_iq, fs, cp_est.cfo_hz)

    refined_post = sync.refine_pss_local(
        iq=corrected,
        nfft=nfft,
        coarse_start=int(cp_est.useful_start),
        coarse_offset=int(refined_pre.ssb_bin_offset),
        coarse_nid2=int(refined_pre.nid2),
        timing_radius=int(args.cp_timing_radius),
        bin_radius=2,
    )

    sss_best = search_sss_locally(
        sync=sync,
        corrected=corrected,
        fs=fs,
        scs=scs,
        refined_pss_start=int(refined_post.useful_start),
        refined_bin_offset=int(refined_post.ssb_bin_offset),
        nid2=int(refined_post.nid2),
        short_cp=short_cp,
        timing_radius=int(args.sss_timing_radius),
        bin_radius=int(args.sss_bin_radius),
    )

    stride = int(sss_best['stride'])
    pss_start_for_grid = int(sss_best['sss_useful_start']) - 2 * stride
    grid = extract_grid_deterministic(
        sync=sync,
        iq=corrected,
        fs=fs,
        scs=scs,
        pss_useful_start=pss_start_for_grid,
        ssb_bin_offset=int(sss_best['ssb_bin_offset']),
        stride=stride,
    )

    pss_row = grid[0, 56:183]
    pss_corr = sync.normalized_corr(sync.pss_sequence(int(refined_post.nid2)), pss_row)
    cp_post = sync.cp_correlation_at(
        corrected, pss_start_for_grid, nfft, fs, int(short_cp)
    )
    cp_metric = float(cp_post.metric) if cp_post is not None else 0.0

    reason_parts = []
    if expected_pci is not None and int(sss_best['pci']) != int(expected_pci):
        reason_parts.append(
            f"PCI mismatch recovered={sss_best['pci']} expected={expected_pci}"
        )
    if float(pss_corr) < float(args.min_fine_pss_corr):
        reason_parts.append(f"PSS={pss_corr:.3f}<{args.min_fine_pss_corr:.3f}")
    if float(sss_best['sss_corr']) < float(args.min_sss_corr):
        reason_parts.append(f"SSS={sss_best['sss_corr']:.3f}<{args.min_sss_corr:.3f}")
    if float(sss_best['peak_ratio']) < float(args.min_sss_peak_ratio):
        reason_parts.append(
            f"ratio={sss_best['peak_ratio']:.3f}<{args.min_sss_peak_ratio:.3f}"
        )
    if cp_metric < float(args.min_cp_metric):
        reason_parts.append(f"CP={cp_metric:.3f}<{args.min_cp_metric:.3f}")

    accepted = len(reason_parts) == 0
    rejection_reason = '; '.join(reason_parts) if reason_parts else 'lock'

    result = SyncResult(
        grid=np.asarray(grid, dtype=np.complex64),
        nid1=int(sss_best['nid1']),
        nid2=int(sss_best['nid2']),
        pci=int(sss_best['pci']),
        pss_corr=float(pss_corr),
        sss_corr=float(sss_best['sss_corr']),
        sss_peak_ratio=float(sss_best['peak_ratio']),
        cp_metric=float(cp_metric),
        cfo_hz=float(cp_est.cfo_hz),
        coarse_time_ms=float(det.time_ms),
        coarse_peak_over_threshold=float(det.peak_over_threshold),
        coarse_peak_over_median=float(det.peak_over_median),
        pss_useful_start=int(pss_start_for_grid),
        sss_useful_start=int(sss_best['sss_useful_start']),
        sss_timing_delta=int(sss_best['sss_timing_delta']),
        ssb_bin_offset=int(sss_best['ssb_bin_offset']),
        accepted=bool(accepted),
        rejection_reason=str(rejection_reason),
    )
    return result, rejection_reason


def process_one_burst(step1, sync, det_iq: np.ndarray, args) -> Tuple[Optional[SyncResult], str]:
    """Recover the configured PCI from multiple ranked PSS candidates.

    The original monitor evaluated only the strongest PSS peak.  That is fragile
    for a distant selected cell when a stronger co-channel cell or another SSB
    occasion dominates the window.  The tracking path now evaluates several
    clustered PSS candidates, first filtering by the selected PCI's N_ID2, and
    returns the strongest candidate that fully decodes to the configured PCI.

    ``getattr`` defaults preserve compatibility with older callers whose
    argument parsers do not yet expose the tracking-candidate controls.
    """
    expected_pci = int(args.pci)
    expected_nid2 = expected_pci % 3
    nfft = sync.nfft_from_rate(float(args.detector_rate), float(args.scs))
    max_candidates = max(1, int(getattr(args, 'tracking_max_pss_candidates', 8)))
    cluster_radius = max(
        0,
        int(getattr(args, 'tracking_pss_cluster_radius', max(64, nfft // 4))),
    )

    candidates, _, _, max_ratios = step1.detect_all(
        det_iq=det_iq,
        threshold_db=float(args.threshold_db),
        peak_window=int(args.peak_window),
        minimum_threshold=float(args.minimum_threshold),
        guard_samples=int(args.guard_samples),
        # detect_all ranks candidates across all three N_ID2 values. Request
        # three times the per-PCI budget so stronger unrelated N_ID2 peaks do
        # not crowd the selected PCI out of the returned candidate set.
        max_candidates=max_candidates * 3,
        cluster_radius_samples=cluster_radius,
        nfft=nfft,
        sample_rate=float(args.detector_rate),
    )
    if not candidates:
        return None, (
            'no PSS threshold crossing '
            f'(max corr/thr PSS0={max_ratios[0]:.3f}, '
            f'PSS1={max_ratios[1]:.3f}, PSS2={max_ratios[2]:.3f})'
        )

    eligible = [
        candidate for candidate in candidates
        if int(candidate.nid2) == expected_nid2
    ][:max_candidates]
    if not eligible:
        observed = ','.join(str(int(candidate.nid2)) for candidate in candidates[:6])
        return None, (
            f'no PSS candidate with expected N_ID2={expected_nid2}; '
            f'observed ranked N_ID2=[{observed}]'
        )

    rejection_reasons: list[str] = []
    for candidate in eligible:
        try:
            result, reason = _process_detection(
                step1=step1,
                sync=sync,
                det_iq=det_iq,
                args=args,
                det=candidate,
                expected_pci=expected_pci,
            )
        except Exception as exc:
            rejection_reasons.append(
                f'sample={candidate.sample_index_7p68}: {type(exc).__name__}: {exc}'
            )
            continue

        if result is not None and result.accepted and int(result.pci) == expected_pci:
            return result, 'lock'
        rejection_reasons.append(
            f'sample={candidate.sample_index_7p68}: {reason}'
        )

    preview = ' | '.join(rejection_reasons[:4])
    if len(rejection_reasons) > 4:
        preview += f' | ... {len(rejection_reasons) - 4} more'
    return None, (
        f'configured PCI={expected_pci} not recovered from '
        f'{len(eligible)} eligible PSS candidate(s); {preview}'
    )


def process_one_burst_any_pci(
    step1,
    sync,
    det_iq: np.ndarray,
    args,
) -> Tuple[Optional[SyncResult], str]:
    """Strongest-candidate synchronization without a configured PCI."""
    det, _, _, max_ratios = step1.detect(
        det_iq,
        threshold_db=float(args.threshold_db),
        peak_window=int(args.peak_window),
        minimum_threshold=float(args.minimum_threshold),
        guard_samples=int(args.guard_samples),
        nfft=sync.nfft_from_rate(float(args.detector_rate), float(args.scs)),
        sample_rate=float(args.detector_rate),
    )
    if det is None:
        return None, (
            'no PSS threshold crossing '
            f'(max corr/thr PSS0={max_ratios[0]:.3f}, '
            f'PSS1={max_ratios[1]:.3f}, PSS2={max_ratios[2]:.3f})'
        )
    return _process_detection(
        step1=step1,
        sync=sync,
        det_iq=det_iq,
        args=args,
        det=det,
        expected_pci=None,
    )


def process_all_candidates(
    step1,
    sync,
    det_iq: np.ndarray,
    args,
    max_candidates: int = 12,
    cluster_radius_samples: int = 64,
) -> Tuple[list[SyncResult], dict]:
    """Recover all valid PCI candidates in one detector window.

    The return list contains the strongest accepted result for each recovered
    PCI.  Repeated SSB occasions and beams with the same PCI are intentionally
    collapsed because the first milestone identifies ``(frequency, PCI)``
    combinations rather than individual SSB beams.
    """
    candidates, _corr, _threshold, max_ratios = step1.detect_all(
        det_iq=det_iq,
        threshold_db=float(args.threshold_db),
        peak_window=int(args.peak_window),
        minimum_threshold=float(args.minimum_threshold),
        guard_samples=int(args.guard_samples),
        max_candidates=int(max_candidates),
        cluster_radius_samples=int(cluster_radius_samples),
        nfft=sync.nfft_from_rate(float(args.detector_rate), float(args.scs)),
        sample_rate=float(args.detector_rate),
    )

    best_by_pci: dict[int, SyncResult] = {}
    rejected_reasons: list[str] = []
    for det in candidates:
        try:
            result, reason = _process_detection(
                step1=step1,
                sync=sync,
                det_iq=det_iq,
                args=args,
                det=det,
                expected_pci=None,
            )
        except Exception as exc:
            rejected_reasons.append(
                f'N_ID2={det.nid2} sample={det.sample_index_7p68}: {exc}'
            )
            continue

        if result is None or not result.accepted:
            rejected_reasons.append(reason)
            continue

        previous = best_by_pci.get(int(result.pci))
        rank = (
            float(result.sss_corr),
            float(result.pss_corr),
            float(result.coarse_peak_over_median),
            float(result.cp_metric),
        )
        if previous is None:
            best_by_pci[int(result.pci)] = result
        else:
            previous_rank = (
                float(previous.sss_corr),
                float(previous.pss_corr),
                float(previous.coarse_peak_over_median),
                float(previous.cp_metric),
            )
            if rank > previous_rank:
                best_by_pci[int(result.pci)] = result

    results = sorted(
        best_by_pci.values(),
        key=lambda item: (
            item.sss_corr,
            item.pss_corr,
            item.coarse_peak_over_median,
        ),
        reverse=True,
    )
    diagnostics = {
        'pss_candidates': len(candidates),
        'accepted_pcis': len(results),
        'max_ratios': np.asarray(max_ratios, dtype=np.float64),
        'rejected_reasons': rejected_reasons,
    }
    return results, diagnostics

def setup_plot():
    """Configure and return plot."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    plt.ion()
    fig, (ax_spec, ax_grid) = plt.subplots(2, 1, figsize=(13, 8))

    freq_mhz = np.linspace(-3.84, 3.84, 256, endpoint=False)
    spec_line, = ax_spec.plot(freq_mhz, np.zeros(256))
    ax_spec.set_xlim(-3.84, 3.84)
    ax_spec.set_ylim(-50, 5)
    ax_spec.set_title('SSB-Centered Spectrum — Fixed Frequency Axis')
    ax_spec.set_xlabel('Frequency relative to SSB center [MHz]')
    ax_spec.set_ylabel('Relative magnitude [dB]')
    ax_spec.grid(True)

    blank = np.zeros((4, 240), dtype=np.float32)
    image = ax_grid.imshow(blank, aspect='auto', origin='lower',
                           extent=[0, 239, -0.5, 3.5], vmin=-40, vmax=0)
    fig.colorbar(image, ax=ax_grid, label='Relative magnitude [dB]')
    ax_grid.add_patch(Rectangle((56, -0.5), 127, 1.0, fill=False,
                                linewidth=2, label='PSS: symbol 0, k=56..182'))
    ax_grid.add_patch(Rectangle((56, 1.5), 127, 1.0, fill=False,
                                linewidth=2, linestyle='--',
                                label='SSS: symbol 2, k=56..182'))
    ax_grid.set_title('Recovered SS/PBCH Grid — waiting for PCI=1 lock')
    ax_grid.set_xlabel('SS/PBCH-block subcarrier index')
    ax_grid.set_ylabel('SS/PBCH-block OFDM symbol index')
    ax_grid.set_yticks([0, 1, 2, 3])
    ax_grid.legend(loc='upper right')

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.canvas.draw()
    fig.canvas.flush_events()
    return plt, fig, ax_spec, spec_line, ax_grid, image


def update_plot(state, det_iq: np.ndarray, result: Optional[SyncResult],
                burst: int, status: str):
    """Update plot with the newest state."""
    plt, fig, ax_spec, spec_line, ax_grid, image = state
    nfft = 256
    usable = len(det_iq) // nfft * nfft
    blocks = det_iq[:usable].reshape(-1, nfft)
    blocks = blocks[-min(32, len(blocks)):]
    psd = np.mean(np.abs(np.fft.fftshift(np.fft.fft(blocks, axis=1), axes=1)) ** 2, axis=0)
    psd_db = 10 * np.log10(psd + 1e-12)
    psd_db -= np.max(psd_db)
    spec_line.set_ydata(psd_db)

    if result is None:
        fig.suptitle(f'OCUDU Step 2 v5 Continuous-Multicore | burst={burst} | searching')
        ax_grid.set_title(f'Recovered SS/PBCH Grid — {status}')
    else:
        grid_db = 20 * np.log10(np.abs(result.grid) + 1e-12)
        grid_db -= np.max(grid_db)
        image.set_data(grid_db)
        state_text = 'LOCK' if result.accepted else 'CANDIDATE'
        fig.suptitle(
            f'OCUDU Step 2 v5 Continuous-Multicore | burst={burst} | {state_text} | '
            f'PCI={result.pci} | N_ID1={result.nid1} | N_ID2={result.nid2} | '
            f'CFO={result.cfo_hz:.1f} Hz'
        )
        ax_grid.set_title(
            f'4 x 240 SS/PBCH Grid | PSS={result.pss_corr:.3f} | '
            f'SSS={result.sss_corr:.3f} | ratio={result.sss_peak_ratio:.3f} | '
            f'CP={result.cp_metric:.3f} | SSS delta={result.sss_timing_delta}'
        )
    fig.canvas.draw_idle()
    fig.canvas.flush_events()
    plt.pause(0.04)


def build_synthetic_ssb(sync, nid1=0, nid2=1, nfft=256, cp=18, cfo_hz=850.0,
                        fs=7.68e6, insertion=3000, total=12000):
    """Build and return synthetic ssb."""
    rng = np.random.default_rng(2026)
    grid = np.zeros((4, 240), dtype=np.complex64)
    grid[0, 56:183] = sync.pss_sequence(nid2)
    grid[2, 56:183] = sync.sss_sequence(nid1, nid2)
    # Populate PBCH-bearing regions with deterministic low-amplitude QPSK-like values.
    for row in (1, 3):
        vals = rng.choice([1+1j, 1-1j, -1+1j, -1-1j], size=240).astype(np.complex64)
        grid[row] = 0.35 * vals
    grid[2, :48] = 0.35 * rng.choice([1+1j, 1-1j, -1+1j, -1-1j], size=48)
    grid[2, 192:] = 0.35 * rng.choice([1+1j, 1-1j, -1+1j, -1-1j], size=48)

    x = (0.01 * rng.standard_normal(total) + 1j * 0.01 * rng.standard_normal(total)).astype(np.complex64)
    stride = nfft + cp
    for k in range(4):
        freq = np.zeros(nfft, dtype=np.complex64)
        freq[nfft//2-120:nfft//2+120] = grid[k]
        useful = np.fft.ifft(np.fft.ifftshift(freq)).astype(np.complex64)
        symbol = np.concatenate([useful[-cp:], useful])
        start = insertion + k * stride
        x[start:start+len(symbol)] += 12.0 * symbol
    n = np.arange(total)
    x *= np.exp(1j * 2 * np.pi * cfo_hz * n / fs).astype(np.complex64)
    return x


def synthetic_self_test(step1, sync) -> int:
    """Run hardware-free regression checks for this processing component."""
    args = SimpleNamespace(
        threshold_db=-14.0,
        peak_window=11,
        minimum_threshold=1e-8,
        guard_samples=256,
        min_pss_peak_over_median=20.0,
        pci=1,
        detector_rate=7.68e6,
        scs=30e3,
        pss_timing_radius=16,
        ssb_bin_radius=3,
        cp_timing_radius=8,
        sss_timing_radius=12,
        sss_bin_radius=3,
        min_fine_pss_corr=0.50,
        min_sss_corr=0.50,
        min_sss_peak_ratio=1.20,
        min_cp_metric=0.10,
    )
    det_iq = build_synthetic_ssb(sync)
    result, reason = process_one_burst(step1, sync, det_iq, args)
    print('[SELF-TEST] Step 2 v2 synthetic SSB')
    if result is None:
        print(f'  FAIL: {reason}')
        return 1
    print(f'  PCI:             {result.pci}')
    print(f'  N_ID1:           {result.nid1}')
    print(f'  N_ID2:           {result.nid2}')
    print(f'  PSS correlation: {result.pss_corr:.6f}')
    print(f'  SSS correlation: {result.sss_corr:.6f}')
    print(f'  SSS peak ratio:  {result.sss_peak_ratio:.6f}')
    print(f'  CP metric:       {result.cp_metric:.6f}')
    print(f'  CFO estimate:    {result.cfo_hz:.3f} Hz')
    print(f'  Accepted:        {result.accepted}')
    if result.pci != 1 or result.nid1 != 0 or result.nid2 != 1:
        print('[SELF-TEST] FAIL')
        return 1
    print('[SELF-TEST] PASS')
    return 0




class ContinuousX410Receiver:
    """One continuous UHD stream for X410 or N310.

    The historical class name is retained for compatibility with existing scripts.
    """

    def __init__(self, step1, device_args: str, rate: float, freq: float,
                 gain: float, antenna: str, channel: int, settle_time: float,
                 window_samples: int, ring_windows: int = 3):
        """Initialize the ContinuousX410Receiver instance and its runtime state."""
        self.uhd = step1.uhd
        if self.uhd is None:
            raise RuntimeError("UHD Python module not available.")

        self.channel = int(channel)
        self.window_samples = int(window_samples)
        self.capacity = int(max(2, ring_windows) * self.window_samples)
        self.ring = np.zeros(self.capacity, dtype=np.complex64)
        self.write_pos = 0
        self.total_samples = 0
        self.generation = 0
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.error = None

        self.usrp = open_multi_usrp(self.uhd, device_args)
        configure_rx_channel(self.usrp, self.uhd, self.channel, rate, freq, gain, antenna)
        time.sleep(float(settle_time))

        self.actual_rate = float(self.usrp.get_rx_rate(self.channel))
        self.actual_freq = float(self.usrp.get_rx_freq(self.channel))
        self.actual_gain = float(self.usrp.get_rx_gain(self.channel))
        self.actual_antenna = self.usrp.get_rx_antenna(self.channel)

        stream_args = self.uhd.usrp.StreamArgs("fc32", "sc16")
        stream_args.channels = [self.channel]
        self.streamer = self.usrp.get_rx_stream(stream_args)
        self.md = self.uhd.types.RXMetadata()
        self.max_packet = int(self.streamer.get_max_num_samps())
        self.buff = np.zeros((1, self.max_packet), dtype=np.complex64)

        self.thread = threading.Thread(
            target=self._capture_loop,
            name="usrp-continuous-rx",
            daemon=True,
        )

    def start(self):
        """Start the component and allocate the required runtime resources."""
        cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.start_cont)
        cmd.stream_now = True
        self.streamer.issue_stream_cmd(cmd)
        self.thread.start()

    def _append(self, samples):
        """Internal helper for append."""
        samples = np.asarray(samples, dtype=np.complex64)
        n = len(samples)
        if n >= self.capacity:
            samples = samples[-self.capacity:]
            n = len(samples)

        with self.lock:
            first = min(n, self.capacity - self.write_pos)
            self.ring[self.write_pos:self.write_pos + first] = samples[:first]
            rest = n - first
            if rest:
                self.ring[:rest] = samples[first:]
            self.write_pos = (self.write_pos + n) % self.capacity
            self.total_samples += n
            self.generation += 1
            if self.total_samples >= self.window_samples:
                self.ready.set()

    def _capture_loop(self):
        """Internal helper for capture loop."""
        try:
            while not self.stop_event.is_set():
                got = int(self.streamer.recv(self.buff, self.md, 0.25))
                if self.md.error_code != self.uhd.types.RXMetadataErrorCode.none:
                    if self.stop_event.is_set():
                        break
                    print(f"[WARN] RX metadata: {self.md.strerror()}", file=sys.stderr)
                    if got <= 0:
                        continue
                if got > 0:
                    self._append(self.buff[0, :got])
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def latest_window(self, last_generation: int, timeout_s: float = 1.0):
        """Handle the latest window operation for this OCUDU processing stage."""
        if not self.ready.wait(timeout=float(timeout_s)):
            if self.error is not None:
                raise RuntimeError(f"Continuous capture failed: {self.error}")
            return None, last_generation

        with self.lock:
            generation = int(self.generation)
            if generation == int(last_generation) or self.total_samples < self.window_samples:
                return None, last_generation

            end = int(self.write_pos)
            start = (end - self.window_samples) % self.capacity
            if start < end:
                out = self.ring[start:end].copy()
            else:
                out = np.concatenate((self.ring[start:], self.ring[:end])).copy()
            return out, generation

    def stop(self):
        """Stop the component and release its runtime resources."""
        self.stop_event.set()
        try:
            cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.stop_cont)
            self.streamer.issue_stream_cmd(cmd)
        except Exception:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)


def prepare_direct_ssb_iq(raw: np.ndarray, rx_rate: float,
                          detector_rate: float) -> np.ndarray:
    """Prepare an SSB-centered detector stream.

    The USRP is tuned directly to the SSB center. When UHD realizes exactly
    7.68 MS/s, no resampling is needed. If UHD returns another supported rate,
    use a small rational resampler as a compatibility fallback.
    """
    x = np.asarray(raw, dtype=np.complex64)
    x = x - np.mean(x)

    if abs(float(rx_rate) - float(detector_rate)) <= 1.0:
        y = x
    else:
        ratio = Fraction(float(detector_rate) / float(rx_rate)).limit_denominator(512)
        y = resample_poly(x, up=ratio.numerator, down=ratio.denominator)

    y = np.asarray(y, dtype=np.complex64)
    y -= np.mean(y)
    y /= np.sqrt(np.mean(np.abs(y) ** 2)) + 1e-12
    return y.astype(np.complex64)

def main():
    """Run the command-line entry point and return its process exit status."""
    ap = argparse.ArgumentParser(description='OCUDU Step 2 v5 Continuous-Multicore: low-latency deterministic SSB extraction and local SSS recovery')
    ap.add_argument('--step1-helper', default='nr_pss.py')
    ap.add_argument('--sync-helper', default='nr_sync.py')
    ap.add_argument('--device-args', default=DEFAULT_DEVICE_ARGS,
                    help='UHD device args; type is auto-detected so X410 and N310 are both supported.')
    ap.add_argument('--dl-arfcn', type=int, default=632628)
    ap.add_argument('--ssb-arfcn', type=int, default=630912)
    ap.add_argument('--pci', type=int, default=1)
    ap.add_argument('--rate', type=float, default=7.68e6,
                    help='USRP synchronization RX rate. Default: 7.68e6 MS/s at direct SSB tuning.')
    ap.add_argument('--detector-rate', type=float, default=7.68e6)
    ap.add_argument('--scs', type=float, default=30e3)
    ap.add_argument('--gain', type=float, default=30.0)
    ap.add_argument('--antenna', default='auto',
                    help='RX antenna name, or auto to keep the connected USRP default.')
    ap.add_argument('--channel', type=int, default=0)
    ap.add_argument('--burst-ms', type=float, default=12.0,
                    help='Finite SSB-centered capture duration. Default: 12 ms, above the OCUDU 10 ms SSB period.')
    ap.add_argument('--settle-time', type=float, default=0.5)
    ap.add_argument('--threshold-db', type=float, default=-14.0)
    ap.add_argument('--peak-window', type=int, default=11)
    ap.add_argument('--minimum-threshold', type=float, default=1e-8)
    ap.add_argument('--guard-samples', type=int, default=512)
    ap.add_argument('--min-pss-peak-over-median', type=float, default=100.0)
    ap.add_argument('--pss-timing-radius', type=int, default=16)
    ap.add_argument('--ssb-bin-radius', type=int, default=4)
    ap.add_argument('--cp-timing-radius', type=int, default=8)
    ap.add_argument('--sss-timing-radius', type=int, default=32)
    ap.add_argument('--sss-bin-radius', type=int, default=4)
    ap.add_argument('--min-fine-pss-corr', type=float, default=0.50)
    ap.add_argument('--min-sss-corr', type=float, default=0.45)
    ap.add_argument('--min-sss-peak-ratio', type=float, default=1.25)
    ap.add_argument('--min-cp-metric', type=float, default=0.10)
    ap.add_argument('--no-plot', action='store_true')
    ap.add_argument('--pause-s', type=float, default=0.001)
    ap.add_argument('--plot-every', type=int, default=3, help='Refresh GUI every N accepted locks. Default: 3.')
    ap.add_argument('--print-every', type=int, default=10, help='Print LOCK status every N accepted locks. Default: 10.')
    ap.add_argument('--quiet-search', action='store_true', help='Suppress periodic SEARCH/CANDIDATE output.')
    ap.add_argument('--ring-windows', type=int, default=3,
                    help='Number of detector windows retained in the rolling buffer. Default: 3.')
    ap.add_argument('--snapshot-timeout-s', type=float, default=1.0,
                    help='Maximum wait for fresh samples from the continuous stream.')
    ap.add_argument('--print-rejects', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()

    step1 = load_module(args.step1_helper, 'ocudu_step1_helper_multicore')
    sync = load_module(args.sync_helper, 'ocudu_sync_helper_v2')
    if args.self_test:
        raise SystemExit(synthetic_self_test(step1, sync))

    dl_freq = step1.nr_arfcn_to_freq_hz(args.dl_arfcn)
    ssb_freq = step1.nr_arfcn_to_freq_hz(args.ssb_arfcn)
    ssb_offset = ssb_freq - dl_freq

    # Start one continuous USRP SSB-centered stream.
    window_samples = int(round(float(args.burst_ms) * 1e-3 * float(args.rate)))
    rx = ContinuousX410Receiver(
        step1=step1,
        device_args=args.device_args,
        rate=float(args.rate),
        freq=ssb_freq,
        gain=float(args.gain),
        antenna=args.antenna,
        channel=int(args.channel),
        settle_time=float(args.settle_time),
        window_samples=window_samples,
        ring_windows=int(args.ring_windows),
    )
    if float(rx.actual_rate) <= 0:
        raise ValueError('UHD returned an invalid RX rate.')
    rx.start()

    short_cp, long_cp = sync.normal_cp_lengths_samples(args.detector_rate, args.scs)
    print('========== OCUDU Step 2 v5 Continuous-Multicore: OTA SSS / PCI Synchronization ==========')
    print(f'DL center frequency:     {dl_freq/1e6:.6f} MHz')
    print(f'SSB center frequency:    {ssb_freq/1e6:.6f} MHz')
    print(f'SSB offset from DL:      {ssb_offset/1e6:.6f} MHz')
    print(f'Connected USRP:          {device_label(rx.usrp, rx.channel)}')
    print('USRP tuning mode:        direct SSB-center tuning')
    print('UHD stream mode:         continuous start_cont')
    print('Window policy:           newest complete window only; stale windows dropped')
    print(f'Expected PCI:            {args.pci}')
    print(f'Expected N_ID2:          {args.pci % 3}')
    print(f'USRP actual RX rate:     {rx.actual_rate/1e6:.6f} MS/s')
    print(f'Detector target rate:    {args.detector_rate/1e6:.6f} MS/s')
    print(f'Detector NFFT:           {sync.nfft_from_rate(args.detector_rate, args.scs)}')
    print(f'Detector CP lengths:     short={short_cp}, long={long_cp} samples')
    print(f'Case-B SSB stride:       {sync.nfft_from_rate(args.detector_rate,args.scs)+short_cp} samples')
    print(f'PSS threshold:           {args.threshold_db:.2f} dB')
    print('Fixed spectrum center:   SSB center = 3463.680 MHz')
    print(f'Burst duration:          {args.burst_ms:.3f} ms')
    print(f'GUI refresh:             every {args.plot_every} accepted locks')
    print('PSS correlators:         3 parallel persistent workers')
    print('SSS scoring:             vectorized 336-candidate operation')
    print(f'Terminal LOCK print:     every {args.print_every} accepted locks')
    print('Disk output:             disabled')
    print('Press Ctrl+C to stop.')

    plot_state = None if args.no_plot else setup_plot()
    burst = 0
    locks = 0
    rejected = 0
    last_gui_lock = 0
    last_generation = -1
    skipped_waits = 0
    try:
        while True:
            raw, generation = rx.latest_window(
                last_generation=last_generation,
                timeout_s=float(args.snapshot_timeout_s),
            )
            if raw is None:
                skipped_waits += 1
                if not args.quiet_search and skipped_waits % 10 == 0:
                    print(f'[WAIT] no fresh complete window yet; waits={skipped_waits}')
                continue

            last_generation = generation
            burst += 1
            det_iq = prepare_direct_ssb_iq(
                raw=raw,
                rx_rate=float(rx.actual_rate),
                detector_rate=float(args.detector_rate),
            )
            try:
                result, status = process_one_burst(step1, sync, det_iq, args)
            except Exception as exc:
                result = None
                status = f'sync exception: {exc}'

            if result is None:
                rejected += 1
                if not args.quiet_search and (args.print_rejects or burst % 25 == 0):
                    print(f'[SEARCH] burst={burst} rejected={rejected} reason={status}')
            elif result.accepted:
                locks += 1
                if locks == 1 or locks % max(1, int(args.print_every)) == 0:
                    print(
                        '[LOCK] '
                        f'burst={burst} locks={locks} PCI={result.pci} '
                        f'N_ID1={result.nid1} N_ID2={result.nid2} '
                        f'PSS={result.pss_corr:.3f} SSS={result.sss_corr:.3f} '
                        f'ratio={result.sss_peak_ratio:.3f} CP={result.cp_metric:.3f} '
                        f'CFO={result.cfo_hz:.1f}Hz SSS_delta={result.sss_timing_delta} '
                        f'bin={result.ssb_bin_offset}'
                    )
            else:
                rejected += 1
                if not args.quiet_search and (args.print_rejects or burst % 25 == 0):
                    print(
                        '[CANDIDATE] '
                        f'burst={burst} rejected={rejected} PCI={result.pci} '
                        f'N_ID1={result.nid1} N_ID2={result.nid2} '
                        f'PSS={result.pss_corr:.3f} SSS={result.sss_corr:.3f} '
                        f'ratio={result.sss_peak_ratio:.3f} CP={result.cp_metric:.3f} '
                        f'SSS_delta={result.sss_timing_delta} bin={result.ssb_bin_offset} '
                        f'reason={status}'
                    )

            if plot_state is not None:
                should_refresh = (
                    result is not None and result.accepted and
                    (locks == 1 or locks - last_gui_lock >= max(1, int(args.plot_every)))
                )
                if should_refresh:
                    update_plot(plot_state, det_iq, result, burst, status)
                    last_gui_lock = locks
                else:
                    plot_state[0].pause(float(args.pause_s))
            else:
                time.sleep(float(args.pause_s))
    except KeyboardInterrupt:
        print('\n[INFO] Ctrl+C received. Stopping Step 2 v5 Continuous-Multicore...')
    finally:
        try:
            rx.stop()
        except Exception:
            pass
        if plot_state is not None:
            try:
                plot_state[0].ioff()
                plot_state[0].close(plot_state[1])
            except Exception:
                pass
        print('[INFO] Step 2 v5 Continuous-Multicore stopped. No files were written.')


if __name__ == '__main__':
    main()

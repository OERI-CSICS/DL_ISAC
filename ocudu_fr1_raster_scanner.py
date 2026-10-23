#!/usr/bin/env python3
"""OCUDU FR1 synchronization-raster scanner and ISAC monitor launcher.

First milestone
---------------
* Scan a configured list of FR1 GSCN synchronization-raster positions.
* Recover every valid PCI candidate found in each capture window.
* Aggregate and rank unique (SSB frequency, SCS, PCI) combinations.
* Allow the operator to select a discovered cell and hand it to the existing
  four-channel OCUDU downlink ISAC monitor.
* Optionally annotate discovered cells with MCC, MNC, PLMN, and operator name
  from a user-maintained local mapping file.

The scanner is passive and receive-only.  Use only in authorized environments.
It decodes PSS/SSS. Operator metadata in this milestone is explicitly marked as
MANUAL_MAP; over-the-air MCC/MNC recovery still requires PBCH/MIB/SIB1 decoding.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import shlex
import signal
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional

import numpy as np

import isac_core as default_core
import nr_pss as default_step1
import nr_cell_sync as default_step2
import nr_sync as default_sync
from usrp_io import DEFAULT_DEVICE_ARGS, configure_rx_channel, device_label, open_multi_usrp

SCRIPT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# FR1 synchronization-raster utilities (kept local to avoid another runtime file)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class RasterTarget:
    gscn: int
    ssb_frequency_hz: float
    scs_hz: float
    label: str = ""

    @property
    def scs_khz(self) -> float:
        return self.scs_hz / 1e3


def gscn_to_frequency_hz(gscn: int) -> float:
    """Convert global GSCN to SS_REF according to the 3GPP global raster."""
    g = int(gscn)
    if 2 <= g <= 7498:
        remainder = g % 3
        if remainder == 0:
            n, m = g // 3, 3
        elif remainder == 1:
            n, m = (g - 1) // 3, 5
        else:
            n, m = (g + 1) // 3, 1
        if not 1 <= n <= 2499:
            raise ValueError(f"GSCN {g} maps outside the lower-raster N range")
        return float(n * 1.2e6 + m * 50e3)
    if 7499 <= g <= 22255:
        return float(3.0e9 + (g - 7499) * 1.44e6)
    if 22256 <= g <= 26639:
        return float(24_250.08e6 + (g - 22256) * 17.28e6)
    raise ValueError(f"Unsupported GSCN: {g}")


def frequency_hz_to_nr_arfcn(freq_hz: float) -> int:
    f_mhz = float(freq_hz) / 1e6
    if 0.0 <= f_mhz < 3000.0:
        n = round(f_mhz / 0.005)
        reconstructed = 0.005 * n
    elif 3000.0 <= f_mhz < 24_250.08:
        n = 600000 + round((f_mhz - 3000.0) / 0.015)
        reconstructed = 3000.0 + 0.015 * (n - 600000)
    elif 24_250.08 <= f_mhz <= 100000.0:
        n = 2016667 + round((f_mhz - 24_250.08) / 0.06)
        reconstructed = 24_250.08 + 0.06 * (n - 2016667)
    else:
        raise ValueError(f"Frequency outside NR global raster: {f_mhz:.6f} MHz")
    if abs(reconstructed - f_mhz) > 0.0026:
        raise ValueError(f"Frequency {f_mhz:.6f} MHz is not representable on NR-ARFCN raster")
    return int(n)


def parse_gscn_spec(spec: str) -> list[int]:
    values, seen = [], set()
    for raw_token in str(spec).split(','):
        token = raw_token.strip()
        if not token:
            continue
        fields = token.split(':')
        if len(fields) == 1:
            expanded = [int(fields[0])]
        elif len(fields) in (2, 3):
            first, last = int(fields[0]), int(fields[1])
            step = int(fields[2]) if len(fields) == 3 else 1
            if step <= 0:
                raise ValueError(f"GSCN range step must be positive: {token}")
            direction = 1 if last >= first else -1
            expanded = list(range(first, last + direction, direction * step))
        else:
            raise ValueError(f"Invalid GSCN token: {token}")
        for gscn in expanded:
            gscn_to_frequency_hz(gscn)
            if gscn not in seen:
                values.append(gscn)
                seen.add(gscn)
    if not values:
        raise ValueError("The GSCN scan specification is empty")
    return values


def build_raster_targets(gscns, scs_hz: float, min_frequency_hz=None,
                         max_frequency_hz=None, label: str = "custom") -> list[RasterTarget]:
    targets = []
    for gscn in gscns:
        freq = gscn_to_frequency_hz(int(gscn))
        if min_frequency_hz is not None and freq < float(min_frequency_hz):
            continue
        if max_frequency_hz is not None and freq > float(max_frequency_hz):
            continue
        targets.append(RasterTarget(int(gscn), float(freq), float(scs_hz), str(label)))
    return targets


def load_scan_plan_csv(path: str | Path, min_frequency_hz=None,
                       max_frequency_hz=None) -> list[RasterTarget]:
    plan_path = Path(path).expanduser().resolve()
    targets = []
    with plan_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = {"gscn", "scs_khz"}.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Scan plan {plan_path} is missing columns: {sorted(missing)}")
        for row_number, row in enumerate(reader, start=2):
            if str(row.get("enabled", "1")).strip().lower() in {"0", "false", "no", "off"}:
                continue
            try:
                gscn = int(str(row["gscn"]).strip())
                scs_hz = float(str(row["scs_khz"]).strip()) * 1e3
                freq = gscn_to_frequency_hz(gscn)
            except Exception as exc:
                raise ValueError(f"Invalid scan-plan row {row_number} in {plan_path}: {exc}") from exc
            if min_frequency_hz is not None and freq < float(min_frequency_hz):
                continue
            if max_frequency_hz is not None and freq > float(max_frequency_hz):
                continue
            targets.append(RasterTarget(gscn, freq, scs_hz, str(row.get("label", "")).strip()))
    unique, seen = [], set()
    for target in targets:
        key = (target.gscn, int(round(target.scs_hz)))
        if key not in seen:
            unique.append(target)
            seen.add(key)
    if not unique:
        raise ValueError(f"No enabled FR1 targets were loaded from {plan_path}")
    return unique


def raster_self_test() -> None:
    assert abs(gscn_to_frequency_hz(7821) - 3_463.68e6) < 1.0
    assert parse_gscn_spec("7821,7823:7827:2") == [7821, 7823, 7825, 7827]
    assert frequency_hz_to_nr_arfcn(3_463.68e6) == 630912
    assert 0.0 < gscn_to_frequency_hz(5279) < 3.0e9
    print("[RASTER-SELF-TEST] PASS")


def utc_now() -> str:
    """Handle the utc now operation for this OCUDU processing stage."""
    return datetime.now(timezone.utc).isoformat()


def discovery_quality(result) -> float:
    """Return a transparent 0..100 synchronization-quality score.

    This is a discovery ranking, not standardized RSRP/RSRQ/SINR.  It combines
    PSS/SSS normalized correlation, PSS prominence over the matched-filter
    median, SSS winner-to-runner-up ratio, and cyclic-prefix consistency.
    """
    corr = np.clip(0.5 * (float(result.pss_corr) + float(result.sss_corr)), 0.0, 1.0)
    prominence_db = 10.0 * math.log10(max(float(result.coarse_peak_over_median), 1.0))
    prominence = np.clip((prominence_db - 10.0) / 30.0, 0.0, 1.0)
    ratio = np.clip((float(result.sss_peak_ratio) - 1.0) / 1.0, 0.0, 1.0)
    cp = np.clip(float(result.cp_metric) / 0.6, 0.0, 1.0)
    return float(100.0 * (0.55 * corr + 0.20 * prominence + 0.15 * ratio + 0.10 * cp))


def _text(value) -> str:
    """Internal helper for text."""
    return "" if value is None else str(value).strip()


def _enabled(value) -> bool:
    """Internal helper for enabled."""
    text = _text(value).lower()
    return text not in {"0", "false", "no", "off", "disabled"}


def _optional_int(value) -> Optional[int]:
    """Internal helper for optional int."""
    text = _text(value)
    return None if not text else int(text)


def _optional_float(value) -> Optional[float]:
    """Internal helper for optional float."""
    text = _text(value)
    return None if not text else float(text)


def _normalize_operator_identity(mcc: str, mnc: str, plmn: str) -> tuple[str, str, str]:
    """Internal helper for normalize operator identity."""
    mcc = _text(mcc)
    mnc = _text(mnc)
    plmn = _text(plmn)

    if plmn and (not mcc or not mnc):
        compact = plmn.replace("-", "").replace(" ", "")
        if compact.isdigit() and len(compact) in (5, 6):
            mcc = mcc or compact[:3]
            mnc = mnc or compact[3:]

    if mcc and (not mcc.isdigit() or len(mcc) != 3):
        raise ValueError(f"MCC must contain exactly three digits, got {mcc!r}")
    if mnc and (not mnc.isdigit() or len(mnc) not in (2, 3)):
        raise ValueError(f"MNC must contain two or three digits, got {mnc!r}")
    if bool(mcc) != bool(mnc):
        raise ValueError("MCC and MNC must be supplied together")
    if plmn and mcc and mnc:
        compact_plmn = plmn.replace("-", "").replace(" ", "")
        if compact_plmn != f"{mcc}{mnc}":
            raise ValueError(
                f"PLMN {plmn!r} is inconsistent with MCC={mcc} and MNC={mnc}"
            )

    canonical_plmn = f"{mcc}-{mnc}" if mcc and mnc else plmn
    return mcc, mnc, canonical_plmn


@dataclass(frozen=True)
class OperatorMetadata:
    """Represent operator metadata state and behavior used by the OCUDU pipeline."""
    mcc: str = ""
    mnc: str = ""
    plmn: str = ""
    operator_name: str = ""
    source: str = ""
    notes: str = ""

    @property
    def known(self) -> bool:
        """Handle the known operation for this OCUDU processing stage."""
        return bool(self.mcc or self.mnc or self.plmn or self.operator_name)


@dataclass(frozen=True)
class OperatorMapEntry:
    """Represent operator map entry state and behavior used by the OCUDU pipeline."""
    pci: int
    metadata: OperatorMetadata
    row_number: int
    gscn: Optional[int] = None
    ssb_frequency_hz: Optional[float] = None
    scs_hz: Optional[float] = None

    def specificity(
        self,
        *,
        gscn: int,
        ssb_frequency_hz: float,
        scs_hz: float,
        pci: int,
        frequency_tolerance_hz: float,
    ) -> int:
        """Handle the specificity operation for this OCUDU processing stage."""
        if int(pci) != int(self.pci):
            return -1
        score = 10
        if self.gscn is not None:
            if int(gscn) != int(self.gscn):
                return -1
            score += 100
        if self.ssb_frequency_hz is not None:
            if abs(float(ssb_frequency_hz) - float(self.ssb_frequency_hz)) > frequency_tolerance_hz:
                return -1
            score += 80
        if self.scs_hz is not None:
            if abs(float(scs_hz) - float(self.scs_hz)) > 1.0:
                return -1
            score += 20
        return score


class OperatorMetadataResolver:
    """Resolve operator annotations from an explicit local mapping file.

    PSS/SSS do not carry MCC/MNC. This resolver therefore never infers an
    operator from PCI or spectrum alone. It applies only mappings deliberately
    supplied by the user and records their provenance as MANUAL_MAP.
    """

    def __init__(
        self,
        entries: list[OperatorMapEntry] | None = None,
        frequency_tolerance_hz: float = 1.0,
    ) -> None:
        """Initialize the OperatorMetadataResolver instance and its runtime state."""
        self.entries = list(entries or [])
        self.frequency_tolerance_hz = float(frequency_tolerance_hz)

    @classmethod
    def from_csv(
        cls,
        path: str | Path | None,
        frequency_tolerance_hz: float = 1.0,
    ) -> "OperatorMetadataResolver":
        """Handle the from csv operation for this OCUDU processing stage."""
        if not path:
            return cls([], frequency_tolerance_hz)
        source_path = Path(path).expanduser()
        if not source_path.is_absolute() and not source_path.exists():
            source_path = SCRIPT_DIR / source_path
        source_path = source_path.resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Operator mapping file not found: {source_path}")

        entries: list[OperatorMapEntry] = []
        with source_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"Operator mapping file has no header: {source_path}")
            for row_number, row in enumerate(reader, start=2):
                if not _enabled(row.get("enabled", "1")):
                    continue
                try:
                    pci = _optional_int(row.get("pci"))
                    if pci is None or not 0 <= pci <= 1007:
                        raise ValueError("pci is required and must be in 0..1007")
                    gscn = _optional_int(row.get("gscn"))
                    frequency_hz = _optional_float(row.get("ssb_frequency_hz"))
                    frequency_mhz = _optional_float(row.get("ssb_frequency_mhz"))
                    if frequency_hz is None and frequency_mhz is not None:
                        frequency_hz = frequency_mhz * 1e6
                    scs_hz = _optional_float(row.get("scs_hz"))
                    scs_khz = _optional_float(row.get("scs_khz"))
                    if scs_hz is None and scs_khz is not None:
                        scs_hz = scs_khz * 1e3
                    if gscn is None and frequency_hz is None:
                        raise ValueError("at least one of gscn or SSB frequency is required")

                    mcc, mnc, plmn = _normalize_operator_identity(
                        row.get("mcc", ""),
                        row.get("mnc", ""),
                        row.get("plmn", ""),
                    )
                    operator_name = _text(row.get("operator_name"))
                    if not (plmn or operator_name):
                        raise ValueError("PLMN and/or operator_name is required")
                    metadata = OperatorMetadata(
                        mcc=mcc,
                        mnc=mnc,
                        plmn=plmn,
                        operator_name=operator_name,
                        source=f"MANUAL_MAP:{source_path.name}:row{row_number}",
                        notes=_text(row.get("notes")),
                    )
                    entries.append(
                        OperatorMapEntry(
                            pci=pci,
                            metadata=metadata,
                            row_number=row_number,
                            gscn=gscn,
                            ssb_frequency_hz=frequency_hz,
                            scs_hz=scs_hz,
                        )
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Invalid operator mapping at {source_path}:{row_number}: {exc}"
                    ) from exc
        print(f"[OPERATOR-MAP] Loaded {len(entries)} enabled entries from {source_path}")
        if not entries:
            print(
                "[OPERATOR-MAP][WARNING] The mapping file contains no enabled entries. "
                "Set enabled=1 for rows that match discovered GSCN/frequency and PCI values."
            )
        return cls(entries, frequency_tolerance_hz)

    def resolve(
        self,
        *,
        gscn: int,
        ssb_frequency_hz: float,
        scs_hz: float,
        pci: int,
    ) -> OperatorMetadata:
        """Resolve metadata for the supplied discovered cell identity."""
        best: Optional[OperatorMapEntry] = None
        best_score = -1
        for entry in self.entries:
            score = entry.specificity(
                gscn=gscn,
                ssb_frequency_hz=ssb_frequency_hz,
                scs_hz=scs_hz,
                pci=pci,
                frequency_tolerance_hz=self.frequency_tolerance_hz,
            )
            if score > best_score:
                best = entry
                best_score = score
        return OperatorMetadata() if best is None else best.metadata


@dataclass(frozen=True)
class CellObservation:
    """Represent cell observation state and behavior used by the OCUDU pipeline."""
    timestamp_utc: str
    gscn: int
    ssb_frequency_hz: float
    scs_hz: float
    label: str
    pci: int
    nid1: int
    nid2: int
    quality_score: float
    pss_corr: float
    sss_corr: float
    sss_peak_ratio: float
    cp_metric: float
    pss_peak_over_median: float
    pss_peak_over_threshold: float
    cfo_hz: float
    raw_rms_dbfs: float
    ssb_bin_offset: int
    mcc: str = ""
    mnc: str = ""
    plmn: str = ""
    operator_name: str = ""
    operator_source: str = ""
    operator_notes: str = ""

    @property
    def key(self) -> tuple[int, int, int]:
        """Handle the key operation for this OCUDU processing stage."""
        return (
            int(self.gscn),
            int(round(self.scs_hz)),
            int(self.pci),
        )


@dataclass
class DiscoveredCell:
    """Represent discovered cell state and behavior used by the OCUDU pipeline."""
    gscn: int
    ssb_frequency_hz: float
    scs_hz: float
    label: str
    pci: int
    nid1: int
    nid2: int
    first_seen_utc: str
    last_seen_utc: str
    hits: int = 0
    best_quality_score: float = 0.0
    mean_quality_score: float = 0.0
    pss_corr: float = 0.0
    sss_corr: float = 0.0
    sss_peak_ratio: float = 0.0
    cp_metric: float = 0.0
    pss_peak_over_median: float = 0.0
    pss_peak_over_threshold: float = 0.0
    cfo_hz: float = 0.0
    raw_rms_dbfs: float = -300.0
    ssb_bin_offset: int = 0
    mcc: str = ""
    mnc: str = ""
    plmn: str = ""
    operator_name: str = ""
    operator_source: str = ""
    operator_notes: str = ""

    @classmethod
    def from_observation(cls, obs: CellObservation) -> "DiscoveredCell":
        """Handle the from observation operation for this OCUDU processing stage."""
        return cls(
            gscn=obs.gscn,
            ssb_frequency_hz=obs.ssb_frequency_hz,
            scs_hz=obs.scs_hz,
            label=obs.label,
            pci=obs.pci,
            nid1=obs.nid1,
            nid2=obs.nid2,
            first_seen_utc=obs.timestamp_utc,
            last_seen_utc=obs.timestamp_utc,
            hits=1,
            best_quality_score=obs.quality_score,
            mean_quality_score=obs.quality_score,
            pss_corr=obs.pss_corr,
            sss_corr=obs.sss_corr,
            sss_peak_ratio=obs.sss_peak_ratio,
            cp_metric=obs.cp_metric,
            pss_peak_over_median=obs.pss_peak_over_median,
            pss_peak_over_threshold=obs.pss_peak_over_threshold,
            cfo_hz=obs.cfo_hz,
            raw_rms_dbfs=obs.raw_rms_dbfs,
            ssb_bin_offset=obs.ssb_bin_offset,
            mcc=obs.mcc,
            mnc=obs.mnc,
            plmn=obs.plmn,
            operator_name=obs.operator_name,
            operator_source=obs.operator_source,
            operator_notes=obs.operator_notes,
        )

    @property
    def key(self) -> tuple[int, int, int]:
        """Handle the key operation for this OCUDU processing stage."""
        return (self.gscn, int(round(self.scs_hz)), self.pci)

    @property
    def rank_score(self) -> float:
        # Reward persistence without allowing repeated hits to overwhelm quality.
        """Handle the rank score operation for this OCUDU processing stage."""
        hit_bonus = min(3.0, math.log2(max(1, self.hits)))
        return float(0.85 * self.best_quality_score + 0.15 * self.mean_quality_score + hit_bonus)

    @property
    def pss_prominence_db(self) -> float:
        """Handle the pss prominence db operation for this OCUDU processing stage."""
        return float(10.0 * math.log10(max(self.pss_peak_over_median, 1e-12)))

    def update(self, obs: CellObservation) -> None:
        """Update the object with the supplied state changes."""
        old_hits = self.hits
        self.hits += 1
        self.last_seen_utc = obs.timestamp_utc
        self.mean_quality_score = (
            self.mean_quality_score * old_hits + obs.quality_score
        ) / self.hits

        if obs.quality_score >= self.best_quality_score:
            self.best_quality_score = obs.quality_score
            self.nid1 = obs.nid1
            self.nid2 = obs.nid2
            self.pss_corr = obs.pss_corr
            self.sss_corr = obs.sss_corr
            self.sss_peak_ratio = obs.sss_peak_ratio
            self.cp_metric = obs.cp_metric
            self.pss_peak_over_median = obs.pss_peak_over_median
            self.pss_peak_over_threshold = obs.pss_peak_over_threshold
            self.cfo_hz = obs.cfo_hz
            self.raw_rms_dbfs = obs.raw_rms_dbfs
            self.ssb_bin_offset = obs.ssb_bin_offset

        if obs.plmn or obs.operator_name:
            self.mcc = obs.mcc
            self.mnc = obs.mnc
            self.plmn = obs.plmn
            self.operator_name = obs.operator_name
            self.operator_source = obs.operator_source
            self.operator_notes = obs.operator_notes


class CellCatalog:
    """Represent cell catalog state and behavior used by the OCUDU pipeline."""
    def __init__(self) -> None:
        """Initialize the CellCatalog instance and its runtime state."""
        self._cells: dict[tuple[int, int, int], DiscoveredCell] = {}

    def clear(self) -> None:
        """Handle the clear operation for this OCUDU processing stage."""
        self._cells.clear()

    def update(self, obs: CellObservation) -> DiscoveredCell:
        """Update the object with the supplied state changes."""
        cell = self._cells.get(obs.key)
        if cell is None:
            cell = DiscoveredCell.from_observation(obs)
            self._cells[obs.key] = cell
        else:
            cell.update(obs)
        return cell

    def ranked(self) -> list[DiscoveredCell]:
        """Handle the ranked operation for this OCUDU processing stage."""
        return sorted(
            (replace(cell) for cell in self._cells.values()),
            key=lambda c: (c.rank_score, c.hits, c.sss_corr, c.pss_corr),
            reverse=True,
        )


@dataclass
class ScanSnapshot:
    """Represent scan snapshot state and behavior used by the OCUDU pipeline."""
    sequence: int = 0
    running: bool = False
    complete: bool = False
    stopped: bool = False
    error: str = ""
    message: str = "READY"
    target_index: int = 0
    target_total: int = 0
    burst_index: int = 0
    burst_total: int = 0
    current_gscn: Optional[int] = None
    current_frequency_hz: Optional[float] = None
    current_scs_hz: Optional[float] = None
    rejected_candidates: int = 0
    cells: list[DiscoveredCell] = field(default_factory=list)


class ScanMailbox:
    """Represent scan mailbox state and behavior used by the OCUDU pipeline."""
    def __init__(self) -> None:
        """Initialize the ScanMailbox instance and its runtime state."""
        self._lock = threading.Lock()
        self._snapshot = ScanSnapshot()

    def update(self, **changes) -> None:
        """Update the object with the supplied state changes."""
        with self._lock:
            data = dict(self._snapshot.__dict__)
            data.update(changes)
            data["sequence"] = self._snapshot.sequence + 1
            cells = data.get("cells", [])
            data["cells"] = [replace(cell) for cell in cells]
            self._snapshot = ScanSnapshot(**data)

    def get(self) -> ScanSnapshot:
        """Return a thread-safe copy of the latest state."""
        with self._lock:
            data = dict(self._snapshot.__dict__)
            data["cells"] = [replace(cell) for cell in self._snapshot.cells]
            return ScanSnapshot(**data)


class RetunableX410ScannerReceiver:
    """One-channel finite-burst X410/N310 receiver reused across GSCN retunes.

    The historical class name is retained for compatibility with existing scripts.
    """

    def __init__(
        self,
        step1: ModuleType,
        device_args: str,
        rate: float,
        gain: float,
        antenna: str,
        channel: int,
        initial_frequency_hz: float,
        settle_time_s: float,
    ) -> None:
        """Initialize the RetunableX410ScannerReceiver instance and its runtime state."""
        self.uhd = step1.uhd
        if self.uhd is None:
            raise RuntimeError("UHD Python module is unavailable")
        self.channel = int(channel)
        self.usrp = open_multi_usrp(self.uhd, device_args)
        configure_rx_channel(
            self.usrp, self.uhd, self.channel, rate, initial_frequency_hz, gain, antenna
        )
        time.sleep(float(settle_time_s))

        self.actual_rate = float(self.usrp.get_rx_rate(self.channel))
        self.actual_gain = float(self.usrp.get_rx_gain(self.channel))
        self.actual_antenna = self.usrp.get_rx_antenna(self.channel)
        self.actual_frequency_hz = float(self.usrp.get_rx_freq(self.channel))

        stream_args = self.uhd.usrp.StreamArgs("fc32", "sc16")
        stream_args.channels = [self.channel]
        self.streamer = self.usrp.get_rx_stream(stream_args)
        self.metadata = self.uhd.types.RXMetadata()
        self.max_packet = int(self.streamer.get_max_num_samps())
        self.buffer = np.zeros((1, self.max_packet), dtype=np.complex64)

    def tune(self, frequency_hz: float, settle_time_s: float) -> float:
        """Handle the tune operation for this OCUDU processing stage."""
        self.usrp.set_rx_freq(
            self.uhd.types.TuneRequest(float(frequency_hz)), self.channel
        )
        time.sleep(max(0.0, float(settle_time_s)))
        self.actual_frequency_hz = float(self.usrp.get_rx_freq(self.channel))
        return self.actual_frequency_hz

    def capture(self, duration_s: float, timeout_s: float) -> np.ndarray:
        """Capture one aligned block of receive samples."""
        num_samples = int(round(float(duration_s) * self.actual_rate))
        if num_samples <= 0:
            raise ValueError("Capture duration produced no samples")
        output = np.zeros(num_samples, dtype=np.complex64)

        command = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.num_done)
        command.num_samps = num_samples
        command.stream_now = True
        self.streamer.issue_stream_cmd(command)

        total = 0
        deadline = time.monotonic() + max(float(timeout_s), float(duration_s) + 1.0)
        while total < num_samples:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Timed out after receiving {total}/{num_samples} samples"
                )
            received = int(self.streamer.recv(self.buffer, self.metadata, 0.5))
            if self.metadata.error_code != self.uhd.types.RXMetadataErrorCode.none:
                message = self.metadata.strerror()
                # Timeouts can occur during a retune; retry until the deadline.
                if received <= 0:
                    continue
                print(f"[WARN] RX metadata: {message}", file=sys.stderr)
            if received <= 0:
                continue
            count = min(received, num_samples - total)
            output[total:total + count] = self.buffer[0, :count]
            total += count
        return output


class RasterScanner:
    """Represent raster scanner state and behavior used by the OCUDU pipeline."""
    def __init__(
        self,
        args,
        targets,
        core: ModuleType,
        step1: ModuleType,
        step2: ModuleType,
        sync: ModuleType,
        mailbox: ScanMailbox,
        operator_resolver: Optional[OperatorMetadataResolver] = None,
    ) -> None:
        """Initialize the RasterScanner instance and its runtime state."""
        self.args = args
        self.targets = list(targets)
        self.core = core
        self.step1 = step1
        self.step2 = step2
        self.sync = sync
        self.mailbox = mailbox
        self.operator_resolver = operator_resolver or OperatorMetadataResolver()
        self.stop_event = threading.Event()
        self.catalog = CellCatalog()

    def request_stop(self) -> None:
        """Request a cooperative stop without blocking the caller."""
        self.stop_event.set()

    def scan(self) -> list[DiscoveredCell]:
        """Scan all configured raster targets and return ranked discoveries."""
        args = self.args
        receiver = None
        rejected_total = 0
        self.catalog.clear()
        self.mailbox.update(
            running=True,
            complete=False,
            stopped=False,
            error="",
            message="OPENING USRP",
            target_index=0,
            target_total=len(self.targets),
            burst_index=0,
            burst_total=int(args.dwell_bursts),
            rejected_candidates=0,
            cells=[],
        )

        try:
            first = self.targets[0]
            primary = int(args.primary_index)
            receiver = RetunableX410ScannerReceiver(
                step1=self.step1,
                device_args=args.device_args,
                rate=float(args.rate),
                gain=float(args.gains[primary]),
                antenna=str(args.antennas[primary]),
                channel=int(args.channels[primary]),
                initial_frequency_hz=float(first.ssb_frequency_hz),
                settle_time_s=float(args.settle_time),
            )
            print("========== OCUDU FR1 Synchronization-Raster Scanner ==========")
            print(f"Targets:                 {len(self.targets)}")
            print(f"Discovery RX channel:    {args.channels[primary]}")
            print(f"Discovery antenna:       {args.antennas[primary]}")
            print(f"Discovery gain:          {args.gains[primary]:.1f} dB")
            print(f"Actual sample rate:      {receiver.actual_rate / 1e6:.6f} MS/s")
            print(f"Bursts per GSCN:         {args.dwell_bursts}")
            print(f"Capture per burst:       {args.burst_ms:.1f} ms")

            for target_index, target in enumerate(self.targets, start=1):
                if self.stop_event.is_set():
                    break

                actual_frequency = receiver.tune(
                    target.ssb_frequency_hz,
                    float(args.retune_settle_ms) / 1e3,
                )
                args.scs = float(target.scs_hz)
                self.mailbox.update(
                    message=(
                        f"SCANNING GSCN {target.gscn} | "
                        f"{target.ssb_frequency_hz / 1e6:.6f} MHz | "
                        f"SCS {target.scs_hz / 1e3:.0f} kHz"
                    ),
                    target_index=target_index,
                    target_total=len(self.targets),
                    burst_index=0,
                    burst_total=int(args.dwell_bursts),
                    current_gscn=target.gscn,
                    current_frequency_hz=target.ssb_frequency_hz,
                    current_scs_hz=target.scs_hz,
                    cells=self.catalog.ranked(),
                )

                for burst_index in range(1, int(args.dwell_bursts) + 1):
                    if self.stop_event.is_set():
                        break
                    raw = receiver.capture(
                        duration_s=float(args.burst_ms) / 1e3,
                        timeout_s=float(args.capture_timeout_s),
                    )
                    raw_rms = float(self.core.rms_db(raw))
                    detector_iq = self.core.prepare_iq(
                        raw,
                        receiver.actual_rate,
                        float(args.detector_rate),
                        normalize_rms=True,
                    )
                    results, diagnostics = self.step2.process_all_candidates(
                        self.step1,
                        self.sync,
                        detector_iq,
                        args,
                        max_candidates=int(args.max_pss_candidates),
                        cluster_radius_samples=int(args.pss_cluster_radius),
                    )
                    rejected_total += len(diagnostics.get("rejected_reasons", []))

                    for result in results:
                        operator_metadata = self.operator_resolver.resolve(
                            gscn=int(target.gscn),
                            ssb_frequency_hz=float(target.ssb_frequency_hz),
                            scs_hz=float(target.scs_hz),
                            pci=int(result.pci),
                        )
                        observation = CellObservation(
                            timestamp_utc=utc_now(),
                            gscn=int(target.gscn),
                            ssb_frequency_hz=float(target.ssb_frequency_hz),
                            scs_hz=float(target.scs_hz),
                            label=str(target.label),
                            pci=int(result.pci),
                            nid1=int(result.nid1),
                            nid2=int(result.nid2),
                            quality_score=discovery_quality(result),
                            pss_corr=float(result.pss_corr),
                            sss_corr=float(result.sss_corr),
                            sss_peak_ratio=float(result.sss_peak_ratio),
                            cp_metric=float(result.cp_metric),
                            pss_peak_over_median=float(result.coarse_peak_over_median),
                            pss_peak_over_threshold=float(result.coarse_peak_over_threshold),
                            cfo_hz=float(result.cfo_hz),
                            raw_rms_dbfs=raw_rms,
                            ssb_bin_offset=int(result.ssb_bin_offset),
                            mcc=operator_metadata.mcc,
                            mnc=operator_metadata.mnc,
                            plmn=operator_metadata.plmn,
                            operator_name=operator_metadata.operator_name,
                            operator_source=operator_metadata.source,
                            operator_notes=operator_metadata.notes,
                        )
                        cell = self.catalog.update(observation)
                        print(
                            "[CELL] "
                            f"GSCN={cell.gscn} "
                            f"f={cell.ssb_frequency_hz / 1e6:.6f}MHz "
                            f"SCS={cell.scs_hz / 1e3:.0f}kHz PCI={cell.pci} "
                            f"quality={cell.best_quality_score:.1f} "
                            f"PSS={cell.pss_corr:.3f} SSS={cell.sss_corr:.3f} "
                            f"hits={cell.hits} "
                            f"PLMN={cell.plmn or 'Unknown'} "
                            f"operator={cell.operator_name or 'Unknown'}"
                        )

                    self.mailbox.update(
                        message=(
                            f"SCANNING.GSCN {target.gscn} | " 
                            f"{target.ssb_frequency_hz / 1e6:.6f} MHz | "
                            f"SCS {target.scs_hz / 1e3:.0f} kHz | "
                            f"burst {burst_index}/"
                            f"{args.dwell_bursts}, valid PCIs={len(results)}"
                        ),
                        target_index=target_index,
                        burst_index=burst_index,
                        current_gscn=target.gscn,
                        current_frequency_hz=actual_frequency,
                        current_scs_hz=target.scs_hz,
                        rejected_candidates=rejected_total,
                        cells=self.catalog.ranked(),
                    )

            stopped = self.stop_event.is_set()
            ranked = self.catalog.ranked()
            self.mailbox.update(
                running=False,
                complete=not stopped,
                stopped=stopped,
                message=("SCAN STOPPED" if stopped else f"SCAN COMPLETE: {len(ranked)} CELLS"),
                rejected_candidates=rejected_total,
                cells=ranked,
            )
            return ranked

        except Exception as exc:
            self.mailbox.update(
                running=False,
                complete=False,
                stopped=self.stop_event.is_set(),
                error=str(exc),
                message=f"SCAN ERROR: {exc}",
                rejected_candidates=rejected_total,
                cells=self.catalog.ranked(),
            )
            raise
        finally:
            # UHD resources are released when this worker exits.  The GUI uses
            # os.execv for monitor handoff, guaranteeing clean device ownership.
            receiver = None


def export_cells_csv(path: str | Path, cells: list[DiscoveredCell]) -> Path:
    """Export cells csv to the requested output path."""
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "rank",
            "gscn",
            "ssb_frequency_hz",
            "scs_hz",
            "label",
            "mcc",
            "mnc",
            "plmn",
            "operator_name",
            "operator_source",
            "operator_notes",
            "pci",
            "nid1",
            "nid2",
            "rank_score",
            "best_quality_score",
            "mean_quality_score",
            "hits",
            "pss_corr",
            "sss_corr",
            "sss_peak_ratio",
            "cp_metric",
            "pss_prominence_db",
            "cfo_hz",
            "raw_rms_dbfs",
            "ssb_bin_offset",
            "first_seen_utc",
            "last_seen_utc",
        ])
        for rank, cell in enumerate(cells, start=1):
            writer.writerow([
                rank,
                cell.gscn,
                f"{cell.ssb_frequency_hz:.3f}",
                f"{cell.scs_hz:.1f}",
                cell.label,
                cell.mcc,
                cell.mnc,
                cell.plmn,
                cell.operator_name,
                cell.operator_source,
                cell.operator_notes,
                cell.pci,
                cell.nid1,
                cell.nid2,
                f"{cell.rank_score:.3f}",
                f"{cell.best_quality_score:.3f}",
                f"{cell.mean_quality_score:.3f}",
                cell.hits,
                f"{cell.pss_corr:.6f}",
                f"{cell.sss_corr:.6f}",
                f"{cell.sss_peak_ratio:.6f}",
                f"{cell.cp_metric:.6f}",
                f"{cell.pss_prominence_db:.3f}",
                f"{cell.cfo_hz:.3f}",
                f"{cell.raw_rms_dbfs:.3f}",
                cell.ssb_bin_offset,
                cell.first_seen_utc,
                cell.last_seen_utc,
            ])
    return output


def build_monitor_argv(args, cell: DiscoveredCell) -> list[str]:
    """Build and return monitor argv."""
    monitor_script = Path(args.monitor_script).expanduser()
    if not monitor_script.is_absolute():
        monitor_script = SCRIPT_DIR / monitor_script
    monitor_script = monitor_script.resolve()
    if not monitor_script.exists():
        raise FileNotFoundError(f"Monitor script not found: {monitor_script}")

    argv = [
        sys.executable,
        str(monitor_script),
        "--device-args", str(args.device_args),
        "--channels", ",".join(str(v) for v in args.channels),
        "--antennas", ",".join(str(v) for v in args.antennas),
        "--gains", ",".join(f"{float(v):g}" for v in args.gains),
        "--primary-index", str(args.primary_index),
        "--spacing-m", str(args.spacing_m),
        "--ssb-frequency-hz", f"{cell.ssb_frequency_hz:.3f}",
        "--selected-gscn", str(cell.gscn),
        "--pci", str(cell.pci),
        "--rate", str(args.rate),
        "--detector-rate", str(args.detector_rate),
        "--scs", str(cell.scs_hz),
        "--burst-ms", str(args.monitor_burst_ms),
        "--lock-grace-windows", str(args.monitor_lock_grace_windows),
        "--lock-grace-seconds", str(args.monitor_lock_grace_seconds),
        "--lock-reference-reset-windows", str(args.monitor_lock_reference_reset_windows),
        "--lock-reference-reset-seconds", str(args.monitor_lock_reference_reset_seconds),
        "--lock-health-window", str(args.monitor_lock_health_window),
        "--tracking-max-pss-candidates", str(args.monitor_tracking_max_pss_candidates),
        "--tracking-pss-cluster-radius", str(args.monitor_tracking_pss_cluster_radius),
        "--threshold-db", str(args.threshold_db),
        "--peak-window", str(args.peak_window),
        "--minimum-threshold", str(args.minimum_threshold),
        "--guard-samples", str(args.guard_samples),
        "--min-pss-peak-over-median", str(args.min_pss_peak_over_median),
        "--pss-timing-radius", str(args.pss_timing_radius),
        "--ssb-bin-radius", str(args.ssb_bin_radius),
        "--cp-timing-radius", str(args.cp_timing_radius),
        "--sss-timing-radius", str(args.sss_timing_radius),
        "--sss-bin-radius", str(args.sss_bin_radius),
        "--min-fine-pss-corr", str(args.min_fine_pss_corr),
        "--min-sss-corr", str(args.min_sss_corr),
        "--min-sss-peak-ratio", str(args.min_sss_peak_ratio),
        "--min-cp-metric", str(args.min_cp_metric),
    ]
    if cell.mcc:
        argv.extend(["--selected-mcc", cell.mcc])
    if cell.mnc:
        argv.extend(["--selected-mnc", cell.mnc])
    if cell.plmn:
        argv.extend(["--selected-plmn", cell.plmn])
    if cell.operator_name:
        argv.extend(["--selected-operator-name", cell.operator_name])
    if cell.operator_source:
        argv.extend(["--selected-operator-source", cell.operator_source])
    if args.reverse_direction_labels:
        argv.append("--reverse-direction-labels")
    if args.monitor_extra_args:
        argv.extend(shlex.split(args.monitor_extra_args))
    return argv


def print_ranked_cells(cells: list[DiscoveredCell]) -> None:
    """Print ranked cells in a readable terminal format."""
    if not cells:
        print("[RESULT] No valid (SSB frequency, PCI) combinations were detected.")
        return
    print("\nRank  GSCN   SSB MHz       SCS  PCI  Score  Hits  PLMN      Operator                 PSS    SSS")
    print("----  -----  ------------  ---  ---  -----  ----  --------  -----------------------  -----  -----")
    for rank, cell in enumerate(cells, start=1):
        print(
            f"{rank:>4}  {cell.gscn:>5}  {cell.ssb_frequency_hz/1e6:>12.6f}  "
            f"{cell.scs_hz/1e3:>3.0f}  {cell.pci:>3}  {cell.rank_score:>5.1f}  "
            f"{cell.hits:>4}  {(cell.plmn or 'Unknown')[:8]:>8}  "
            f"{(cell.operator_name or 'Unknown')[:23]:<23}  "
            f"{cell.pss_corr:>5.3f}  {cell.sss_corr:>5.3f}"
        )


class ScannerDashboard:
    """Display ranked FR1 discoveries and hand a selected cell to the monitor."""

    def __init__(
        self,
        args,
        targets,
        core,
        step1,
        step2,
        sync,
        operator_resolver: Optional[OperatorMetadataResolver] = None,
    ) -> None:
        """Initialize the ScannerDashboard instance and its runtime state."""
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except Exception as exc:
            raise RuntimeError(
                "PyQt5 is required for the scanner GUI. Install with: "
                "python3 -m pip install PyQt5"
            ) from exc

        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.args = args
        self.targets = targets
        self.core = core
        self.step1 = step1
        self.step2 = step2
        self.sync = sync
        self.operator_resolver = operator_resolver or OperatorMetadataResolver()
        self.mailbox = ScanMailbox()
        self.scanner: Optional[RasterScanner] = None
        self.scan_thread: Optional[threading.Thread] = None
        self.last_sequence = -1
        self.cells_by_key: dict[tuple[int, int, int], DiscoveredCell] = {}
        self._last_table_signature: Optional[tuple] = None
        self._table_columns_initialized = False

        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
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
        self.window.setWindowTitle("OCUDU FR1 Raster Scanner → Downlink ISAC")
        self.window.resize(1480, 760)

        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        mapped_count = len(self.operator_resolver.entries)
        layout.addWidget(
            core.create_brand_header(
                QtCore,
                QtGui,
                QtWidgets,
                title_html=(
                    "<b>OCUDU FR1 Synchronization-Raster Scanner</b><br/>"
                    "PSS/SSS discovery and ranked handoff to the array ISAC monitor"
                    f"<br/><b>Operator map:</b> {mapped_count} enabled entr"
                    f"{'y' if mapped_count == 1 else 'ies'}"
                ),
                left_logo_path=args.left_logo,
                right_logo_path=args.right_logo,
            )
        )

        controls = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start / Rescan")
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.monitor_button = QtWidgets.QPushButton("Monitor Selected Cell")
        self.export_button = QtWidgets.QPushButton("Export CSV")
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.monitor_button)
        controls.addWidget(self.export_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, max(1, len(targets) * int(args.dwell_bursts)))
        layout.addWidget(self.progress)

        self.status = QtWidgets.QLabel("READY")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        columns = [
            "Rank", "GSCN", "SSB frequency (MHz)", "SCS (kHz)", "Label",
            "MCC", "MNC", "PLMN", "Operator", "Metadata source",
            "PCI", "Score", "Best quality", "Hits", "PSS corr", "SSS corr",
            "SSS ratio", "CP", "PSS prominence (dB)", "CFO (Hz)",
            "Raw RMS (dBFS)", "Bin offset", "Last seen (UTC)",
        ]
        self.table = QtWidgets.QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        note = QtWidgets.QLabel(
            "Discovery score is a non-standard synchronization-confidence ranking; "
            "it is not SS-RSRP, SS-RSRQ, or SS-SINR. MCC/MNC/PLMN/operator values "
            "marked MANUAL_MAP come from the local operator mapping CSV, not PSS/SSS. "
            "Unknown means no enabled mapping row matched the discovered GSCN/frequency, "
            "SCS, and PCI. Double-click a cell to hand it to the four-channel ISAC pipeline."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.window.setCentralWidget(central)
        self.start_button.clicked.connect(self.start_scan)
        self.stop_button.clicked.connect(self.stop_scan)
        self.monitor_button.clicked.connect(self.monitor_selected)
        self.export_button.clicked.connect(self.export_csv_dialog)
        self.table.itemSelectionChanged.connect(self.update_button_state)
        self.table.cellDoubleClicked.connect(lambda _row, _col: self.monitor_selected())

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)
        self.update_button_state()
        if args.fullscreen:
            # Use maximized, not Qt full-screen, so the native title bar remains visible.
            self.window.showMaximized()
        else:
            self.window.show()

        if args.auto_start:
            QtCore.QTimer.singleShot(100, self.start_scan)

    def start_scan(self) -> None:
        """Handle the start scan operation for this OCUDU processing stage."""
        if self.scan_thread is not None and self.scan_thread.is_alive():
            return
        self.mailbox = ScanMailbox()
        self.scanner = RasterScanner(
            self.args,
            self.targets,
            self.core,
            self.step1,
            self.step2,
            self.sync,
            self.mailbox,
            self.operator_resolver,
        )

        def runner() -> None:
            """Handle the runner operation for this OCUDU processing stage."""
            try:
                cells = self.scanner.scan()
                if self.args.export_csv:
                    output = export_cells_csv(self.args.export_csv, cells)
                    print(f"[CSV] Discovery results saved to {output}")
            except Exception as exc:
                print(f"[ERROR] Scanner failed: {exc}", file=sys.stderr)

        self.scan_thread = threading.Thread(
            target=runner,
            name="ocudu-fr1-raster-scan",
            daemon=True,
        )
        self.scan_thread.start()
        self.update_button_state()

    def stop_scan(self) -> None:
        """Handle the stop scan operation for this OCUDU processing stage."""
        if self.scanner is not None:
            self.scanner.request_stop()

    def refresh(self) -> None:
        """Refresh the displayed state from the latest thread-safe snapshot."""
        snapshot = self.mailbox.get()
        if snapshot.sequence == self.last_sequence:
            return
        self.last_sequence = snapshot.sequence

        completed = max(
            0,
            (snapshot.target_index - 1) * int(self.args.dwell_bursts)
            + snapshot.burst_index,
        )
        self.progress.setValue(completed)
        frequency = (
            ""
            if snapshot.current_frequency_hz is None
            else f" | {snapshot.current_frequency_hz / 1e6:.6f} MHz"
        )
        self.status.setText(
            f"{snapshot.message}{frequency} | "
            f"target {snapshot.target_index}/{snapshot.target_total} | "
            f"burst {snapshot.burst_index}/{snapshot.burst_total} | "
            f"cells {len(snapshot.cells)} | rejected candidates "
            f"{snapshot.rejected_candidates}"
            + (f"<br/><b>{snapshot.error}</b>" if snapshot.error else "")
        )
        self.populate_table(snapshot.cells)
        self.update_button_state()

    def populate_table(self, cells: list[DiscoveredCell]) -> None:
        # Rebuilding every QTableWidgetItem is expensive. Update only when the
        # ranked cell content changed, not for status-only mailbox updates.
        """Handle the populate table operation for this OCUDU processing stage."""
        signature = tuple(
            (
                cell.key,
                round(float(cell.rank_score), 3),
                int(cell.hits),
                cell.operator_source,
                cell.last_seen_utc,
            )
            for cell in cells
        )
        if signature == self._last_table_signature:
            return
        self._last_table_signature = signature

        selected_key = self.selected_key()
        self.cells_by_key = {cell.key: cell for cell in cells}
        self.table.setRowCount(len(cells))
        for row, cell in enumerate(cells):
            values = [
                str(row + 1),
                str(cell.gscn),
                f"{cell.ssb_frequency_hz / 1e6:.6f}",
                f"{cell.scs_hz / 1e3:.0f}",
                cell.label,
                cell.mcc or "Unknown",
                cell.mnc or "Unknown",
                cell.plmn or "Unknown",
                cell.operator_name or "Unknown",
                cell.operator_source or "Unmapped",
                str(cell.pci),
                f"{cell.rank_score:.1f}",
                f"{cell.best_quality_score:.1f}",
                str(cell.hits),
                f"{cell.pss_corr:.3f}",
                f"{cell.sss_corr:.3f}",
                f"{cell.sss_peak_ratio:.2f}",
                f"{cell.cp_metric:.3f}",
                f"{cell.pss_prominence_db:.1f}",
                f"{cell.cfo_hz:.1f}",
                f"{cell.raw_rms_dbfs:.1f}",
                str(cell.ssb_bin_offset),
                cell.last_seen_utc,
            ]
            for col, value in enumerate(values):
                item = self.QtWidgets.QTableWidgetItem(value)
                if col == 0:
                    item.setData(self.QtCore.Qt.UserRole, cell.key)
                self.table.setItem(row, col, item)

        if not self._table_columns_initialized and cells:
            self.table.resizeColumnsToContents()
            self._table_columns_initialized = True
        if selected_key is not None:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is not None and tuple(item.data(self.QtCore.Qt.UserRole)) == selected_key:
                    self.table.selectRow(row)
                    break

    def selected_key(self) -> Optional[tuple[int, int, int]]:
        """Handle the selected key operation for this OCUDU processing stage."""
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        value = item.data(self.QtCore.Qt.UserRole)
        return tuple(value) if value is not None else None

    def selected_cell(self) -> Optional[DiscoveredCell]:
        """Handle the selected cell operation for this OCUDU processing stage."""
        key = self.selected_key()
        return self.cells_by_key.get(key) if key is not None else None

    def update_button_state(self) -> None:
        """Update button state with the newest state."""
        running = self.scan_thread is not None and self.scan_thread.is_alive()
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.monitor_button.setEnabled((not running) and self.selected_cell() is not None)
        self.export_button.setEnabled((not running) and bool(self.cells_by_key))

    def export_csv_dialog(self) -> None:
        """Export csv dialog to the requested output path."""
        cells = list(self.cells_by_key.values())
        cells.sort(key=lambda c: c.rank_score, reverse=True)
        if not cells:
            return
        default = str((Path.cwd() / "ocudu_fr1_discovered_cells.csv").resolve())
        path, _ = self.QtWidgets.QFileDialog.getSaveFileName(
            self.window,
            "Export discovered cells",
            default,
            "CSV files (*.csv)",
        )
        if path:
            output = export_cells_csv(path, cells)
            self.status.setText(f"Exported {len(cells)} cells to {output}")

    def monitor_selected(self) -> None:
        """Handle the monitor selected operation for this OCUDU processing stage."""
        if self.scan_thread is not None and self.scan_thread.is_alive():
            return
        cell = self.selected_cell()
        if cell is None:
            return
        argv = build_monitor_argv(self.args, cell)
        print("[HANDOFF] Launching selected cell in four-channel ISAC monitor:")
        print(" ".join(shlex.quote(v) for v in argv))
        self.timer.stop()
        self.window.close()
        os.chdir(SCRIPT_DIR)
        os.execv(sys.executable, argv)

    def exec(self) -> int:
        """Handle the exec operation for this OCUDU processing stage."""
        return int(self.app.exec_())

    def close(self) -> None:
        """Close the component and release GUI or worker resources."""
        self.stop_scan()
        if self.scan_thread is not None and self.scan_thread.is_alive():
            self.scan_thread.join(timeout=2.0)
        self.timer.stop()
        self.window.close()


def add_sync_arguments(parser: argparse.ArgumentParser) -> None:
    """Handle the add sync arguments operation for this OCUDU processing stage."""
    defaults = [
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
    for name, value_type, default in defaults:
        parser.add_argument("--" + name, type=value_type, default=default)


def build_parser(core: ModuleType) -> argparse.ArgumentParser:
    """Build and return parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Scan configured FR1 GSCNs, rank detected (frequency, PCI) cells, "
            "optionally annotate them from a local operator map, and launch the "
            "selected cell in the OCUDU array ISAC monitor."
        )
    )

    parser.add_argument("--device-args", default=DEFAULT_DEVICE_ARGS,
                        help="UHD device args; type is auto-detected for X410/N310 compatibility.")
    parser.add_argument("--channels", type=core.parse_csv_ints, default=[0, 1, 2, 3])
    parser.add_argument("--antennas", type=core.parse_csv_strings, default=["auto"],
                        help="Comma-separated RX antenna names, or auto to keep the device default.")
    parser.add_argument("--gains", type=core.parse_csv_floats, default=[30.0])
    parser.add_argument("--primary-index", type=int, default=0)
    parser.add_argument("--spacing-m", type=float, default=0.0433)
    parser.add_argument("--reverse-direction-labels", action="store_true")

    parser.add_argument(
        "--scan-gscn",
        default="7821",
        help="Inclusive GSCN list/ranges, e.g. 7821,7900:7920:2.",
    )
    parser.add_argument(
        "--scan-plan-file",
        default=None,
        help="CSV with gscn,scs_khz,label,enabled; overrides --scan-gscn.",
    )
    parser.add_argument("--scan-scs-khz", type=float, choices=[15.0, 30.0], default=30.0)
    parser.add_argument(
        "--operator-map-file",
        default=None,
        help=(
            "Optional CSV mapping a known GSCN/frequency and PCI to MCC, MNC, "
            "PLMN, and operator name. Values are annotated as MANUAL_MAP."
        ),
    )
    parser.add_argument(
        "--operator-frequency-tolerance-hz",
        type=float,
        default=1.0,
        help="Frequency tolerance used when an operator-map row is keyed by frequency.",
    )
    parser.add_argument("--min-frequency-mhz", type=float, default=410.0)
    parser.add_argument("--max-frequency-mhz", type=float, default=7125.0)
    parser.add_argument("--dwell-bursts", type=int, default=2)
    parser.add_argument("--burst-ms", type=float, default=40.0)
    parser.add_argument("--retune-settle-ms", type=float, default=75.0)
    parser.add_argument("--settle-time", type=float, default=0.5)
    parser.add_argument("--capture-timeout-s", type=float, default=3.0)
    parser.add_argument("--max-pss-candidates", type=int, default=16)
    parser.add_argument("--pss-cluster-radius", type=int, default=64)

    parser.add_argument("--rate", type=float, default=7.68e6)
    parser.add_argument("--detector-rate", type=float, default=7.68e6)
    # Dynamically set for every target; defined for helper compatibility.
    parser.add_argument("--scs", type=float, default=30e3, help=argparse.SUPPRESS)
    add_sync_arguments(parser)

    parser.add_argument(
        "--monitor-script",
        default="ocudu_array_monitor.py",
    )
    parser.add_argument("--monitor-burst-ms", type=float, default=40.0)
    parser.add_argument("--monitor-lock-grace-windows", type=int, default=6)
    parser.add_argument("--monitor-lock-grace-seconds", type=float, default=2.0)
    parser.add_argument("--monitor-lock-reference-reset-windows", type=int, default=15)
    parser.add_argument("--monitor-lock-reference-reset-seconds", type=float, default=4.0)
    parser.add_argument("--monitor-lock-health-window", type=int, default=50)
    parser.add_argument("--monitor-tracking-max-pss-candidates", type=int, default=8)
    parser.add_argument("--monitor-tracking-pss-cluster-radius", type=int, default=64)
    parser.add_argument(
        "--monitor-extra-args",
        default="",
        help="Additional quoted arguments appended to the selected-cell monitor.",
    )

    parser.add_argument("--export-csv", default=None)
    parser.add_argument(
        "--left-logo",
        default="assets/csics_odu_logo_white_bg.png",
        help="Project-relative or absolute path to the upper-left CSICS/ODU logo.",
    )
    parser.add_argument(
        "--right-logo",
        default="assets/odu_logo_white_bg.png",
        help="Project-relative or absolute path to the upper-right ODU logo.",
    )
    parser.add_argument(
        "--fullscreen",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Open the dashboard maximized while retaining title-bar controls; use --no-fullscreen for a normal window.",
    )
    parser.add_argument("--no-gui", action="store_true")
    parser.add_argument("--auto-start", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--monitor-best", action="store_true")
    parser.add_argument("--dry-run-plan", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def normalize_args(args, core: ModuleType) -> None:
    """Normalize args into the expected representation."""
    count = len(args.channels)
    args.antennas = core.expand_values(args.antennas, count, "--antennas")
    args.gains = [float(v) for v in core.expand_values(args.gains, count, "--gains")]
    if not 0 <= int(args.primary_index) < count:
        raise ValueError(f"--primary-index must be in 0..{count - 1}")
    if int(args.dwell_bursts) < 1:
        raise ValueError("--dwell-bursts must be at least 1")
    if float(args.rate) < 7.68e6 or float(args.detector_rate) < 7.68e6:
        raise ValueError("The current PSS/SSS implementation requires at least 7.68 MS/s")
    if float(args.operator_frequency_tolerance_hz) < 0.0:
        raise ValueError("--operator-frequency-tolerance-hz cannot be negative")
    if float(args.monitor_burst_ms) <= 0.0:
        raise ValueError("--monitor-burst-ms must be positive")
    if int(args.monitor_lock_grace_windows) < 0:
        raise ValueError("--monitor-lock-grace-windows cannot be negative")
    if float(args.monitor_lock_grace_seconds) < 0.0:
        raise ValueError("--monitor-lock-grace-seconds cannot be negative")
    if int(args.monitor_lock_reference_reset_windows) <= int(args.monitor_lock_grace_windows):
        raise ValueError(
            "--monitor-lock-reference-reset-windows must exceed "
            "--monitor-lock-grace-windows"
        )
    if float(args.monitor_lock_reference_reset_seconds) < float(args.monitor_lock_grace_seconds):
        raise ValueError(
            "--monitor-lock-reference-reset-seconds must be at least "
            "--monitor-lock-grace-seconds"
        )
    if int(args.monitor_lock_health_window) < 1:
        raise ValueError("--monitor-lock-health-window must be at least 1")
    if int(args.monitor_tracking_max_pss_candidates) < 1:
        raise ValueError("--monitor-tracking-max-pss-candidates must be at least 1")
    if int(args.monitor_tracking_pss_cluster_radius) < 0:
        raise ValueError("--monitor-tracking-pss-cluster-radius cannot be negative")


def build_targets(args):
    """Build configured raster targets without an external helper module."""
    min_hz = float(args.min_frequency_mhz) * 1e6
    max_hz = float(args.max_frequency_mhz) * 1e6
    if args.scan_plan_file:
        targets = load_scan_plan_csv(
            args.scan_plan_file, min_frequency_hz=min_hz, max_frequency_hz=max_hz
        )
    else:
        targets = build_raster_targets(
            parse_gscn_spec(args.scan_gscn),
            scs_hz=float(args.scan_scs_khz) * 1e3,
            min_frequency_hz=min_hz,
            max_frequency_hz=max_hz,
            label="CLI",
        )
    if not targets:
        raise ValueError("The configured scan plan contains no targets in the frequency limits")
    return targets


def self_test() -> int:
    """Run hardware-free regression checks for this processing component."""
    raster_self_test()

    class Result:
        """Represent result state and behavior used by the OCUDU pipeline."""
        pss_corr = 0.92
        sss_corr = 0.88
        coarse_peak_over_median = 1000.0
        sss_peak_ratio = 1.8
        cp_metric = 0.4

    score = discovery_quality(Result())
    assert 0.0 <= score <= 100.0

    catalog = CellCatalog()
    base = CellObservation(
        timestamp_utc=utc_now(),
        gscn=7821,
        ssb_frequency_hz=3_463.68e6,
        scs_hz=30e3,
        label="test",
        pci=1,
        nid1=0,
        nid2=1,
        quality_score=score,
        pss_corr=0.92,
        sss_corr=0.88,
        sss_peak_ratio=1.8,
        cp_metric=0.4,
        pss_peak_over_median=1000.0,
        pss_peak_over_threshold=4.0,
        cfo_hz=125.0,
        raw_rms_dbfs=-42.0,
        ssb_bin_offset=0,
    )
    catalog.update(base)
    catalog.update(replace(base, quality_score=score - 2.0))
    ranked = catalog.ranked()
    assert len(ranked) == 1 and ranked[0].hits == 2
    assert ranked[0].pci == 1

    resolver = OperatorMetadataResolver([
        OperatorMapEntry(
            pci=1,
            gscn=7821,
            scs_hz=30e3,
            row_number=2,
            metadata=OperatorMetadata(
                mcc="001", mnc="01", plmn="001-01",
                operator_name="OCUDU Lab", source="MANUAL_MAP:self-test",
            ),
        )
    ])
    metadata = resolver.resolve(
        gscn=7821,
        ssb_frequency_hz=3_463.68e6,
        scs_hz=30e3,
        pci=1,
    )
    assert metadata.plmn == "001-01" and metadata.operator_name == "OCUDU Lab"
    print("[SCANNER-SELF-TEST] PASS")
    return 0


def main() -> int:
    """Run the command-line entry point and return its process exit status."""
    core = default_core
    parser = build_parser(core)
    args = parser.parse_args()
    normalize_args(args, core)

    if args.self_test:
        return self_test()

    targets = build_targets(args)
    operator_resolver = OperatorMetadataResolver.from_csv(
        args.operator_map_file,
        frequency_tolerance_hz=float(args.operator_frequency_tolerance_hz),
    )
    print("Configured scan targets:")
    for index, target in enumerate(targets, start=1):
        print(
            f"  {index:>4}: GSCN={target.gscn} "
            f"f={target.ssb_frequency_hz / 1e6:.6f} MHz "
            f"SCS={target.scs_hz / 1e3:.0f} kHz label={target.label or '-'}"
        )
    if args.dry_run_plan:
        return 0

    step1, step2, sync = default_step1, default_step2, default_sync

    if args.no_gui:
        mailbox = ScanMailbox()
        scanner = RasterScanner(
            args, targets, core, step1, step2, sync, mailbox, operator_resolver
        )

        def stop_handler(*_):
            """Handle the stop handler operation for this OCUDU processing stage."""
            scanner.request_stop()

        signal.signal(signal.SIGINT, stop_handler)
        cells = scanner.scan()
        print_ranked_cells(cells)
        if args.export_csv:
            output = export_cells_csv(args.export_csv, cells)
            print(f"[CSV] Discovery results saved to {output}")
        if args.monitor_best and cells:
            argv = build_monitor_argv(args, cells[0])
            print("[HANDOFF] Launching highest-ranked cell")
            os.chdir(SCRIPT_DIR)
            os.execv(sys.executable, argv)
        return 0

    dashboard = ScannerDashboard(
        args, targets, core, step1, step2, sync, operator_resolver
    )

    def shutdown(*_):
        """Handle the shutdown operation for this OCUDU processing stage."""
        dashboard.close()
        dashboard.app.quit()

    signal.signal(signal.SIGINT, shutdown)
    heartbeat = dashboard.QtCore.QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)
    try:
        return dashboard.exec()
    finally:
        dashboard.close()


if __name__ == "__main__":
    raise SystemExit(main())

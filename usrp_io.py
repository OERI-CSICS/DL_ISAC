#!/usr/bin/env python3
"""Shared USRP compatibility helpers for the CSICS passive-ISAC tools.

The signal-processing pipeline is intentionally hardware-family agnostic.  These
helpers keep the UHD setup portable between MPM-based USRPs such as the X410
(type=x4xx) and N310 (type=n3xx), while preserving explicit user overrides.
"""
from __future__ import annotations

from typing import Iterable

DEFAULT_DEVICE_ARGS = "addr=192.168.10.2"
_AUTO_ANTENNAS = {"", "auto", "default", "none"}


def open_multi_usrp(uhd, device_args: str):
    """Open a UHD MultiUSRP and add actionable context to MPM mismatch errors."""
    args = str(device_args or "").strip()
    try:
        return uhd.usrp.MultiUSRP(args)
    except RuntimeError as exc:
        text = str(exc)
        if "MPM major compat number mismatch" in text or "MPM" in text and "compat" in text.lower():
            raise RuntimeError(
                text
                + "\n\n"
                + "CSICS USRP compatibility diagnostic:\n"
                + "  The host UHD library and the embedded UHD/MPM software on the USRP do not match.\n"
                + "  If this same USRP works from another computer, first make the failing computer use\n"
                + "  the SAME UHD release/build as the working computer; do not reflash the radio first.\n"
                + "\n"
                + "  Compare on BOTH host computers:\n"
                + "    uhd_config_info --version\n"
                + "    which uhd_config_info\n"
                + "    python3 -c \"import uhd; print(uhd.__file__)\"\n"
                + "\n"
                + "  Then verify the radio after the host versions match:\n"
                + f"    uhd_usrp_probe --args \"{args}\"\n"
                + "\n"
                + "  For an N310, the alternative is to update/downgrade its N3xx filesystem/MPM to\n"
                + "  the desired UHD release. For an X410, update its X4xx filesystem to the matching\n"
                + "  UHD release. See README_USRP_X410_N310_COMPATIBILITY.md in this package."
            ) from exc
        if "Failure to create rfnoc_graph" in text or "RFNOC::GRAPH" in text:
            raise RuntimeError(
                text
                + "\n\n"
                + "CSICS RFNoC/CHDR diagnostic:\n"
                + "  MultiUSRP on N3xx/X4xx uses RFNoC internally. This error is normally below the\n"
                + "  application layer: inspect the UHD log lines immediately ABOVE this exception.\n"
                + "\n"
                + "  N310: use the explicit management/data addresses that match the radio wiring.\n"
                + "  For the CSICS XG N310 profile the expected form is:\n"
                + "    type=n3xx,mgmt_addr=192.168.30.2,addr=192.168.10.2,second_addr=192.168.20.2\n"
                + "  X410 example:\n"
                + "    type=x4xx,addr=192.168.10.2\n"
                + "\n"
                + "  First reproduce outside Python:\n"
                + f"    uhd_usrp_probe --args \"{args}\"\n"
                + "  If probe also fails, fix UHD/FPGA/network first; changing DSP code will not fix it.\n"
                + "  Check host routes, SFP cabling, MTU, firewall, and that the FPGA image matches UHD."
            ) from exc
        raise


def validate_rx_channels(usrp, channels: Iterable[int]) -> int:
    """Validate requested RX channels against the actual connected USRP."""
    count = int(usrp.get_rx_num_channels())
    bad = [int(ch) for ch in channels if int(ch) < 0 or int(ch) >= count]
    if bad:
        raise RuntimeError(
            f"Requested RX channel(s) {bad}, but the connected USRP reports "
            f"{count} RX channel(s): valid indices are 0..{count - 1}."
        )
    return count


def configure_rx_channel(usrp, uhd, channel: int, rate: float, freq: float,
                         gain: float, antenna: str | None = "auto") -> str:
    """Configure one RX channel, choosing the device default antenna in AUTO mode."""
    channel = int(channel)
    validate_rx_channels(usrp, [channel])

    usrp.set_rx_rate(float(rate), channel)
    usrp.set_rx_freq(uhd.types.TuneRequest(float(freq)), channel)
    usrp.set_rx_gain(float(gain), channel)

    requested = str(antenna or "").strip()
    if requested.lower() not in _AUTO_ANTENNAS:
        available = [str(x) for x in usrp.get_rx_antennas(channel)]
        if requested in available:
            usrp.set_rx_antenna(requested, channel)
        else:
            current = str(usrp.get_rx_antenna(channel))
            print(
                f"[WARN] RX antenna '{requested}' is not available on channel {channel}. "
                f"Available={available}. Keeping device default '{current}'."
            )

    return str(usrp.get_rx_antenna(channel))


def device_identity(usrp, channel: int = 0) -> dict[str, str]:
    """Return best-effort model/serial information without depending on one UHD version."""
    info: dict[str, str] = {}
    try:
        raw = usrp.get_usrp_rx_info(int(channel))
        if raw:
            info.update({str(k): str(v) for k, v in dict(raw).items()})
    except Exception:
        pass
    try:
        info.setdefault("mboard_name", str(usrp.get_mboard_name(0)))
    except Exception:
        pass
    return info


def device_label(usrp, channel: int = 0) -> str:
    """Create a short human-readable connected-device label."""
    info = device_identity(usrp, channel)
    product = info.get("product") or info.get("mboard_name") or info.get("mboard_id") or "USRP"
    serial = info.get("serial") or info.get("mboard_serial")
    return f"{product} (serial {serial})" if serial else product

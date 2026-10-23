# OCUDU Downlink Passive ISAC — Lean Branded X410/N310 Package

Version: **V03 Lean / Waterfall-Optimized / CSICS-ODU Branded**  
Purpose: passive 5G NR downlink synchronization, FR1 SSB discovery, multi-channel channel-response sensing, motion/activity detection, relative spatial trend visualization, and optional ML dataset recording using Ettus USRP X410 or N310.

> This software is receive-only and intended for authorized laboratory/testbed use.

---

## 1. What changed in this release

The original V01 archive had accumulated milestone reports, multiple README files, launcher variants, test scripts, example CSV files, and Python cache files. The runtime code was spread among the useful application modules plus many non-runtime artifacts.

V03 keeps the lean V02 runtime architecture and restores only the **two CSICS/ODU branding PNGs** needed to reproduce the original dashboard appearance. The FR1 raster helper remains folded into the scanner. Historical documents, launch wrappers, examples, tests, and `__pycache__` files are not shipped.

### Final package layout

```text
OCUDU_DOWNLINK_ISAC_V03_BRANDED/
├── README.md
├── requirements.txt
├── ocudu_fr1_raster_scanner.py
├── ocudu_array_monitor.py
├── isac_core.py
├── nr_pss.py
├── nr_cell_sync.py
├── nr_sync.py
├── usrp_io.py
├── dataset_recorder.py
└── assets/
    ├── csics_odu_logo_white_bg.png
    └── odu_logo_white_bg.png
```

### Module responsibilities

| File | Responsibility |
|---|---|
| `ocudu_fr1_raster_scanner.py` | FR1 GSCN scan plan, PSS/SSS discovery, cell ranking, optional operator mapping, GUI/CLI scanner, selected-cell handoff to the ISAC monitor. |
| `ocudu_array_monitor.py` | Main real-time ISAC worker and PyQtGraph dashboard. Maintains lock continuity, motion/activity state, direction trend, waterfall, metrics, and dataset handoff. |
| `isac_core.py` | Multi-channel UHD receive ring, synchronized channel extraction, array calibration, activity fusion, residual/spatial calculations, utility parsing. |
| `nr_pss.py` | PSS generation/detection, frequency shift/decimation, NR-ARFCN conversion, multicore PSS search. |
| `nr_cell_sync.py` | Fine PSS/SSS processing, PCI recovery, candidate handling, deterministic SSB-grid extraction. |
| `nr_sync.py` | NR PSS/SSS sequences, OFDM/CP timing, CFO estimation/correction, SSB resource-grid helpers. |
| `usrp_io.py` | X410/N310 UHD compatibility layer, channel validation, antenna handling, device identity, MPM/RFNoC diagnostics. |
| `dataset_recorder.py` | Non-blocking feature/IQ dataset recording and binary IQ spool-to-CSV finalization. |
| `requirements.txt` | Python GUI/DSP dependencies. UHD itself is installed through Ettus/OS packages, not pip. |
| `assets/*.png` | Bundled CSICS/ODU branding used by both dashboards by default. |

---

## 2. Waterfall sensing optimization

The major performance work in V02 is not merely cosmetic. It changes how sensing rows are transported and rendered.

### Problem in V01

The processing worker stored only the newest `waterfall_row` in the latest-state snapshot. If several synchronized sensing windows completed between two GUI refreshes, only the newest row remained. Intermediate sensing rows were therefore lost from the waterfall even though the DSP had processed them.

The GUI also updated the history using operations equivalent to:

```python
waterfall[:-1] = waterfall[1:]
waterfall[-1] = new_row
limit = percentile(abs(waterfall), 98)
setImage(waterfall)
```

This causes two avoidable costs:

1. the entire waterfall history is shifted for every new displayed row;
2. adaptive levels are recomputed over the full waterfall for every update.

### V02/V03 optimized architecture

V02 uses two different transport policies:

- **latest-state mailbox** for scalar status and curves where stale frames should be discarded;
- **bounded waterfall-row deque** for time-history rows that should be preserved.

The GUI drains all queued waterfall rows on each refresh and inserts them into a **mirrored circular buffer**. The mirrored layout makes the chronological image available as a contiguous NumPy view without shifting the full history.

Adaptive color levels are now rate-limited independently of sensing and image rendering.

### New waterfall options

```text
--gui-fps FLOAT
    Main dashboard/status refresh rate. Default: 20 Hz.

--waterfall-fps FLOAT
    Maximum ImageItem rendering rate. Default: 10 Hz.
    Sensing continues at full processing rate between image renders.

--waterfall-level-period-s FLOAT
    Minimum interval between adaptive 98th-percentile level calculations.
    Default: 0.5 s.

--waterfall-queue-rows INT
    Maximum sensed rows waiting for the GUI. Default: 512.
    The queue is bounded so GUI slowdown cannot consume unlimited memory.

--history INT
    Number of rows visible in the waterfall. Default: 120.

--adaptive-waterfall / --no-adaptive-waterfall
    Enable/disable automatic waterfall level adjustment.

--waterfall-display all|triggered
    `all`: display residual sensing rows continuously.
    `triggered`: mask rows while the detector declares STILL.
```

The status line includes a `waterfall dropped=` counter. A value of zero means all queued sensing rows have reached the display buffer. If this counter increases, either increase `--waterfall-queue-rows` or reduce GUI/render load.

### Synthetic microbenchmark

A local 5,000-row CPU microbenchmark compared the old full-history shift + per-row percentile path with the new circular insertion + throttled percentile path using a 120 × 127 waterfall:

```text
Old path: 0.878 s
New path: 0.353 s
Relative speedup: ~2.48×
```

This benchmark measures only the **waterfall update/level-management path**. It does not imply that UHD sample streaming, PSS/SSS synchronization, or end-to-end RF sensing is 2.48× faster.

---

## 3. Important design principle: sensing must not wait for drawing

The real-time path is organized as:

```text
USRP X410/N310
      │
      ▼
UHD continuous multi-channel RX thread
      │
      ▼
Aligned sample ring buffer
      │
      ▼
Processing worker
  ├─ PSS search
  ├─ SSS / PCI lock
  ├─ timing + CFO correction
  ├─ 127-subcarrier channel estimation
  ├─ multi-antenna residual/spatial activity
  ├─ event detector / direction trend
  └─ waterfall-row queue
      │
      ├──────── latest state ────────┐
      │                              │
      ▼                              ▼
Bounded waterfall queue        status/curve snapshot
      │                              │
      └──────────────┬───────────────┘
                     ▼
               PyQtGraph GUI
```

The GUI never owns the RF receive loop. Rendering latency therefore does not directly stall `recv()`.

---

## 4. System requirements

Recommended environment:

- Ubuntu Linux
- Python 3.10+
- Ettus UHD with Python bindings
- NumPy
- SciPy
- PyQt5
- PyQtGraph
- Ethernet interface(s) configured for the selected USRP data path

Install Python GUI/DSP packages:

```bash
python3 -m pip install -r requirements.txt
```

UHD should normally be installed from the Ettus-supported packages/repository or built consistently on the host. Verify the bindings before using this code:

```bash
uhd_config_info --version
uhd_find_devices
python3 -c "import uhd; print(uhd.__file__)"
```

Do **not** install an unrelated pip package named `uhd` as a substitute for the Ettus Python bindings.

---

## 5. Validate the radio before running the application

Always make `uhd_usrp_probe` work first. If it cannot initialize the device, the Python application will not repair the underlying UHD/MPM/FPGA/network problem.

### X410 example

```bash
uhd_usrp_probe --args "type=x4xx,addr=192.168.10.2"
```

### N310 example — one data interface

Use the management address and the SFP data address that actually match your lab wiring, for example:

```bash
uhd_usrp_probe --args "type=n3xx,mgmt_addr=192.168.30.2,addr=192.168.10.2"
```

### N310 example — two data interfaces

Only specify `second_addr` when the second data link is physically connected, configured, routable, and intended to be used:

```bash
uhd_usrp_probe --args "type=n3xx,mgmt_addr=192.168.30.2,addr=192.168.10.2,second_addr=192.168.20.2"
```

For multi-channel/high-rate N310 operation, confirm the host has the expected routes and link MTU:

```bash
ip -br addr
ip route get 192.168.10.2
ip route get 192.168.20.2
ip link
```

---

## 6. X410/N310 device compatibility

The application uses the high-level UHD `MultiUSRP` API. It does not manually construct an RFNoC graph. On RFNoC-capable MPM devices, UHD may use RFNoC internally while `MultiUSRP` automatically connects the receive streamer to the radio/DSP chain.

`usrp_io.py` provides the common hardware layer:

- opens `MultiUSRP`;
- validates requested RX channel indices against the connected device;
- configures sample rate, frequency, gain, and antenna;
- supports `antenna=auto` so family-specific antenna names are not forced;
- supplies clearer diagnostic messages for MPM compatibility and RFNoC graph failures.

For portability, prefer explicit device strings when the lab contains more than one USRP.

---

## 7. Run hardware-free tests first

These do not require an SDR:

```bash
python3 nr_pss.py --self-test
python3 isac_core.py --self-test
python3 ocudu_fr1_raster_scanner.py --self-test
python3 ocudu_array_monitor.py --self-test
```

On a workstation with PyQt5/PyQtGraph installed, test the dashboard without UHD:

```bash
python3 ocudu_array_monitor.py \
  --gui-smoke-test \
  --channels 0,1 \
  --no-fullscreen
```

The normal window uses native title-bar controls, including minimize, maximize/restore, and close.

---

## 8. Workflow A — discover a cell with the FR1 raster scanner

Example using an X410:

```bash
python3 ocudu_fr1_raster_scanner.py \
  --device-args "type=x4xx,addr=192.168.10.2" \
  --channels 0,1,2,3 \
  --antennas auto \
  --gains 30 \
  --scan-gscn 7821 \
  --scan-scs-khz 30
```

Example using an N310:

```bash
python3 ocudu_fr1_raster_scanner.py \
  --device-args "type=n3xx,mgmt_addr=192.168.30.2,addr=192.168.10.2,second_addr=192.168.20.2" \
  --channels 0,1,2,3 \
  --antennas auto \
  --gains 30 \
  --scan-gscn 7821 \
  --scan-scs-khz 30
```

The scanner recovers PSS/SSS and ranks unique cells by GSCN/frequency/SCS/PCI. Selecting a row and choosing **Monitor Selected Cell** launches the array monitor with the discovered SSB frequency and PCI.

### Scan multiple GSCNs

```bash
--scan-gscn "7821,7823:7841:2"
```

The range syntax is inclusive:

```text
first:last:step
```

### Use a scan-plan CSV

Create a file such as `my_scan_plan.csv`:

```csv
gscn,scs_khz,label,enabled
7821,30,OCUDU lab,1
7823,30,adjacent raster,1
7825,30,disabled example,0
```

Then:

```bash
python3 ocudu_fr1_raster_scanner.py \
  --scan-plan-file my_scan_plan.csv \
  --device-args "..."
```

---

## 9. Optional operator metadata

PSS/SSS identify synchronization and PCI; they do not by themselves provide MCC/MNC/operator identity. The scanner therefore does not guess the operator from PCI or spectrum.

If the lab already knows a mapping, provide an explicit CSV:

```csv
enabled,gscn,ssb_frequency_mhz,scs_khz,pci,mcc,mnc,plmn,operator_name,notes
1,7821,3463.680,30,1,001,01,001-01,OCUDU Lab Network,authorized lab mapping
```

Run:

```bash
python3 ocudu_fr1_raster_scanner.py \
  --operator-map-file my_operator_map.csv \
  --device-args "..."
```

Mapped values are labeled as manual-map metadata rather than over-the-air decoding.

---

## 10. Workflow B — run the ISAC monitor directly

If SSB center frequency and PCI are already known, skip raster discovery.

Example:

```bash
python3 ocudu_array_monitor.py \
  --device-args "type=x4xx,addr=192.168.10.2" \
  --channels 0,1,2,3 \
  --antennas auto \
  --gains 30 \
  --ssb-frequency-hz 3463680000 \
  --pci 1 \
  --rate 7.68e6 \
  --detector-rate 7.68e6 \
  --scs 30000
```

N310 example:

```bash
python3 ocudu_array_monitor.py \
  --device-args "type=n3xx,mgmt_addr=192.168.30.2,addr=192.168.10.2,second_addr=192.168.20.2" \
  --channels 0,1,2,3 \
  --antennas auto \
  --gains 30 \
  --ssb-frequency-hz 3463680000 \
  --pci 1 \
  --rate 7.68e6 \
  --detector-rate 7.68e6 \
  --scs 30000
```

If only one N310 data cable is connected, remove `second_addr`.

---

## 11. Array/channel configuration

### Select channels

```bash
--channels 0
--channels 0,1
--channels 0,1,2,3
```

The code checks the actual number of RX channels reported by UHD before starting.

### Gain

One value expands to all selected channels:

```bash
--gains 30
```

or specify per-channel values:

```bash
--gains 30,30,28,28
```

### Antenna

Recommended portable setting:

```bash
--antennas auto
```

Explicit names can be supplied when needed:

```bash
--antennas RX2,RX2,RX2,RX2
```

If an explicit antenna does not exist on the connected device, the compatibility layer warns and keeps the device default rather than silently assuming an X410-specific name.

### Primary synchronization element

```bash
--primary-index 0
```

If synchronization fallback is enabled, other selected channels can be tried when the primary channel temporarily misses the target PCI.

### Physical element labels

To make the plots match the antenna board instead of showing only UHD channel numbers, use `--element-labels` with exactly one label per selected channel:

```bash
--channels 0,1,2,3 \
--element-labels Top0,Top1,Top2,Top3
```

The labels affect GUI/recording metadata only; the UHD channel mapping remains controlled by `--channels`.

### N310 to the attached 2 × 8 array: recommended receive-only cabling

The N310 provides **four independent RX chains**, RF0 through RF3. Each RF group has both a `TX/RX` connector and an `RX2` connector, but those two connectors are alternative inputs to the **same receive chain**. Connecting antennas to all eight physical N310 RF SMA connectors does **not** create eight simultaneous receivers.

For the current one-dimensional AoA/activity implementation, use **four adjacent elements from one row** of the 2 × 8 array. The simplest mapping is the first four elements of the TOP row:

| Array element | N310 front-panel group | N310 connector | UHD channel | GUI label |
|---|---|---|---:|---|
| TOP Ant 0 | RF0 | RX2 | 0 | `Top0` |
| TOP Ant 1 | RF1 | RX2 | 1 | `Top1` |
| TOP Ant 2 | RF2 | RX2 | 2 | `Top2` |
| TOP Ant 3 | RF3 | RX2 | 3 | `Top3` |

Use four **50-ohm, equal-electrical-length, phase-matched coax cables**. Use the same cable type and connector/adaptor type on all four paths. For a receive-only passive-ISAC setup, `RX2` is a clean choice because it avoids using the shared transmit/receive connector. Do not connect a second antenna to `TX/RX` on the same RF group and expect another independent channel.

Run the monitor as:

```bash
python3 ocudu_array_monitor.py \
  --device-args "type=n3xx,mgmt_addr=192.168.30.2,addr=192.168.10.2,second_addr=192.168.20.2" \
  --channels 0,1,2,3 \
  --antennas RX2,RX2,RX2,RX2 \
  --element-labels Top0,Top1,Top2,Top3 \
  --spacing-m YOUR_MEASURED_CENTER_TO_CENTER_SPACING_M
```

Do **not** copy the default `--spacing-m` blindly. Measure the physical center-to-center spacing of adjacent patches on this particular antenna panel. For operation around 3.1–3.45 GHz, half a free-space wavelength is approximately 48.4–43.4 mm. A uniform spacing near or below half a wavelength is desirable for unambiguous broadside ULA AoA; larger spacing can create grating-lobe ambiguities.

If you prefer to use the central four patches, `TOP Ant 2..5` is equally valid; simply cable them in left-to-right order to RF0..RF3 and label them `Top2,Top3,Top4,Top5`. Keep the four physical positions contiguous and ordered.

**Do not mix TOP and BOTTOM row elements for the current 1-D AoA estimator.** The present spatial model assumes the selected elements lie on one straight line with uniform spacing. A 2 × 2 or 2 × 4 selection spanning both rows requires a 2-D array manifold / azimuth-elevation estimator. The activity waterfall can still use arbitrary channels, but the reported one-dimensional direction/AoA would no longer have the intended geometry.

#### Cable and calibration requirements for phase-based sensing

1. Use equal-length, phase-stable 50-ohm coax on all four channels.
2. Route the cables similarly; avoid one cable being tightly coiled while others are straight.
3. Start with the same RX gain on all channels.
4. Perform a broadside calibration using a stationary known source before an AoA experiment and retain the per-channel phase offsets for that session.
5. Recalibrate after changing cables, RF ports, frequency, master clock, or after a radio reinitialization. N310 channel phase can change between initialization runs.
6. Keep the received power within the N310 input limits. The Ettus N310 specification lists a maximum recommended receiver input level of about **-15 dBm at the 1-dB compression point**; use external attenuation if your nearby gNB/test transmitter can drive the array above a safe receive level.

#### What if all 16 array elements are required?

One N310 cannot digitize all 16 elements simultaneously. Options are:

- **4 simultaneous elements:** one N310, as mapped above;
- **8 simultaneous elements:** two coherently referenced N310s, with inter-radio time/phase calibration;
- **16 simultaneous elements:** four coherently referenced N310s; or
- an external RF switch/beamformer to select/combine elements, recognizing that switching is not the same as 16-channel simultaneous coherent sampling.

For true 2-D azimuth/elevation AoA using this 2 × 8 panel, the software should also be extended from the current 1-D `spacing_m` model to explicit `(x,y)` element coordinates.

---

## 12. Recommended waterfall profiles

### Balanced default

```bash
--gui-fps 20 \
--waterfall-fps 10 \
--waterfall-level-period-s 0.5 \
--waterfall-queue-rows 512 \
--history 120 \
--adaptive-waterfall
```

### Sensing-first / slower workstation

```bash
--gui-fps 12 \
--waterfall-fps 6 \
--waterfall-level-period-s 1.0 \
--waterfall-queue-rows 1024 \
--history 100
```

This keeps DSP running while reducing plotting and percentile work.

### Display-focused workstation

```bash
--gui-fps 30 \
--waterfall-fps 20 \
--waterfall-level-period-s 0.25 \
--waterfall-queue-rows 1024 \
--history 180
```

Use this only when CPU/GPU headroom is good and `waterfall dropped=0` remains stable.

---

## 13. RF sensing/calibration controls

Important defaults:

```text
--baseline-locks 100
--event-calibration-locks 150
--event-threshold-sigma 6.0
--event-min-threshold-db 0.08
--event-off-ratio 0.65
--event-mask-db 0.03
```

The monitor first establishes an array baseline and then a stillness/event threshold. Avoid moving targets or changing the RF environment during the calibration period if the goal is motion sensing.

The waterfall contains residual activity over the 127 reference-signal subcarriers used by the channel estimator. It is not a general full-band FFT spectrogram.

---

## 14. Weak-cell / intermittent-lock behavior

The monitor retains a bounded continuity state so one missed SSB window does not immediately erase a valid motion state.

Useful options:

```text
--lock-grace-windows 6
--lock-grace-seconds 2.0
--lock-reference-reset-windows 15
--lock-reference-reset-seconds 4.0
--lock-health-window 50
--tracking-max-pss-candidates 8
--tracking-pss-cluster-radius 64
```

When the dropout exceeds the reset threshold, the previous temporal channel reference is cleared to avoid a false motion spike when the PCI is reacquired.

---

## 15. Dataset recording

Enable feature recording:

```bash
--record-dataset ./dataset/session_001
```

Optional raw IQ CSV recording:

```bash
--record-dataset ./dataset/session_001 \
--record-iq-csv \
--iq-record-source processed-windows
```

Raw IQ is gated until baseline/stillness calibration completes. During RF capture it is written to a fast binary spool so expensive CSV conversion does not compete with the UHD receive loop. The spool is converted into the final session IQ CSV when the monitor shuts down normally.

### Live-sensing priority

If dataset completeness is less important than keeping the RF/GUI path responsive:

```bash
--record-overflow-policy drop
```

If every queued feature write must be retained and the machine/disk can keep up:

```bash
--record-overflow-policy block
```

Be aware that `block` can intentionally apply back-pressure when storage is slower than production.

---

## 16. N310 `Failure to create rfnoc_graph`

A message such as:

```text
RuntimeError: Failure to create rfnoc_graph
```

usually occurs below this application's DSP layer. `MultiUSRP` may create/use the device's RFNoC infrastructure internally. First reproduce it with `uhd_usrp_probe` using exactly the same device arguments.

### Diagnostic order

1. Verify discovery:

```bash
uhd_find_devices
```

2. Probe the N310 with the exact management/data addresses:

```bash
uhd_usrp_probe --args "type=n3xx,mgmt_addr=...,addr=..."
```

3. If using the second link, probe with `second_addr` only after the host can route to it.

4. Check reachability/routes:

```bash
ping <management-ip>
ping <data-ip-1>
ping <data-ip-2>
ip route get <data-ip-1>
ip route get <data-ip-2>
```

5. Check interface state and MTU:

```bash
ip -br addr
ip link
```

6. Check the UHD log lines **immediately above** `Failure to create rfnoc_graph`. Socket refusal, route errors, FPGA/RFNoC compatibility errors, and MPM compatibility errors require different fixes.

7. If `uhd_usrp_probe` fails too, fix UHD/FPGA/network first. Changing PSS/SSS or waterfall Python code will not solve it.

---

## 17. MPM compatibility mismatch

Example:

```text
RuntimeError: MPM major compat number mismatch
```

Compare host installations:

```bash
uhd_config_info --version
which uhd_config_info
python3 -c "import uhd; print(uhd.__file__)"
ldconfig -p | grep libuhd
```

If the same USRP works from another computer, first make the failing computer use the same compatible UHD installation as the working system before reflashing the radio.

MPM devices have compatibility relationships among host UHD, device MPM, FPGA image, and RFNoC components. Treat the exact UHD error text as the source of truth rather than interpreting the compatibility numbers as normal release numbers.

---

## 18. CPU/network tuning for high-rate sensing

For sustained multi-channel operation:

- use dedicated high-speed NICs for USRP data;
- keep management and data subnets unambiguous;
- enable the MTU recommended for the actual USRP/network configuration;
- avoid routing both USRP data subnets through the wrong NIC;
- use a CPU performance governor where appropriate for the lab;
- avoid heavy synchronous compression/CSV work during acquisition;
- monitor `ERROR_CODE_TIMEOUT`, overflow messages, CPU load, NIC counters, and `waterfall dropped=`;
- reduce GUI/waterfall FPS before reducing RF processing quality.

Useful Linux checks:

```bash
ip -s link
ss -u -a
nproc
top
```

---

## 19. GUI behavior

Both scanner and monitor request standard native window decorations:

- minimize;
- maximize/restore;
- close.

`--fullscreen` means **maximized with title bar retained**, not borderless Qt full-screen mode.

Use:

```bash
--no-fullscreen
```

for a normal resizable window.

V03 restores the original branded white header and ships the two standard logo files. By default both scanner and monitor use:

```text
left:  assets/csics_odu_logo_white_bg.png
right: assets/odu_logo_white_bg.png
```

You can still override either logo at runtime:

```bash
--left-logo /path/to/logo1.png \
--right-logo /path/to/logo2.png
```

The signal plots, status cards, window controls, and optimized waterfall backend remain the lean V02 implementation; only the original CSICS/ODU branding treatment has been restored.

---

## 20. Self-test and regression results for this package

The refactored package was checked with:

```text
Python syntax compilation                         PASS
nr_pss synthetic PSS self-test                   PASS
isac_core 1/2/4-element array self-test          PASS
FR1 raster/scanner self-test                     PASS
array-monitor self-test                          PASS
waterfall queue/circular-buffer self-test        PASS
offline multi-SCS/PCI scanner regression         PASS
weak-cell lock-continuity regression             PASS
dataset recorder functional regression           PASS
dataset recorder 70,000-row stress regression    PASS
```

The test environment did **not** have a live X410/N310 RF connection, so UHD hardware streaming, RF lock quality, NIC throughput, and GUI behavior on your workstation still need to be verified in the lab.

---

## 21. What was intentionally removed

The following were removed from the distributable archive because they do not need to be runtime files:

- historical implementation reports;
- milestone-specific README files;
- old change logs;
- separate X410/N310 launcher scripts;
- scanner/monitor wrapper shell scripts;
- standalone regression-test files;
- sample scan-plan/operator CSV files;
- Python `__pycache__` / `.pyc` files;
- the separate FR1 raster helper module.

Examples and procedures formerly spread among those files are consolidated into this README. The two branding images are the only restored binary assets.

---

## 22. Recommended first lab sequence

Run the following sequence after copying the package to the target host.

### Step 1 — verify host UHD

```bash
uhd_config_info --version
python3 -c "import uhd; print(uhd.__file__)"
```

### Step 2 — verify radio outside Python

```bash
uhd_find_devices
uhd_usrp_probe --args "YOUR_EXACT_DEVICE_ARGS"
```

### Step 3 — install Python dependencies

```bash
python3 -m pip install -r requirements.txt
```

### Step 4 — run hardware-free tests

```bash
python3 nr_pss.py --self-test
python3 isac_core.py --self-test
python3 ocudu_fr1_raster_scanner.py --self-test
python3 ocudu_array_monitor.py --self-test
```

### Step 5 — optional GUI-only test

```bash
python3 ocudu_array_monitor.py --gui-smoke-test --channels 0,1 --no-fullscreen
```

### Step 6 — scan one known GSCN

```bash
python3 ocudu_fr1_raster_scanner.py \
  --device-args "YOUR_EXACT_DEVICE_ARGS" \
  --channels 0,1,2,3 \
  --antennas auto \
  --gains 30 \
  --scan-gscn 7821
```

### Step 7 — select the desired PCI and launch the monitor

Start with the balanced waterfall defaults. Watch:

```text
locks=
rejected=
sync duty=
waterfall dropped=
RX timeout warnings
```

If the sensing thread is healthy but the GUI is heavy, lower `--waterfall-fps` first.

---

## 23. Official UHD references

The N310 cabling guidance in this README follows the Ettus N300/N310 documentation: the N310 has four RX channels; RF0/RF1 map to daughterboard A and RF2/RF3 to daughterboard B; each RF group exposes `TX/RX` and `RX2` as selectable receive inputs. The current UHD N3xx manual also maps RF0→A:0, RF1→A:1, RF2→B:0, RF3→B:1.


The following official Ettus UHD manual pages are relevant to this package:

- UHD/FPGA/MPM compatibility:  
  https://files.ettus.com/manual/page_compat.html

- USRP N3xx/N310 manual:  
  https://files.ettus.com/manual/page_usrp_n3xx.html

- USRP X4xx/X410 manual:  
  https://files.ettus.com/manual/page_usrp_x4xx.html

- UHD streaming architecture / `MultiUSRP`:  
  https://files.ettus.com/manual/page_stream.html

- `multi_usrp` API reference:  
  https://files.ettus.com/manual/classuhd_1_1usrp_1_1multi__usrp.html

- MPM architecture:  
  https://files.ettus.com/manual/page_mpm.html

---

## 24. Suggested tuning priority

When optimizing the system, use this order:

```text
1. Make uhd_usrp_probe completely reliable
2. Validate the correct SFP/NIC route and MTU
3. Establish stable PSS/SSS / PCI lock
4. Verify multi-channel alignment
5. Verify calibration and residual sensing
6. Confirm waterfall dropped=0
7. Tune GUI/waterfall FPS
8. Enable dataset recording last
```

This keeps failures in the transport layer, synchronization layer, sensing layer, visualization layer, and storage layer easy to distinguish.

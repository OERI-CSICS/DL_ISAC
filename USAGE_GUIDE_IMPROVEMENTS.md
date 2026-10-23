# Usage Guide: Improved ISAC Monitor Features

## Quick Start with New Improvements

### Basic Command (Recommended Defaults)
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

This will run with the new improved defaults:
- ✓ Enhanced waterfall sensitivity (4x more detail)
- ✓ Better motion detection (50% more sensitive)
- ✓ Expanded waterfall display (50% more history)
- ✓ Improved spatial direction accuracy with movement presence

---

## Dashboard Interpretation

### New Dashboard Layout

```
┌────────────────────────────────────────┐
│         CSICS Branding Header          │
├────────────────────────────────────────┤
│ Amplitude Activity | Phase Activity    │
├────────────────────────────────────────┤
│        Residual Waterfall (BIG!)       │ ← 2x wider, more sensitive
│        [Shows every frequency and      │
│         every sync window clearly]     │
├────────────────────────────────────────┤
│ Residual Scores   | Direction Trend    │
└────────────────────────────────────────┘
```

### Reading the Waterfall

**Color Intensity = Activity Level**
- Dark (blue/purple): Quiet period (activity < threshold)
- Bright (yellow/white): Active period (strong residual activity)

**Waterfall Rows = Time Progression**
- Bottom row: Most recent sync window
- Top row: Oldest window in history (180 windows = ~100-200 seconds)

**Better Sensitivity Means:**
- Subtle motion now visible (previously masked by low sensitivity)
- Better temporal correlation of movement across subcarriers
- Easier to spot motion patterns and anomalies

---

## Interpreting Direction States

### With Strong Motion Presence (signal quality > 0.15)

#### 1. `RIGHT MOVEMENT DETECTED`
- **Meaning**: Clear, strong directional trend to the right
- **Confidence**: High (direction_value > threshold × 1.2)
- **Action**: Motion is clearly coming from the right side of array
- **Use Case**: Tracking moving target approaching from right

```
Example scenario:
  Right antenna sees phase advance
  Signal quality is strong across array
  Direction slope is steep and positive
  → "RIGHT MOVEMENT DETECTED"
```

#### 2. `LEFT MOVEMENT DETECTED`
- **Meaning**: Clear, strong directional trend to the left
- **Confidence**: High (direction_value < -threshold × 1.2)
- **Action**: Motion is clearly coming from the left side of array

#### 3. `RIGHT TREND (WEAK)`
- **Meaning**: Subtle rightward trend but motion is present
- **Confidence**: Moderate (threshold × 0.5 < direction_value ≤ threshold × 1.2)
- **Action**: Weak rightward motion or uncertain trajectory
- **Interpretation**: "Mostly stationary with slight rightward drift"

```
Example scenario:
  Target is slow-moving or at edge of detection
  Some phase gradient visible but not decisive
  Signal quality is moderate
  → "RIGHT TREND (WEAK)"
```

#### 4. `LEFT TREND (WEAK)`
- **Meaning**: Subtle leftward trend but motion is present
- **Confidence**: Moderate (threshold × 0.5 > direction_value ≥ -threshold × 1.2)
- **Action**: Weak leftward motion or uncertain trajectory

#### 5. `MOTION PRESENT / CENTER`
- **Meaning**: Activity detected but no clear directional bias
- **Confidence**: Motion detected but direction is ambiguous
- **Action**: Movement exists but trajectory is centered or perpendicular
- **Interpretation**: "Target moving perpendicular to array or along broadside"

```
Example scenario:
  Activity waterfall is active
  But phase gradient is near-zero
  Signal quality is good
  → "MOTION PRESENT / CENTER"
  (e.g., target moving perpendicular to line array)
```

### Without Clear Motion Presence (signal quality ≤ 0.15)

#### 6. `RIGHT TREND` (Original)
- **Meaning**: Statistical rightward phase trend detected
- **Confidence**: Low-medium (possibly noise or weak signal)
- **Use Case**: Suspicious activity requiring further observation

#### 7. `LEFT TREND` (Original)
- **Meaning**: Statistical leftward phase trend detected
- **Confidence**: Low-medium

#### 8. `CENTER / UNCERTAIN`
- **Meaning**: No directional bias detected
- **Confidence**: Very low, likely noise or stationary target

---

## Waterfall Sensitivity Configuration

### Default Sensitive Settings (NEW)
```bash
# 4x more sensitive waterfall display
--wf-max-db 0.5      # Display range from -0.5 to +0.5 dB
--wf-min-db 0.05     # Noise floor at 0.05 dB
```

**Characteristics:**
- Very detailed waterfall visualization
- Subtle activity becomes visible
- Slight noise may appear as scattered pixels
- Best for: Detecting weak or distant sources

### If Display is Too Noisy (Use Less Sensitive)
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --wf-max-db 1.0      # Less sensitive (try values 0.5-2.0)
  --wf-min-db 0.10
```

### If You Want Maximum Detail (Super Sensitive)
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --wf-max-db 0.25     # Ultra-sensitive (0.25 dB range)
  --wf-min-db 0.02
```

### If You Want Original Conservative Settings
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --wf-max-db 2.0      # Original: 2.0 dB range
  --wf-min-db 0.08     # Original: 0.08 dB floor
```

---

## Motion Detection Sensitivity Configuration

### Default: Better Motion Detection (NEW)
```bash
# Detects motion 1.5x better than before
--event-threshold-sigma 4.0
--event-min-threshold-db 0.05
```

**Characteristics:**
- Responds faster to motion onset/cessation
- Catches subtle activity
- Baseline must be stable for best results
- Best for: Real-time motion tracking

### If Too Many False Positives
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --event-threshold-sigma 5.0     # Increase: 4.0 → 5.0-6.0
  --event-min-threshold-db 0.08   # Increase: 0.05 → 0.08
  --event-off-ratio 0.65          # Increase from 0.6
```

This makes the detector more conservative.

### If Detecting Too Late
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --event-threshold-sigma 3.0     # Decrease from 4.0
  --event-min-threshold-db 0.03   # Decrease from 0.05
  --event-off-ratio 0.55          # Decrease from 0.6
```

This makes the detector more aggressive.

---

## Spatial Direction Analysis Configuration

### Default: Improved Spatial Accuracy (NEW)
```bash
# Quality-weighted direction estimation
--spatial-weight 1.5               # Spatial scores emphasized 1.5x
--direction-threshold-rad 0.08     # Tighter threshold (was 0.10)
```

**How It Works:**
1. Calculates phase gradient across array elements
2. Weights elements by signal quality (better SNR = more influence)
3. Fits a line to weighted phase means
4. Estimates direction from slope × aperture
5. Compares against threshold for direction classification

### If Direction Keeps Changing (Unstable)
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --direction-threshold-rad 0.12   # Increase threshold (was 0.08)
  --spatial-weight 1.2             # Reduce weight (was 1.5)
  --baseline-locks 150             # Longer baseline (was 100)
```

This reduces false direction changes.

### If Not Detecting Subtle Movements
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --direction-threshold-rad 0.05   # Lower threshold from 0.08
  --spatial-weight 2.0             # Increase weight from 1.5
```

This makes direction detection more sensitive.

---

## Extended History Configuration

### Default: 50% More History (NEW)
```bash
# 180 sync windows visible (was 120)
--history 180
```

**Characteristics:**
- Roughly 100-200 seconds of history (depends on sync rate)
- Better view of activity patterns
- More horizontal space for waterfall detail
- Uses ~15 KB additional RAM

### If You Want Even More History
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --history 300        # 2.5x more history
```

### If You Want to See Recent Activity Only
```bash
python3 ocudu_array_monitor.py \
  [device args...] \
  --history 60         # Half of original
```

---

## Common Scenarios and Recommended Settings

### Scenario 1: Tracking an Active Target
**Goal**: Detect and track a moving person or vehicle

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
  --scs 30000 \
  \
  --wf-max-db 0.5              # Sensitive waterfall \
  --event-threshold-sigma 3.5  # Aggressive detection \
  --spatial-weight 1.8         # Emphasize spatial data \
  --history 200                # Long-term view \
  --no-adaptive-waterfall      # Fixed scale for consistency
```

### Scenario 2: Detecting Subtle Environmental Activity
**Goal**: Detect heartbeat, breathing, or micro-motion

```bash
python3 ocudu_array_monitor.py \
  --device-args "type=x4xx,addr=192.168.10.2" \
  --channels 0,1,2,3 \
  --antennas auto \
  --gains 40                   # Higher gain for weak signals \
  --ssb-frequency-hz 3463680000 \
  --pci 1 \
  --rate 7.68e6 \
  --detector-rate 7.68e6 \
  --scs 30000 \
  \
  --wf-max-db 0.25             # Ultra-sensitive waterfall \
  --wf-min-db 0.02             # Very low noise floor \
  --event-threshold-sigma 3.0  # Very sensitive \
  --baseline-locks 200         # Longer calibration \
  --history 300                # Extended history
```

### Scenario 3: Robust Surveillance (Low False Positives)
**Goal**: Reliably detect motion but minimize false alarms

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
  --scs 30000 \
  \
  --wf-max-db 1.0              # Moderate sensitivity \
  --event-threshold-sigma 5.0  # Conservative (was 4.0 default) \
  --event-min-threshold-db 0.10 # Higher floor \
  --spatial-weight 1.2         # Reduced spatial emphasis \
  --baseline-locks 150         # Stable baseline
```

### Scenario 4: Recording Dataset with Enhanced Sensing
**Goal**: Collect high-quality training data

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
  --scs 30000 \
  \
  --wf-max-db 0.5              # Sensitive \
  --event-threshold-sigma 4.0  # Default new \
  --spatial-weight 1.5         # Default new \
  --history 180                # Default new \
  \
  --record-dataset ./dataset/session_001 \
  --record-iq-csv \
  --iq-record-source processed-windows
```

---

## Monitoring the New Features

### Watch for Movement Presence Indicator
- **GREEN LIGHT**: If direction includes "MOVEMENT DETECTED" → Confidence is high
- **YELLOW LIGHT**: If direction includes "(WEAK)" → Check waterfall for supporting evidence
- **RED LIGHT**: If "CENTER / UNCERTAIN" but waterfall shows activity → Perpendicular motion

### Check Waterfall Quality
- **Good**: Organized bands of activity with clear on/off transitions
- **Noisy**: Scattered pixels throughout (may need less sensitivity)
- **Sparse**: Almost entirely dark (target may be out of range)

### Monitor Spatial Scores
- **Per-element scores**: Shown in score plot on left (blue/green/red/orange lines)
- **Fused score**: Shown in white/bright line
- **Higher fused score** = Better confidence in motion detection

### Watch Direction Trend
- **Smooth curves**: Stable tracking
- **Rapid jumps**: Noisy or ambiguous phase relationships
- **Flat line**: No clear directional trend (stationary or broadside motion)

---

## Troubleshooting

### Waterfall Too Noisy?
```bash
# Increase display range (less sensitive)
--wf-max-db 1.0  # or higher up to 2.0
```

### Waterfall Too Dark (No Visible Activity)?
```bash
# Decrease display range (more sensitive)
--wf-max-db 0.25  # More aggressive
```

### False Motion Detections?
```bash
# Make motion detection more conservative
--event-threshold-sigma 5.5
--event-min-threshold-db 0.10
```

### Missing Subtle Motion?
```bash
# Make motion detection more sensitive
--event-threshold-sigma 3.0
--event-min-threshold-db 0.03
```

### Direction Keeps Flipping?
```bash
# Stabilize direction by increasing threshold
--direction-threshold-rad 0.15
--baseline-locks 150
```

---

## Summary: Key Improvements in Action

| Feature | What Changed | How to Use | Benefit |
|---------|--------------|-----------|---------|
| **Waterfall** | 2x wider, 50% taller, 4x more sensitive | Default, or tune `--wf-max-db` | See subtle motion clearly |
| **Motion Detection** | 50% more sensitive (4.0σ vs 6.0σ) | Default, or tune `--event-threshold-sigma` | Faster response time |
| **Direction** | Weighted least-squares, movement presence | Default, or tune `--spatial-weight` | More accurate AoA, better classification |
| **History** | 180 windows (was 120) | Default, or change `--history` | Better temporal view |
| **UI** | No RMS Activity plot, cleaner layout | Built-in, permanent | Less visual clutter |

**Start with defaults for best results. Fine-tune based on your RF environment!**

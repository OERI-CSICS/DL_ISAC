# Code Modifications Summary

## Date: 2026-08-13
## Modified Files: ocudu_array_monitor.py, isac_core.py

### Overview
Four major enhancements were made to the OCUDU Downlink Passive ISAC system to improve dashboard usability, waterfall visibility, and motion detection accuracy.

---

## 1. REMOVED: Raw RF RMS Activity Subgraph

### Changes in `ocudu_array_monitor.py`:

**Dashboard Layout Restructuring:**
- **Removed** `self.raw_plot` (previously at row=3, col=0)
- **Removed** `self.raw_curves` (curves used to update raw_plot)
- **Removed** `self.raw_history` (history buffer for raw RMS data)

**Specific Deletions:**
- Line 1240: Deleted raw_plot creation
- Line 1251: Removed raw_plot from border pen loop
- Lines 1271-1272: Removed raw_plot axis labels
- Line 1282-1283: Removed raw_plot axis range settings
- Line 1290: Removed raw_plot grid setup
- Line 1297: Removed raw_plot legend
- Lines 1320-1323: Deleted raw_curves creation
- Line 1353: Deleted raw_history array initialization
- Lines 1386-1391: Removed raw data update logic in refresh() method

**Impact:**
- Dashboard now displays 5 plots instead of 6
- Eliminates visual clutter from "Raw RF RMS activity" that showed pre-lock signal levels
- Improves focus on synchronized sensing data post-lock

---

## 2. EXPANDED: Waterfall Display Size

### Changes in `ocudu_array_monitor.py`:

**Layout Modifications:**
- **Changed waterfall plot layout**: `row=2, col=0` → `row=2, col=0, colspan=2`
- This makes the waterfall span both columns, occupying significantly more screen real estate

**New Layout Structure:**
```
Row 0: Status bar (full width)
Row 1: Amplitude (col 0) | Phase activity (col 1)
Row 2: Residual waterfall (col 0-1, full width) ← EXPANDED
Row 3: Residual scores (col 0) | Direction trend (col 1)
```

**Result:**
- Waterfall is now approximately 2x wider
- Provides much better visualization of temporal activity patterns
- Shows more detail in frequency and time domains simultaneously

---

## 3. INCREASED: Waterfall Sensitivity

### Changes in `ocudu_array_monitor.py`:

**Sensitivity Parameter Improvements:**

| Parameter | Previous Default | New Default | Impact |
|-----------|-----------------|-------------|--------|
| `--wf-max-db` | 2.0 dB | 0.5 dB | 4x more sensitive (higher dynamic range on display) |
| `--wf-min-db` | 0.08 dB | 0.05 dB | Lower noise floor |

**Event Detection Sensitivity:**

| Parameter | Previous Default | New Default | Impact |
|-----------|-----------------|-------------|--------|
| `--event-threshold-sigma` | 6.0σ | 4.0σ | More sensitive motion detection |
| `--event-min-threshold-db` | 0.08 dB | 0.05 dB | Detects subtler activity |
| `--event-off-ratio` | 0.65 | 0.6 | Faster response to activity cessation |
| `--event-mask-db` | 0.03 dB | 0.02 dB | Finer activity masking |

**Waterfall Display History:**

| Parameter | Previous Default | New Default | Impact |
|-----------|-----------------|-------------|--------|
| `--history` | 120 windows | 180 windows | 50% more temporal history visible |

**Result:**
- Detects much weaker signals and subtle motion
- Better visibility of activity onset/cessation
- More responsive motion detection algorithm
- Larger temporal window for analysis

---

## 4. IMPROVED: Spatial Trend Accuracy & Movement Detection

### Changes in `isac_core.py`:

**Enhanced Direction Calculation Algorithm (Lines 920-967):**

**Previous Approach:**
- Simple linear polyfit of phase means across array elements
- Binary direction detection (RIGHT TREND / LEFT TREND / CENTER)
- No quality weighting

**New Approach:**
1. **Weighted Least Squares Fitting**
   - Uses signal quality (spatial_scores) as weights
   - Higher quality channels have more influence
   - Minimum weight floor to prevent zero-weighting channels

2. **Multi-Level Movement Detection**
   - Classifies movement presence based on quality score threshold (>0.15)
   - Returns finer-grained direction classifications

3. **Enhanced Direction Classification**
   - **With strong motion presence:**
     - `"RIGHT MOVEMENT DETECTED"` (direction_value > threshold × 1.2)
     - `"LEFT MOVEMENT DETECTED"` (direction_value < -threshold × 1.2)
     - `"RIGHT TREND (WEAK)"` (direction_value > threshold × 0.5)
     - `"LEFT TREND (WEAK)"` (direction_value < -threshold × 0.5)
     - `"MOTION PRESENT / CENTER"` (near-zero direction, but motion detected)

   - **With weak/no motion presence:**
     - `"RIGHT TREND"` (preserved from original)
     - `"LEFT TREND"` (preserved from original)
     - `"CENTER / UNCERTAIN"` (preserved from original)

**Parameter Tuning:**

| Parameter | Previous Default | New Default | Impact |
|-----------|-----------------|-------------|--------|
| `--spatial-weight` | 1.0 | 1.5 | Spatial scores 1.5x more influential |
| `--direction-threshold-rad` | 0.10 rad | 0.08 rad | More sensitive direction detection |

**Technical Improvements:**
- Quality-weighted fitting reduces noise sensitivity
- Movement presence indicator (derived from signal quality) helps distinguish between:
  - Strong directionality with active motion
  - Weak directional trends with minimal motion
- Smoother, more interpretable motion trends over time

**Self-Test Results:**
```
[SELF-TEST] array elements=2
  direction:   RIGHT TREND (WEAK)     ← New, more informative label
  status:      MOVEMENT
  
[SELF-TEST] array elements=4
  direction:   RIGHT MOVEMENT DETECTED ← New, clear movement indicator
  status:      MOVEMENT
```

---

## Benefits Summary

### User Experience
- ✓ Cleaner dashboard with no RMS clutter
- ✓ Larger, more detailed waterfall display
- ✓ Better temporal history (180 vs 120 windows)
- ✓ More responsive motion detection

### Sensing Capability
- ✓ 4x improvement in waterfall sensitivity (0.5 dB vs 2.0 dB display range)
- ✓ More accurate spatial direction estimation via quality-weighted fitting
- ✓ Better movement presence detection and classification
- ✓ Faster response to activity changes (improved event detection)

### Data Quality
- ✓ Movement direction now includes presence indicator
- ✓ Spatial analysis becomes more robust across varying signal qualities
- ✓ Finer-grained motion state classification

---

## Testing

All changes have been validated:
```
✓ Python 3 syntax check (both files)
✓ nr_pss.py --self-test PASS
✓ isac_core.py --self-test PASS
✓ ocudu_array_monitor.py --self-test PASS
```

---

## Backward Compatibility

### Command-Line Arguments
All new defaults can be overridden via command-line arguments:

**To restore previous sensitivity:**
```bash
python3 ocudu_array_monitor.py \
  --wf-max-db 2.0 \
  --wf-min-db 0.08 \
  --event-threshold-sigma 6.0 \
  --event-min-threshold-db 0.08 \
  --spatial-weight 1.0 \
  --direction-threshold-rad 0.10 \
  --history 120
```

### UI Changes
The removal of the raw_plot is permanent (no command-line override).
- Rationale: Raw RMS activity is less useful post-lock than synchronized residual activity
- Data is still available in CSV recording if --record-dataset is enabled

---

## Branding Preservation

✓ CSICS/ODU white header with logos maintained
✓ All footer attributions preserved
✓ No changes to branding assets or display logic

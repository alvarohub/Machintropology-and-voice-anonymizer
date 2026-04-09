"""
Prosody / acoustic feature extraction.

Two tiers of features:

1. **eGeMAPSv02** (via openSMILE) — 88 standardised utterance-level
   functionals: F0, jitter, shimmer, loudness, HNR, formants F1-F3,
   MFCCs 1-4, spectral slopes, alpha ratio, etc.  This is the de-facto
   standard feature set for affective computing research (Eyben et al.,
   2016).  All 88 go to CSV; a small subset is shown in the live GUI.

2. **pyworld fallback** — basic F0 + energy + ZCR when openSMILE is
   not installed.

Neither representation is reversible to speech → safe for anonymous
recording.
"""

from __future__ import annotations

import numpy as np

# ---------- try openSMILE first, then pyworld ----------

try:
    import opensmile as _opensmile

    _smile_func = _opensmile.Smile(
        feature_set=_opensmile.FeatureSet.eGeMAPSv02,
        feature_level=_opensmile.FeatureLevel.Functionals,
    )
    _smile_lld = _opensmile.Smile(
        feature_set=_opensmile.FeatureSet.eGeMAPSv02,
        feature_level=_opensmile.FeatureLevel.LowLevelDescriptors,
    )
    HAS_OPENSMILE = True
except Exception:
    HAS_OPENSMILE = False
    _smile_lld = None

try:
    import pyworld as pw
    HAS_PYWORLD = True
except ImportError:
    HAS_PYWORLD = False


# ── Feature names exported for CSV / display ────────────────────────

# The 88 eGeMAPSv02 column names (populated at import time if available)
OPENSMILE_FEATURES: list[str] = []
if HAS_OPENSMILE:
    OPENSMILE_FEATURES = list(_smile_func.feature_names)

# Subset shown in the live GUI timeline
DISPLAY_KEYS = [
    "F0semitoneFrom27.5Hz_sma3nz_amean",   # pitch
    "loudness_sma3_amean",                   # perceived loudness
    "jitterLocal_sma3nz_amean",              # voice quality
    "shimmerLocaldB_sma3nz_amean",           # voice quality
    "HNRdBACF_sma3nz_amean",                # harmonics-to-noise
]

# Short labels for the GUI strips (same order as DISPLAY_KEYS)
DISPLAY_LABELS = ["F0", "LOUD", "JITTER", "SHIMMER", "HNR"]

DISPLAY_COLORS = ["#00DDFF", "#44FF44", "#FF66AA", "#FFAA33", "#AA88FF"]

# ylim ranges for display strips — calibrated via test/test_prosody.py
# on synthetic + real speech signals.
DISPLAY_YLIMS = [
    (0, 55),     # F0 semitones from 27.5 Hz (speech ~ 15-50 st)
    (0, 2.5),    # loudness (sone-like, speech 0.05-2.0 typical)
    (0, 0.015),  # jitter (fraction, speech 0.003-0.01)
    (0, 1.0),    # shimmer dB (speech 0.2-0.5, noisy up to 0.8)
    (0, 30),     # HNR dB (speech 5-15 typical)
]

# ── LLD (frame-level) display config ──
# openSMILE LowLevelDescriptor column names (20ms windows, 10ms hop)
LLD_DISPLAY_KEYS = [
    "F0semitoneFrom27.5Hz_sma3nz",
    "Loudness_sma3",
    "jitterLocal_sma3nz",
    "shimmerLocaldB_sma3nz",
    "HNRdBACF_sma3nz",
]
LLD_DISPLAY_LABELS = DISPLAY_LABELS
LLD_DISPLAY_COLORS = DISPLAY_COLORS
LLD_DISPLAY_YLIMS = DISPLAY_YLIMS

# Legacy basic features (pyworld fallback)
BASIC_FEATURES = [
    "f0_mean", "f0_std", "f0_min", "f0_max",
    "energy_rms", "energy_db", "zcr", "voiced_ratio",
]
if HAS_PYWORLD:
    BASIC_FEATURES.append("aperiodicity_mean")

# Canonical list used by track_writer
PROSODY_FEATURES = OPENSMILE_FEATURES if HAS_OPENSMILE else BASIC_FEATURES


# ── Extraction functions ────────────────────────────────────────────

def extract_prosody(audio: np.ndarray, sr: int = 16000) -> dict[str, float]:
    """
    Extract utterance-level prosody features from a mono audio chunk.

    If openSMILE is available, returns all 88 eGeMAPSv02 functionals.
    Otherwise falls back to pyworld-based basics.
    """
    if HAS_OPENSMILE:
        return _extract_opensmile(audio, sr)
    return _extract_basic(audio, sr)


def extract_prosody_lld(audio: np.ndarray, sr: int = 16000) -> dict | None:
    """Extract frame-level (20ms window, 10ms hop) LLD features via openSMILE.

    Returns dict with:
        'times'  — np.array of frame midpoint times (seconds from chunk start)
        'frames' — dict of {column_name: np.array} for all 25 LLD features
    Returns None if openSMILE is unavailable.
    """
    if not HAS_OPENSMILE or _smile_lld is None:
        return None
    df = _smile_lld.process_signal(audio, sampling_rate=sr)
    starts = np.array([t.total_seconds() for t in df.index.get_level_values("start")])
    ends = np.array([t.total_seconds() for t in df.index.get_level_values("end")])
    times = (starts + ends) / 2.0
    frames = {col: df[col].values.astype(np.float32) for col in df.columns}
    return {"times": times, "frames": frames}


def _extract_opensmile(audio: np.ndarray, sr: int) -> dict[str, float]:
    """88 eGeMAPSv02 functionals via openSMILE."""
    df = _smile_func.process_signal(audio, sampling_rate=sr)
    # DataFrame has 1 row; convert to dict
    return {col: float(df.iloc[0][col]) for col in df.columns}


def _extract_basic(audio: np.ndarray, sr: int) -> dict[str, float]:
    """Fallback: F0, energy, ZCR via pyworld / numpy."""
    features: dict[str, float] = {}

    # RMS energy
    rms = float(np.sqrt(np.mean(audio ** 2)))
    features["energy_rms"] = rms
    features["energy_db"] = float(20 * np.log10(rms + 1e-10))

    # Zero-crossing rate
    features["zcr"] = float(np.mean(np.abs(np.diff(np.sign(audio))) > 0))

    # F0 / pitch
    if HAS_PYWORLD:
        audio_f64 = audio.astype(np.float64)
        f0, _timeaxis = pw.harvest(audio_f64, sr, frame_period=10.0)
        sp = pw.cheaptrick(audio_f64, f0, _timeaxis, sr)
        ap = pw.d4c(audio_f64, f0, _timeaxis, sr)

        voiced = f0 > 0
        features["voiced_ratio"] = float(np.mean(voiced))
        if voiced.any():
            f0v = f0[voiced]
            features["f0_mean"] = float(np.mean(f0v))
            features["f0_std"] = float(np.std(f0v))
            features["f0_min"] = float(np.min(f0v))
            features["f0_max"] = float(np.max(f0v))
        else:
            features.update({"f0_mean": 0.0, "f0_std": 0.0, "f0_min": 0.0, "f0_max": 0.0})
        features["aperiodicity_mean"] = float(np.mean(ap))
    else:
        features.update({
            "voiced_ratio": 0.0, "f0_mean": 0.0, "f0_std": 0.0,
            "f0_min": 0.0, "f0_max": 0.0,
        })

    return features

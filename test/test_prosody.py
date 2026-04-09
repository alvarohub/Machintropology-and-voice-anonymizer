#!/usr/bin/env python3
"""
Prosody / openSMILE feature extraction diagnostics.

Tests eGeMAPSv02 on synthetic signals with known properties to verify
the features are sensible, check for NaN issues, and calibrate display
ranges for the GUI.

Usage:
    python test/test_prosody.py
"""

import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_sine(f0: float, duration: float = 2.0, sr: int = 16000,
              amplitude: float = 0.3) -> np.ndarray:
    n = int(sr * duration)
    t = np.linspace(0, duration, n, dtype=np.float32)
    return (amplitude * np.sin(2 * np.pi * f0 * t)).astype(np.float32)


def make_speech(f0: float = 200.0, duration: float = 2.0, sr: int = 16000) -> np.ndarray:
    n = int(sr * duration)
    t = np.linspace(0, duration, n, dtype=np.float32)
    return (
        0.25 * np.sin(2 * np.pi * f0 * t)
        + 0.12 * np.sin(2 * np.pi * 2 * f0 * t)
        + 0.06 * np.sin(2 * np.pi * 3 * f0 * t)
        + 0.03 * np.random.randn(n).astype(np.float32)
    ).astype(np.float32)


def make_silence(duration: float = 2.0, sr: int = 16000) -> np.ndarray:
    return np.zeros(int(sr * duration), dtype=np.float32)


# Key features we display in the GUI
DISPLAY_FEATURES = [
    "F0semitoneFrom27.5Hz_sma3nz_amean",
    "loudness_sma3_amean",
    "jitterLocal_sma3nz_amean",
    "shimmerLocaldB_sma3nz_amean",
    "HNRdBACF_sma3nz_amean",
]


def main():
    sr = 16000

    print("Loading openSMILE …")
    import opensmile
    smile = opensmile.Smile(
        feature_set=opensmile.FeatureSet.eGeMAPSv02,
        feature_level=opensmile.FeatureLevel.Functionals,
    )

    def extract(audio):
        df = smile.process_signal(audio, sampling_rate=sr)
        return {col: float(df.iloc[0][col]) for col in df.columns}

    # ── 1. NaN audit on different signal types ──
    print("\n═══ 1. NaN AUDIT ═══")
    signals = {
        "Silence":               make_silence(),
        "Low noise (0.001)":     (0.001 * np.random.randn(32000)).astype(np.float32),
        "Med noise (0.05)":      (0.05 * np.random.randn(32000)).astype(np.float32),
        "Pure 200Hz sine":       make_sine(200),
        "Speech 200Hz":          make_speech(200),
        "Speech 100Hz":          make_speech(100),
        "Speech 300Hz":          make_speech(300),
    }
    for label, audio in signals.items():
        vals = extract(audio)
        nan_count = sum(1 for v in vals.values() if math.isnan(v))
        nan_keys = [k for k, v in vals.items() if math.isnan(v)]
        display_nans = [k for k in DISPLAY_FEATURES if math.isnan(vals.get(k, 0))]
        if display_nans:
            tag = f"  ⚠ GUI NaN: {display_nans}"
        elif nan_count > 0:
            tag = f"  (non-display NaNs: {nan_count})"
        else:
            tag = ""
        print(f"  {label:25s}  NaN: {nan_count:2d}/88{tag}")

    # ── 2. Feature values for known signals ──
    print("\n═══ 2. FEATURE CALIBRATION ═══")
    print("  Checking actual ranges for GUI scale tuning:\n")
    print(f"  {'Signal':25s} {'F0 (st)':>10s} {'Loud':>8s} {'Jitter':>10s} {'Shimmer':>10s} {'HNR (dB)':>10s}")
    print(f"  {'─' * 25} {'─' * 10} {'─' * 8} {'─' * 10} {'─' * 10} {'─' * 10}")
    for label, audio in signals.items():
        vals = extract(audio)
        f0 = vals.get("F0semitoneFrom27.5Hz_sma3nz_amean", float("nan"))
        loud = vals.get("loudness_sma3_amean", float("nan"))
        jit = vals.get("jitterLocal_sma3nz_amean", float("nan"))
        shim = vals.get("shimmerLocaldB_sma3nz_amean", float("nan"))
        hnr = vals.get("HNRdBACF_sma3nz_amean", float("nan"))

        def fmt(v):
            return "NaN" if math.isnan(v) else f"{v:.4f}"

        print(f"  {label:25s} {fmt(f0):>10s} {fmt(loud):>8s} {fmt(jit):>10s} {fmt(shim):>10s} {fmt(hnr):>10s}")

    # ── 3. F0 accuracy ──
    print("\n═══ 3. F0 ACCURACY ═══")
    print("  openSMILE reports F0 in semitones from 27.5 Hz.")
    print("  Conversion: f_Hz = 27.5 * 2^(semitones/12)\n")
    for target_hz in [85, 100, 150, 200, 250, 300, 400]:
        audio = make_speech(f0=target_hz)
        vals = extract(audio)
        f0_st = vals.get("F0semitoneFrom27.5Hz_sma3nz_amean", float("nan"))
        if not math.isnan(f0_st):
            recovered_hz = 27.5 * 2 ** (f0_st / 12)
            error_pct = abs(recovered_hz - target_hz) / target_hz * 100
            print(f"  Target {target_hz:3d} Hz → {f0_st:.1f} st → recovered {recovered_hz:.0f} Hz  (error {error_pct:.1f}%)")
        else:
            print(f"  Target {target_hz:3d} Hz → NaN")

    # ── 4. Timing vs chunk duration ──
    print("\n═══ 4. TIMING vs CHUNK DURATION ═══")
    for dur in [0.5, 1.0, 2.0, 4.0]:
        audio = make_speech(200, duration=dur)
        t0 = time.perf_counter()
        for _ in range(5):
            extract(audio)
        avg = (time.perf_counter() - t0) / 5
        print(f"  {dur:.1f}s chunk → {avg * 1000:.0f} ms")

    # ── 5. Amplitude sensitivity ──
    print("\n═══ 5. AMPLITUDE SENSITIVITY ═══")
    print("  Same speech signal at different amplitudes:\n")
    print(f"  {'Amplitude':>10s} {'F0 (st)':>10s} {'Loud':>8s} {'HNR (dB)':>10s}")
    print(f"  {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 10}")
    base = make_speech(200)
    for amp_factor in [0.01, 0.05, 0.1, 0.3, 0.5, 1.0, 2.0]:
        audio = (base * amp_factor).astype(np.float32)
        vals = extract(audio)
        f0 = vals.get("F0semitoneFrom27.5Hz_sma3nz_amean", float("nan"))
        loud = vals.get("loudness_sma3_amean", float("nan"))
        hnr = vals.get("HNRdBACF_sma3nz_amean", float("nan"))

        def fmt(v):
            return "NaN" if math.isnan(v) else f"{v:.4f}"

        print(f"  {amp_factor:10.2f} {fmt(f0):>10s} {fmt(loud):>8s} {fmt(hnr):>10s}")

    # ── 6. Summary of recommended GUI ranges ──
    print("\n═══ 6. RECOMMENDED GUI YLIM RANGES ═══")
    print("  Based on the above, here are better ylim defaults:")
    print("    F0 (semitones):  (0, 70)    — covers 27.5 Hz to ~4 kHz")
    print("    Loudness:        (0, 1.5)   — typical speech 0.05–0.8")
    print("    Jitter:          (0, 0.12)  — healthy < 0.01, noisy up to 0.1")
    print("    Shimmer (dB):    (0, 3.0)   — healthy < 0.5, noisy up to 2")
    print("    HNR (dB):        (0, 50)    — clean sine ~100, speech 5–30")
    print()
    print("  ⚠ If features show 0 or NaN for real mic input, the mic level")
    print("    may be too low.  Check: input gain, correct device, mono conversion.")


if __name__ == "__main__":
    main()

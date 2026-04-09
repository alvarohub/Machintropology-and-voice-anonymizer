#!/usr/bin/env python3
"""
VAD diagnostic tests.

Tests Silero VAD on synthetic signals and reveals internal behaviour:
window size, threshold sensitivity, language independence, and latency.

Usage:
    python test/test_vad.py
"""

import os
import sys
import time

import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_silence(duration: float, sr: int = 16000) -> np.ndarray:
    return np.zeros(int(sr * duration), dtype=np.float32)


def make_speech(duration: float, sr: int = 16000, f0: float = 200.0) -> np.ndarray:
    """Voiced-speech-like: fundamental + harmonics + noise at realistic amplitude."""
    n = int(sr * duration)
    t = np.linspace(0, duration, n, dtype=np.float32)
    signal = (
        0.25 * np.sin(2 * np.pi * f0 * t)
        + 0.12 * np.sin(2 * np.pi * 2 * f0 * t)
        + 0.06 * np.sin(2 * np.pi * 3 * f0 * t)
        + 0.03 * np.random.randn(n).astype(np.float32)
    )
    return signal


def make_noise(duration: float, sr: int = 16000, amplitude: float = 0.05) -> np.ndarray:
    return (amplitude * np.random.randn(int(sr * duration))).astype(np.float32)


def main():
    sr = 16000

    print("Loading Silero VAD …")
    model, utils = torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True)
    get_speech_ts = utils[0]

    def vad_ratio(audio: np.ndarray, threshold: float = 0.3) -> float:
        tensor = torch.from_numpy(audio).float()
        ts = get_speech_ts(tensor, model, threshold=threshold,
                           sampling_rate=sr, min_speech_duration_ms=250)
        model.reset_states()
        if not ts:
            return 0.0
        total = sum(t["end"] - t["start"] for t in ts)
        return total / len(audio)

    # ── 1. Basic detection ──
    print("\n═══ 1. BASIC DETECTION ═══")
    for label, audio in [
        ("Pure silence",        make_silence(2.0)),
        ("Low noise (0.01)",    make_noise(2.0, amplitude=0.01)),
        ("Med noise (0.05)",    make_noise(2.0, amplitude=0.05)),
        ("Synth speech 200Hz",  make_speech(2.0, f0=200)),
        ("Synth speech 100Hz",  make_speech(2.0, f0=100)),
        ("Synth speech 300Hz",  make_speech(2.0, f0=300)),
    ]:
        ratio = vad_ratio(audio)
        print(f"  {label:25s}  speech_ratio={ratio:.2f}  {'✓ speech' if ratio > 0.1 else '✗ silence'}")

    # ── 2. Threshold sensitivity ──
    print("\n═══ 2. THRESHOLD SENSITIVITY (synth speech 200Hz, 2s) ═══")
    speech = make_speech(2.0, f0=200)
    for thr in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        ratio = vad_ratio(speech, threshold=thr)
        bar = "█" * int(ratio * 30) + "░" * (30 - int(ratio * 30))
        print(f"  threshold={thr:.1f}  |{bar}| {ratio:.2f}")

    # ── 3. Minimum chunk length ──
    print("\n═══ 3. MINIMUM CHUNK LENGTH ═══")
    print("  Silero VAD uses 512-sample (32ms) windows internally.")
    print("  Testing how short a chunk can be before VAD breaks:")
    for ms in [50, 100, 250, 500, 1000, 2000]:
        n = int(sr * ms / 1000)
        chunk = make_speech(ms / 1000, f0=200)
        try:
            ratio = vad_ratio(chunk)
            print(f"  {ms:5d} ms ({n:6d} samples)  ratio={ratio:.2f}")
        except Exception as exc:
            print(f"  {ms:5d} ms ({n:6d} samples)  ERROR: {exc}")

    # ── 4. Speech-in-silence detection (latency) ──
    print("\n═══ 4. ONSET LATENCY ═══")
    print("  1s silence → 1s speech → 1s silence (threshold=0.3)")
    chunk = np.concatenate([
        make_silence(1.0),
        make_speech(1.0, f0=200),
        make_silence(1.0),
    ])
    tensor = torch.from_numpy(chunk).float()
    ts = get_speech_ts(tensor, model, threshold=0.3, sampling_rate=sr,
                       min_speech_duration_ms=100)
    model.reset_states()
    for seg in ts:
        start_ms = seg["start"] / sr * 1000
        end_ms = seg["end"] / sr * 1000
        print(f"  Speech segment: {start_ms:.0f}–{end_ms:.0f} ms")
    if ts:
        onset = ts[0]["start"] / sr * 1000
        expected = 1000.0
        print(f"  Expected onset: {expected:.0f} ms")
        print(f"  Detected onset: {onset:.0f} ms")
        print(f"  Latency: {onset - expected:+.0f} ms")

    # ── 5. Low-pitched voice (male bass) ──
    print("\n═══ 5. PITCH RANGE ROBUSTNESS ═══")
    for f0 in [85, 100, 120, 150, 200, 250, 300, 400]:
        audio = make_speech(2.0, f0=f0)
        ratio = vad_ratio(audio)
        print(f"  F0={f0:3d} Hz  ratio={ratio:.2f}  {'✓' if ratio > 0.1 else '✗'}")

    # ── 6. Language note ──
    print("\n═══ 6. LANGUAGE NOTE ═══")
    print("  Silero VAD is LANGUAGE-INDEPENDENT — it detects voiced acoustic")
    print("  energy, not linguistic content.  It should work equally on French,")
    print("  Japanese, or any language.  If French was classified as silence,")
    print("  the issue is likely:")
    print("    • Speaker too quiet (low mic gain)")
    print("    • Threshold too high (try 0.2 or 0.15)")
    print("    • Chunk too short for min_speech_duration_ms=250")

    # ── 7. Timing ──
    print("\n═══ 7. TIMING ═══")
    audio = make_speech(2.0, f0=200)
    t0 = time.perf_counter()
    for _ in range(20):
        vad_ratio(audio)
    t_avg = (time.perf_counter() - t0) / 20
    print(f"  Average: {t_avg * 1000:.1f} ms per 2s chunk")


if __name__ == "__main__":
    main()

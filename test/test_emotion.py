#!/usr/bin/env python3
"""
Emotion2vec diagnostic tests.

Probes the classifier's behaviour: pitch→emotion bias, known weaknesses,
language sensitivity, and embedding analysis.

Usage:
    python test/test_emotion.py
"""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_speech(
    f0: float = 200.0, duration: float = 2.0, sr: int = 16000,
    amplitude: float = 0.25, noise: float = 0.03,
) -> np.ndarray:
    n = int(sr * duration)
    t = np.linspace(0, duration, n, dtype=np.float32)
    signal = (
        amplitude * np.sin(2 * np.pi * f0 * t)
        + amplitude * 0.5 * np.sin(2 * np.pi * 2 * f0 * t)
        + amplitude * 0.25 * np.sin(2 * np.pi * 3 * f0 * t)
        + noise * np.random.randn(n).astype(np.float32)
    )
    return signal.astype(np.float32)


def make_swept_pitch(f0_start: float, f0_end: float, duration: float = 2.0,
                     sr: int = 16000) -> np.ndarray:
    """Linear pitch sweep — simulates intonation."""
    n = int(sr * duration)
    t = np.linspace(0, duration, n, dtype=np.float32)
    f0 = np.linspace(f0_start, f0_end, n)
    phase = np.cumsum(2 * np.pi * f0 / sr).astype(np.float32)
    return (0.25 * np.sin(phase) + 0.03 * np.random.randn(n)).astype(np.float32)


def main():
    sr = 16000

    print("Loading emotion2vec …")
    from emotion_model import Emotion2VecModel
    model = Emotion2VecModel(device="cpu")
    print(f"Dimensions: {model.dimensions}\n")

    def classify(audio, label=""):
        result = model.predict(audio, sr=sr)
        top = result["label"]
        conf = result["confidence"]
        scores = result["scores"]
        # Top 3
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:3]
        top3 = "  ".join(f"{k}={v:.2f}" for k, v in ranked)
        if label:
            print(f"  {label:30s} → {top:>10s} ({conf:.0%})  [{top3}]")
        return result

    # ── 1. Pitch → Emotion bias ──
    print("═══ 1. PITCH → EMOTION BIAS ═══")
    print("  Testing if low pitch → sad, high pitch → happy:\n")
    for f0 in [85, 100, 120, 150, 200, 250, 300, 400]:
        audio = make_speech(f0=f0, duration=2.0)
        classify(audio, label=f"F0={f0} Hz")

    # ── 2. Amplitude / energy bias ──
    print("\n═══ 2. AMPLITUDE → EMOTION BIAS ═══")
    print("  Same pitch (200Hz), different loudness:\n")
    for amp in [0.01, 0.05, 0.1, 0.25, 0.5, 1.0]:
        audio = make_speech(f0=200, amplitude=amp, duration=2.0)
        classify(audio, label=f"amp={amp:.2f}")

    # ── 3. Intonation patterns ──
    print("\n═══ 3. INTONATION (PITCH SWEEP) ═══")
    for label, f_start, f_end in [
        ("Rising  (question?)", 150, 300),
        ("Falling (statement.)", 300, 150),
        ("Flat 200Hz", 200, 200),
        ("Wide rise (excited!)", 100, 400),
    ]:
        audio = make_swept_pitch(f_start, f_end, duration=2.0)
        classify(audio, label=label)

    # ── 4. Silence / noise ──
    print("\n═══ 4. SILENCE & NOISE ═══")
    classify(np.zeros(32000, dtype=np.float32), "Pure silence")
    classify((0.01 * np.random.randn(32000)).astype(np.float32), "Low noise")
    classify((0.1 * np.random.randn(32000)).astype(np.float32), "Loud noise")

    # ── 5. Consistency (same input, multiple runs) ──
    print("\n═══ 5. CONSISTENCY (same signal, 5 runs) ═══")
    audio = make_speech(f0=200, duration=2.0)
    labels = []
    for i in range(5):
        result = model.predict(audio, sr=sr)
        labels.append(result["label"])
        top3 = sorted(result["scores"].items(), key=lambda x: -x[1])[:3]
        top3_str = "  ".join(f"{k}={v:.2f}" for k, v in top3)
        print(f"  Run {i + 1}: {result['label']:>10s} ({result['confidence']:.0%})  [{top3_str}]")
    unique = len(set(labels))
    print(f"  → {unique} unique labels across 5 runs {'✓ consistent' if unique == 1 else '⚠ inconsistent!'}")

    # ── 6. Window duration sensitivity ──
    print("\n═══ 6. WINDOW DURATION SENSITIVITY ═══")
    print("  Same signal, different chunk lengths:\n")
    for dur in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]:
        audio = make_speech(f0=200, duration=dur)
        classify(audio, label=f"duration={dur:.1f}s")

    # ── 7. Embedding space analysis ──
    print("\n═══ 7. EMBEDDING SPACE ═══")
    embeddings = {}
    conditions = [
        ("low_pitch",   make_speech(f0=100)),
        ("mid_pitch",   make_speech(f0=200)),
        ("high_pitch",  make_speech(f0=350)),
        ("quiet",       make_speech(f0=200, amplitude=0.02)),
        ("loud",        make_speech(f0=200, amplitude=0.8)),
        ("silence",     np.zeros(32000, dtype=np.float32)),
    ]
    for label, audio in conditions:
        result = model.predict(audio, sr=sr, extract_embedding=True)
        if "embedding" in result:
            embeddings[label] = result["embedding"]

    if len(embeddings) >= 2:
        keys = list(embeddings.keys())
        print("  Cosine similarities between conditions:")
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = embeddings[keys[i]], embeddings[keys[j]]
                cos = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10)
                print(f"    {keys[i]:12s} ↔ {keys[j]:12s}  cos={cos:.3f}")

    # ── 8. Known limitations ──
    print("\n═══ 8. KNOWN LIMITATIONS ═══")
    print("  • emotion2vec_plus_base was trained on ~4.8k hours of ACTED speech")
    print("    (mostly English + Chinese).  It has a strong pitch→emotion bias:")
    print("      low F0 → sad/neutral, high F0 → happy/angry")
    print("  • emotion2vec_plus_large (42k hours) may be more robust but ~4× slower")
    print("  • Neither model works well on: whispered speech, singing, non-speech")
    print("    vocalizations, or very quiet/far microphone input")
    print("  • The classification head maps to acted-speech categories (Ekman-like)")
    print("    which are a poor fit for natural conversational speech")
    print("  • For research, the 768-d EMBEDDINGS are far more useful than the")
    print("    discrete labels — they capture nuance the classifier head discards")
    print("  • Language: trained primarily on English + Chinese.  French may work")
    print("    for vocal affect but the categories will be biased toward English")
    print("    affect display norms.")


if __name__ == "__main__":
    main()

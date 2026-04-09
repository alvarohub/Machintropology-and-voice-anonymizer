#!/usr/bin/env python3
"""
Benchmark each stage of the inference pipeline.

Measures wall-clock time for VAD, emotion2vec, and openSMILE on
synthetic speech-like audio.  Tells you whether the pipeline can
keep up with real-time at a given hop duration.

Usage:
    python test/benchmark_pipeline.py
    python test/benchmark_pipeline.py --duration 1.0 --repeats 10
"""

import argparse
import math
import os
import sys
import time

import numpy as np

# Allow imports from parent dir
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_speech_audio(duration: float = 2.0, sr: int = 16000) -> np.ndarray:
    """Synthesise a speech-like signal: F0 ~200 Hz + harmonics + noise."""
    n = int(sr * duration)
    t = np.linspace(0, duration, n, dtype=np.float32)
    audio = (
        0.30 * np.sin(2 * np.pi * 200 * t)
        + 0.15 * np.sin(2 * np.pi * 400 * t)
        + 0.08 * np.sin(2 * np.pi * 600 * t)
        + 0.05 * np.random.randn(n).astype(np.float32)
    )
    return audio


def bench_opensmile(audio: np.ndarray, sr: int, repeats: int) -> tuple[float, dict]:
    """Returns (avg_seconds, feature_dict)."""
    import opensmile

    smile = opensmile.Smile(
        feature_set=opensmile.FeatureSet.eGeMAPSv02,
        feature_level=opensmile.FeatureLevel.Functionals,
    )
    # Warm-up
    smile.process_signal(audio, sampling_rate=sr)

    t0 = time.perf_counter()
    for _ in range(repeats):
        df = smile.process_signal(audio, sampling_rate=sr)
    avg = (time.perf_counter() - t0) / repeats

    vals = {col: float(df.iloc[0][col]) for col in df.columns}
    return avg, vals


def bench_vad(audio: np.ndarray, sr: int, repeats: int) -> tuple[float, list]:
    """Returns (avg_seconds, speech_timestamps)."""
    from vad import SileroVAD

    vad = SileroVAD(threshold=0.3, sample_rate=sr)

    t0 = time.perf_counter()
    for _ in range(repeats):
        ratio = vad.speech_ratio(audio)
    avg = (time.perf_counter() - t0) / repeats

    return avg, ratio


def bench_emotion(
    audio: np.ndarray, sr: int, repeats: int, with_embedding: bool = False,
) -> tuple[float, dict]:
    """Returns (avg_seconds, result_dict)."""
    from emotion_model import Emotion2VecModel

    model = Emotion2VecModel(device="cpu")

    # Warm-up
    model.predict(audio, sr=sr, extract_embedding=with_embedding)

    t0 = time.perf_counter()
    for _ in range(repeats):
        result = model.predict(audio, sr=sr, extract_embedding=with_embedding)
    avg = (time.perf_counter() - t0) / repeats

    return avg, result


def main():
    parser = argparse.ArgumentParser(description="Benchmark the speech→emotion pipeline")
    parser.add_argument("--duration", type=float, default=2.0, help="Chunk duration in seconds")
    parser.add_argument("--repeats", type=int, default=5, help="Repetitions per stage")
    parser.add_argument("--sr", type=int, default=16000, help="Sample rate")
    args = parser.parse_args()

    audio = make_speech_audio(args.duration, args.sr)
    print(f"Audio: {len(audio)} samples ({args.duration}s @ {args.sr} Hz)")
    print(f"       min={audio.min():.3f}  max={audio.max():.3f}  rms={np.sqrt(np.mean(audio**2)):.3f}")
    print(f"       repeats={args.repeats}\n")

    timings: dict[str, float] = {}

    # ── openSMILE ──
    print("─── openSMILE eGeMAPSv02 ───")
    try:
        t_smile, vals = bench_opensmile(audio, args.sr, args.repeats)
        timings["openSMILE"] = t_smile
        nan_count = sum(1 for v in vals.values() if math.isnan(v))
        print(f"  Time:  {t_smile * 1000:.0f} ms")
        print(f"  NaNs:  {nan_count}/{len(vals)}")
        for k in [
            "F0semitoneFrom27.5Hz_sma3nz_amean",
            "loudness_sma3_amean",
            "jitterLocal_sma3nz_amean",
            "shimmerLocaldB_sma3nz_amean",
            "HNRdBACF_sma3nz_amean",
        ]:
            v = vals.get(k, float("nan"))
            tag = " ⚠ NaN" if math.isnan(v) else ""
            print(f"    {k}: {v:.4f}{tag}")
    except ImportError:
        print("  ⚠ opensmile not installed")

    # ── Silero VAD ──
    print("\n─── Silero VAD ───")
    try:
        t_vad, ratio = bench_vad(audio, args.sr, args.repeats)
        timings["VAD"] = t_vad
        print(f"  Time:          {t_vad * 1000:.0f} ms")
        print(f"  Speech ratio:  {ratio:.2f}")
        print(f"  Window:        512 samples (32 ms) internal")
        print(f"  Chunk scanned: {len(audio) // 512} VAD windows")
    except Exception as exc:
        print(f"  ⚠ VAD error: {exc}")

    # ── emotion2vec ──
    print("\n─── emotion2vec ───")
    try:
        t_emo, result = bench_emotion(audio, args.sr, args.repeats, with_embedding=False)
        timings["emotion2vec"] = t_emo
        print(f"  Time (no emb):   {t_emo * 1000:.0f} ms")
        print(f"  Label:           {result['label']} ({result['confidence']:.0%})")
        print(f"  Scores:          {result['scores']}")

        t_emb, result_e = bench_emotion(audio, args.sr, args.repeats, with_embedding=True)
        timings["emotion2vec+emb"] = t_emb
        emb_shape = result_e.get("embedding", np.array([])).shape
        print(f"  Time (with emb): {t_emb * 1000:.0f} ms")
        print(f"  Embedding:       {emb_shape}")
    except Exception as exc:
        print(f"  ⚠ emotion2vec error: {exc}")

    # ── Summary ──
    total = sum(v for k, v in timings.items() if k != "emotion2vec+emb")
    print(f"\n{'=' * 50}")
    print(f"TOTAL per iteration:  {total * 1000:.0f} ms")
    print(f"  (VAD {timings.get('VAD', 0) * 1000:.0f}"
          f" + emo {timings.get('emotion2vec', 0) * 1000:.0f}"
          f" + openSMILE {timings.get('openSMILE', 0) * 1000:.0f})")

    hop = 1.0
    headroom = hop - total
    if headroom > 0:
        print(f"\n✅ Pipeline is {headroom * 1000:.0f} ms faster than real-time (hop=1s)")
    else:
        print(f"\n⚠️  Pipeline is {-headroom * 1000:.0f} ms SLOWER than real-time (hop=1s)")
        print("   Queue will back up — consider skipping stale chunks or reducing work.")


if __name__ == "__main__":
    main()

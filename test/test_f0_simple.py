#!/usr/bin/env python3
"""
Minimal F0 + envelope diagnostic — NO scrolling, NO ring buffers.

Records 5 seconds of mic audio, processes the entire buffer once
with openSMILE eGeMAPSv02 LLD, and plots F0 + loudness left-to-right.

This isolates whether fragmentation comes from openSMILE's extraction
itself or from the display/threading/chunking pipeline.

Usage:
    conda activate ML311
    python test_f0_simple.py
"""

import matplotlib
matplotlib.use("TkAgg")  # required on macOS — do NOT use macosx backend

import numpy as np
import sounddevice as sd
import opensmile
import matplotlib.pyplot as plt

# ── Config ──────────────────────────────────────────────────────────
DURATION = 5.0       # seconds to record
SR = 16000           # sample rate
TARGET_PEAK = 0.5    # peak-normalize to this before openSMILE
MIN_PEAK = 0.01      # below this, skip normalization (noise floor)

# ── Record ──────────────────────────────────────────────────────────
print(f"Recording {DURATION}s at {SR}Hz … speak now!")
audio = sd.rec(int(DURATION * SR), samplerate=SR, channels=1, dtype="float32")
sd.wait()
audio = audio.squeeze()  # (N,) mono
print(f"Done. {len(audio)} samples, peak={np.max(np.abs(audio)):.4f}")

# ── Normalize ───────────────────────────────────────────────────────
peak = np.max(np.abs(audio))
if peak >= MIN_PEAK:
    audio_norm = (audio * (TARGET_PEAK / peak)).astype(np.float32)
    print(f"Normalized: peak {peak:.4f} → {TARGET_PEAK}")
else:
    audio_norm = audio
    print(f"Too quiet (peak={peak:.4f}), skipping normalization")

# ── Extract LLD with openSMILE ──────────────────────────────────────
smile = opensmile.Smile(
    feature_set=opensmile.FeatureSet.eGeMAPSv02,
    feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
)
df = smile.process_signal(audio_norm, sampling_rate=SR)

starts = np.array([t.total_seconds() for t in df.index.get_level_values("start")])
ends = np.array([t.total_seconds() for t in df.index.get_level_values("end")])
times = (starts + ends) / 2.0

f0 = df["F0semitoneFrom27.5Hz_sma3nz"].values.astype(np.float32)
loudness = df["Loudness_sma3"].values.astype(np.float32)

n_total = len(f0)
n_voiced = np.count_nonzero(f0 > 0)
print(f"Frames: {n_total}, voiced: {n_voiced} ({100*n_voiced/n_total:.1f}%)")
if n_voiced > 0:
    f0_voiced = f0[f0 > 0]
    hz_median = 27.5 * 2 ** (np.median(f0_voiced) / 12)
    print(f"F0 median: {np.median(f0_voiced):.1f} st = {hz_median:.0f} Hz")

# ── Also extract WITHOUT normalization for comparison ────────────────
df_raw = smile.process_signal(audio, sampling_rate=SR)
f0_raw = df_raw["F0semitoneFrom27.5Hz_sma3nz"].values.astype(np.float32)
loudness_raw = df_raw["Loudness_sma3"].values.astype(np.float32)
n_voiced_raw = np.count_nonzero(f0_raw > 0)
print(f"Without normalization: {n_voiced_raw}/{n_total} voiced "
      f"({100*n_voiced_raw/n_total:.1f}%)")

# ── Plot ────────────────────────────────────────────────────────────
fig, axes = plt.subplots(4, 1, figsize=(14, 8), sharex=True)
fig.suptitle(f"openSMILE eGeMAPSv02 LLD — {DURATION}s recording", fontsize=13)

# 1) Raw waveform
ax = axes[0]
t_wave = np.linspace(0, DURATION, len(audio))
ax.plot(t_wave, audio, color="gray", linewidth=0.3)
ax.set_ylabel("Waveform")
ax.set_title("Raw audio (before normalization)")

# 2) F0 (normalized audio)
ax = axes[1]
f0_plot = f0.copy()
f0_plot[f0_plot <= 0] = np.nan
ax.plot(times, f0_plot, color="cyan", linewidth=1.5, marker=".", markersize=2)
ax.set_ylabel("F0 (semitones)")
ax.set_title(f"F0 — normalized audio (peak→{TARGET_PEAK}) — {n_voiced}/{n_total} voiced")

# 3) F0 (raw audio — no normalization)
ax = axes[2]
f0_raw_plot = f0_raw.copy()
f0_raw_plot[f0_raw_plot <= 0] = np.nan
ax.plot(times, f0_raw_plot, color="orange", linewidth=1.5, marker=".", markersize=2)
ax.set_ylabel("F0 (semitones)")
ax.set_title(f"F0 — raw audio (peak={peak:.4f}) — {n_voiced_raw}/{n_total} voiced")

# 4) Loudness
ax = axes[3]
ax.fill_between(times, 0, loudness, color="green", alpha=0.4)
ax.plot(times, loudness, color="green", linewidth=1)
ax.set_ylabel("Loudness (sone)")
ax.set_xlabel("Time (s)")
ax.set_title("Loudness (normalized audio)")

for ax in axes:
    ax.grid(True, alpha=0.2)

plt.tight_layout()
plt.show()

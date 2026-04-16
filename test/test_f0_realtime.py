#!/usr/bin/env python3
"""
Real-time F0 + loudness display — MINIMAL, no patches.

Strategy: mirror what works in test_f0_simple.py (single openSMILE call
on the full buffer), but do it live.

- sounddevice InputStream callback appends audio to a growing list
- FuncAnimation every 200ms: concatenate full audio, process with ONE
  openSMILE call, redraw the entire plot from scratch
- Fixed 10s display window (scrolls by trimming old audio)
- No ring buffers, no deques, no NaN bridging, no edge margins,
  no threading hacks, no deduplication

This is intentionally "wasteful" (reprocesses everything each frame)
to prove that the approach works.  Optimization comes later.

Usage:
    conda activate ML311
    python test_f0_realtime.py
"""

import matplotlib
matplotlib.use("TkAgg")

import numpy as np
import sounddevice as sd
import opensmile
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import threading
import time
import signal
import sys

# ── Config ──────────────────────────────────────────────────────────
SR = 16000
DISPLAY_SEC = 10.0       # visible window
MAX_AUDIO_SEC = 12.0     # keep slightly more than display (trim old)
TARGET_PEAK = 0.5
MIN_PEAK = 0.01
UPDATE_MS = 200          # redraw interval

# ── Audio accumulator (lock-protected list) ─────────────────────────
_audio_lock = threading.Lock()
_audio_chunks: list[np.ndarray] = []

def _audio_callback(indata, frames, time_info, status):
    """sounddevice InputStream callback — just append."""
    with _audio_lock:
        _audio_chunks.append(indata[:, 0].copy())

# ── openSMILE (single instance, only used from main thread) ────────
smile = opensmile.Smile(
    feature_set=opensmile.FeatureSet.eGeMAPSv02,
    feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
)

# ── Plot setup ──────────────────────────────────────────────────────
fig, axes = plt.subplots(3, 1, figsize=(14, 7), sharex=True)
fig.suptitle("Real-time openSMILE LLD (full-buffer reprocess)", fontsize=13)

ax_wave, ax_f0, ax_loud = axes

ax_wave.set_ylabel("Waveform")
ax_wave.set_ylim(-0.5, 0.5)
line_wave, = ax_wave.plot([], [], color="gray", linewidth=0.3)

ax_f0.set_ylabel("F0 (semitones)")
ax_f0.set_ylim(0, 50)
line_f0, = ax_f0.plot([], [], color="cyan", linewidth=1.5)

ax_loud.set_ylabel("Loudness (sone)")
ax_loud.set_ylim(0, 3)
ax_loud.set_xlabel("Time (s)")
fill_loud = [None]
line_loud, = ax_loud.plot([], [], color="green", linewidth=1)

for ax in axes:
    ax.set_xlim(0, DISPLAY_SEC)
    ax.grid(True, alpha=0.2)

_t_start = time.time()

def _update(frame_num):
    """FuncAnimation callback — runs on main thread."""
    # 1) Grab accumulated audio
    with _audio_lock:
        if not _audio_chunks:
            return
        chunks = list(_audio_chunks)

    audio = np.concatenate(chunks)
    total_sec = len(audio) / SR

    # 2) Trim to MAX_AUDIO_SEC (keep the tail)
    max_samples = int(MAX_AUDIO_SEC * SR)
    if len(audio) > max_samples:
        audio = audio[-max_samples:]
        total_sec = len(audio) / SR
        # Also trim the chunk list to avoid unbounded memory
        with _audio_lock:
            combined = np.concatenate(_audio_chunks)
            _audio_chunks.clear()
            _audio_chunks.append(combined[-max_samples:])

    # 3) Normalize
    peak = np.max(np.abs(audio))
    if peak >= MIN_PEAK:
        audio_norm = (audio * (TARGET_PEAK / peak)).astype(np.float32)
    else:
        audio_norm = audio.astype(np.float32)

    # 4) Process with openSMILE — single call on full buffer
    try:
        df = smile.process_signal(audio_norm, sampling_rate=SR)
    except Exception as e:
        print(f"[openSMILE error] {e}")
        return

    starts = np.array([t.total_seconds() for t in df.index.get_level_values("start")])
    ends = np.array([t.total_seconds() for t in df.index.get_level_values("end")])
    times = (starts + ends) / 2.0

    f0 = df["F0semitoneFrom27.5Hz_sma3nz"].values.astype(np.float32)
    loudness = df["Loudness_sma3"].values.astype(np.float32)

    # 5) For display: replace unvoiced F0 with NaN (gaps in line)
    f0_plot = f0.copy()
    f0_plot[f0_plot <= 0] = np.nan

    # 6) Waveform time axis
    t_wave = np.linspace(0, total_sec, len(audio))

    # 7) Update x-axis to scroll
    elapsed = time.time() - _t_start
    if elapsed > DISPLAY_SEC:
        x_min = elapsed - DISPLAY_SEC
        x_max = elapsed
        # Shift LLD times to wall-clock
        lld_times = times + (elapsed - total_sec)
        wave_times = t_wave + (elapsed - total_sec)
    else:
        x_min = 0
        x_max = DISPLAY_SEC
        lld_times = times + (elapsed - total_sec)
        wave_times = t_wave + (elapsed - total_sec)

    for ax in axes:
        ax.set_xlim(x_min, x_max)

    # 8) Update plots
    line_wave.set_data(wave_times, audio_norm)
    line_f0.set_data(lld_times, f0_plot)
    line_loud.set_data(lld_times, loudness)

    # Update fill
    if fill_loud[0] is not None:
        fill_loud[0].remove()
    fill_loud[0] = ax_loud.fill_between(
        lld_times, 0, loudness, alpha=0.35, color="green", linewidth=0
    )

    # Update y-limits for F0 based on actual data
    f0_valid = f0_plot[~np.isnan(f0_plot)]
    if len(f0_valid) > 2:
        margin = 2.0
        ax_f0.set_ylim(max(0, f0_valid.min() - margin), f0_valid.max() + margin)

    # Status
    n_voiced = np.count_nonzero(f0 > 0)
    fig.suptitle(
        f"Real-time openSMILE LLD — {total_sec:.1f}s buffered, "
        f"{n_voiced}/{len(f0)} voiced, peak={peak:.4f}",
        fontsize=11,
    )


# ── Cleanup & signal handling ──────────────────────────────────────
stream: sd.InputStream | None = None
anim: FuncAnimation | None = None

def _cleanup():
    global stream
    if stream is not None:
        try:
            stream.abort()
            stream.close()
        except Exception:
            pass
        stream = None
    print("\nAudio stream closed. Clean exit.")

def _sig_handler(signum, frame):
    _cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT,  _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)

# ── Start ───────────────────────────────────────────────────────────
print("Starting real-time F0 display… speak into the mic!")
print("Close the window or press Ctrl-C to stop.")

try:
    stream = sd.InputStream(
        samplerate=SR,
        channels=1,
        dtype="float32",
        blocksize=int(SR * 0.05),  # 50ms blocks
        callback=_audio_callback,
    )
    stream.start()

    anim = FuncAnimation(fig, _update, interval=UPDATE_MS, blit=False, cache_frame_data=False)
    plt.tight_layout()
    plt.show()
finally:
    _cleanup()

print("Done.")

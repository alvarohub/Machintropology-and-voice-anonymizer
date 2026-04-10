#!/usr/bin/env python3
"""
Real-time openSMILE LLD display — all 5 features.

Extends the proven test_f0_realtime.py with jitter, shimmer, and HNR.
Same architecture: single openSMILE call on the full buffer each cycle.

Usage:
    conda activate ML311
    python test_lld_realtime.py
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
DISPLAY_SEC = 10.0
MAX_AUDIO_SEC = 12.0
UPDATE_MS = 200

# Noise gate: frames with Loudness below this are forced to NaN for nz
# features (F0, jitter, shimmer, HNR).  Suppresses noise-floor detections.
# Typical speech loudness is 0.3–2.0 sone; noise floor is <0.05.
NOISE_GATE_LOUDNESS = 0.05   # sone — adjust to taste

# ── Features to display ────────────────────────────────────────────
FEATURES = [
    ("F0semitoneFrom27.5Hz_sma3nz", "F0 (semitones)",    "cyan",   True,  (0, 50)),
    ("Loudness_sma3",               "Loudness (sone)",    "green",  False, (0, 2.5)),
    ("jitterLocal_sma3nz",          "Jitter",             "pink",   True,  (0, 0.35)),
    ("shimmerLocaldB_sma3nz",       "Shimmer (dB)",       "orange", True,  (0, 30)),
    ("HNRdBACF_sma3nz",            "HNR (dB)",           "violet", True,  (0, 15)),
]
# True = "nz" feature (0 means unvoiced → show as NaN gap)

# ── Audio accumulator ───────────────────────────────────────────────
_audio_lock = threading.Lock()
_audio_chunks: list[np.ndarray] = []

def _audio_callback(indata, frames, time_info, status):
    with _audio_lock:
        _audio_chunks.append(indata[:, 0].copy())

# ── openSMILE (main thread only) ───────────────────────────────────
smile = opensmile.Smile(
    feature_set=opensmile.FeatureSet.eGeMAPSv02,
    feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
)

# ── Plot setup ──────────────────────────────────────────────────────
n_strips = len(FEATURES) + 1  # +1 for waveform
fig, axes = plt.subplots(n_strips, 1, figsize=(14, 2 * n_strips), sharex=True)
fig.patch.set_facecolor("#1a1a2e")
fig.suptitle("Real-time openSMILE LLD", fontsize=12, color="white")

# Waveform
ax_wave = axes[0]
ax_wave.set_facecolor("#1a1a2e")
ax_wave.set_ylabel("Waveform", color="white", fontsize=8)
ax_wave.set_ylim(-0.1, 0.1)  # typical mic level range
ax_wave.tick_params(colors="gray", labelsize=7)
line_wave, = ax_wave.plot([], [], color="gray", linewidth=0.3)

# Feature strips
lines = []
fills = [None] * len(FEATURES)
feature_axes = []

for i, (key, label, color, is_nz, ylim) in enumerate(FEATURES):
    ax = axes[i + 1]
    ax.set_facecolor("#1a1a2e")
    ax.set_ylabel(label, color=color, fontsize=8)
    ax.set_ylim(ylim)
    ax.tick_params(colors="gray", labelsize=7)
    ln, = ax.plot([], [], color=color, linewidth=1.5)
    lines.append(ln)
    feature_axes.append(ax)

for ax in axes:
    ax.set_xlim(0, DISPLAY_SEC)
    ax.grid(True, alpha=0.15, color="gray")
    for spine in ax.spines.values():
        spine.set_color("#333")

axes[-1].set_xlabel("Time (s)", color="white", fontsize=9)

_t_start = time.time()

def _update(frame_num):
    # 1) Grab audio
    with _audio_lock:
        if not _audio_chunks:
            return
        chunks = list(_audio_chunks)

    audio = np.concatenate(chunks)
    total_sec = len(audio) / SR

    # 2) Trim
    max_samples = int(MAX_AUDIO_SEC * SR)
    if len(audio) > max_samples:
        audio = audio[-max_samples:]
        total_sec = len(audio) / SR
        with _audio_lock:
            combined = np.concatenate(_audio_chunks)
            _audio_chunks.clear()
            _audio_chunks.append(combined[-max_samples:])

    # 3) Pass raw audio to openSMILE (no normalization)
    peak = np.max(np.abs(audio))
    # Compute trailing 1s RMS to monitor AGC drift
    tail_1s = audio[-SR:] if len(audio) >= SR else audio
    rms_tail = float(np.sqrt(np.mean(tail_1s ** 2)))
    audio_norm = audio.astype(np.float32)

    # 4) openSMILE — single call
    try:
        df = smile.process_signal(audio_norm, sampling_rate=SR)
    except Exception as e:
        print(f"[openSMILE error] {e}")
        return

    starts = np.array([t.total_seconds() for t in df.index.get_level_values("start")])
    ends = np.array([t.total_seconds() for t in df.index.get_level_values("end")])
    times = (starts + ends) / 2.0

    # 5) Time mapping
    elapsed = time.time() - _t_start
    offset = elapsed - total_sec
    lld_times = times + offset
    wave_times = np.linspace(0, total_sec, len(audio)) + offset

    if elapsed > DISPLAY_SEC:
        x_min, x_max = elapsed - DISPLAY_SEC, elapsed
    else:
        x_min, x_max = 0, DISPLAY_SEC

    for ax in axes:
        ax.set_xlim(x_min, x_max)

    # 6) Waveform
    line_wave.set_data(wave_times, audio_norm)

    # 7) Noise gate: get per-frame loudness for gating
    loudness_gate = df["Loudness_sma3"].values.astype(np.float32)
    quiet_mask = loudness_gate < NOISE_GATE_LOUDNESS

    # 8) Feature strips
    for i, (key, label, color, is_nz, ylim) in enumerate(FEATURES):
        vals = df[key].values.astype(np.float32)
        if is_nz:
            vals = vals.copy()
            vals[vals <= 0] = np.nan
            # Gate: suppress detections in quiet frames
            vals[quiet_mask] = np.nan
        lines[i].set_data(lld_times, vals)

        # Fill for non-nz features (loudness)
        if not is_nz:
            if fills[i] is not None:
                fills[i].remove()
            fills[i] = feature_axes[i].fill_between(
                lld_times, 0, vals, alpha=0.3, color=color, linewidth=0
            )

    # 9) Status — show RMS to monitor AGC drift
    f0 = df["F0semitoneFrom27.5Hz_sma3nz"].values
    n_voiced = np.count_nonzero(f0 > 0)
    n_gated = int(np.count_nonzero(quiet_mask))
    fig.suptitle(
        f"Real-time openSMILE LLD — {total_sec:.1f}s buf, "
        f"{n_voiced}/{len(f0)} voiced, "
        f"peak={peak:.4f}, rms1s={rms_tail:.5f}, "
        f"gated={n_gated}",
        fontsize=9, color="white",
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
print("Starting real-time LLD display… speak into the mic!")
print("Close the window or press Ctrl-C to stop.")

try:
    stream = sd.InputStream(
        samplerate=SR, channels=1, dtype="float32",
        blocksize=int(SR * 0.05), callback=_audio_callback,
    )
    stream.start()

    anim = FuncAnimation(fig, _update, interval=UPDATE_MS, blit=False, cache_frame_data=False)
    plt.tight_layout()
    plt.show()
finally:
    _cleanup()

print("Done.")

stream.stop()
stream.close()
print("Done.")

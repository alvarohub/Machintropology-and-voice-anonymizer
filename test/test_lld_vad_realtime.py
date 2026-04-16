#!/usr/bin/env python3
"""
Real-time openSMILE LLD + Silero VAD display.

Built from the proven test_lld_realtime.py architecture:
- sounddevice callback appends audio to a list
- FuncAnimation every 200ms: full buffer → openSMILE + Silero VAD
- VAD gates nz features (F0, jitter, shimmer, HNR) — no speech = NaN
- No threading for extraction, no ring buffers, no patches

Usage:
    conda activate ML311
    python test_lld_vad_realtime.py
"""

import matplotlib
matplotlib.use("TkAgg")

import numpy as np
import sounddevice as sd
import opensmile
import torch
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import threading
import time
import signal
import sys

# ── Config ──────────────────────────────────────────────────────────
SR = 16000
DISPLAY_SEC = 10.0
MAX_AUDIO_SEC = 10.0
UPDATE_MS = 250          # slightly slower to account for VAD cost
VAD_THRESHOLD = 0.3      # Silero speech probability threshold

# ── Features to display ────────────────────────────────────────────
FEATURES = [
    ("F0semitoneFrom27.5Hz_sma3nz", "F0 (st)",      "cyan",   True,  (0, 50)),
    ("Loudness_sma3",               "Loudness",      "green",  False, (0, 2.5)),
    ("jitterLocal_sma3nz",          "Jitter",        "pink",   True,  (0, 0.35)),
    ("shimmerLocaldB_sma3nz",       "Shimmer (dB)",  "orange", True,  (0, 30)),
    ("HNRdBACF_sma3nz",            "HNR (dB)",      "violet", True,  (0, 15)),
]

# ── Audio accumulator ───────────────────────────────────────────────
_audio_lock = threading.Lock()
_audio_chunks: list[np.ndarray] = []

def _audio_callback(indata, frames, time_info, status):
    with _audio_lock:
        _audio_chunks.append(indata[:, 0].copy())

# ── openSMILE ───────────────────────────────────────────────────────
smile = opensmile.Smile(
    feature_set=opensmile.FeatureSet.eGeMAPSv02,
    feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
)

# ── Silero VAD ──────────────────────────────────────────────────────
# Model is already cached at ~/.cache/torch/hub/ (local filesystem).
# No need to redirect — the default cache dir is NOT on the FUSE mount.
print("Loading Silero VAD …")
vad_model, vad_utils = torch.hub.load(
    "snakers4/silero-vad", "silero_vad",
    trust_repo=True, force_reload=False,
)
get_speech_timestamps = vad_utils[0]
print("VAD ready.")


def _compute_vad_mask(audio: np.ndarray, lld_times_in_audio: np.ndarray) -> np.ndarray:
    """Return a boolean mask (True=speech) aligned to LLD frame times.

    Silero VAD returns speech timestamp ranges in samples.
    We map those to a per-LLD-frame mask.
    """
    tensor = torch.from_numpy(audio).float()
    timestamps = get_speech_timestamps(
        tensor, vad_model,
        threshold=VAD_THRESHOLD,
        sampling_rate=SR,
        min_speech_duration_ms=250,
    )
    vad_model.reset_states()

    # Build mask: True if frame midpoint falls inside any speech segment
    mask = np.zeros(len(lld_times_in_audio), dtype=bool)
    for seg in timestamps:
        t_start = seg["start"] / SR
        t_end = seg["end"] / SR
        mask |= (lld_times_in_audio >= t_start) & (lld_times_in_audio <= t_end)
    return mask


def _compute_vad_contour(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (times, probabilities) at ~32ms resolution for display.

    Silero processes 512-sample windows (32ms at 16kHz).
    We run it window-by-window to get a probability contour.
    """
    window = 512
    n_windows = len(audio) // window
    if n_windows == 0:
        return np.array([]), np.array([])

    probs = np.zeros(n_windows, dtype=np.float32)
    times = np.zeros(n_windows, dtype=np.float32)

    for i in range(n_windows):
        chunk = audio[i * window : (i + 1) * window]
        tensor = torch.from_numpy(chunk).float()
        prob = vad_model(tensor, SR).item()
        times[i] = (i * window + window / 2) / SR
        probs[i] = prob

    vad_model.reset_states()
    return times, probs


# ── Plot setup ──────────────────────────────────────────────────────
n_strips = len(FEATURES) + 2  # +1 waveform, +1 VAD
fig, axes = plt.subplots(n_strips, 1, figsize=(14, 1.8 * n_strips), sharex=True,
                         gridspec_kw={"height_ratios": [2, 1] + [1]*len(FEATURES)})
fig.patch.set_facecolor("#1a1a2e")
fig.suptitle("Real-time LLD + VAD", fontsize=12, color="white")

# Waveform
ax_wave = axes[0]
ax_wave.set_facecolor("#1a1a2e")
ax_wave.set_ylabel("Waveform", color="white", fontsize=8)
ax_wave.set_ylim(-0.1, 0.1)
ax_wave.tick_params(colors="gray", labelsize=7)
line_wave, = ax_wave.plot([], [], color="gray", linewidth=0.3)

# VAD probability
ax_vad = axes[1]
ax_vad.set_facecolor("#1a1a2e")
ax_vad.set_ylabel("VAD", color="#88FF88", fontsize=8)
ax_vad.set_ylim(0, 1.05)
ax_vad.tick_params(colors="gray", labelsize=7)
line_vad, = ax_vad.plot([], [], color="#88FF88", linewidth=1.2)
fill_vad = [None]
# Threshold line
ax_vad.axhline(y=VAD_THRESHOLD, color="#FF8888", linewidth=0.8,
               linestyle="--", alpha=0.6)

# Feature strips
lines = []
fills = [None] * len(FEATURES)
feature_axes = []

for i, (key, label, color, is_nz, ylim) in enumerate(FEATURES):
    ax = axes[i + 2]
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

    # 3) openSMILE — single call on full buffer
    audio_f32 = audio.astype(np.float32)
    peak = np.max(np.abs(audio_f32))

    try:
        df = smile.process_signal(audio_f32, sampling_rate=SR)
    except Exception as e:
        print(f"[openSMILE error] {e}")
        return

    starts = np.array([t.total_seconds() for t in df.index.get_level_values("start")])
    ends = np.array([t.total_seconds() for t in df.index.get_level_values("end")])
    times_in_audio = (starts + ends) / 2.0

    # 4) VAD — speech mask aligned to LLD frames
    speech_mask = _compute_vad_mask(audio_f32, times_in_audio)
    silence_mask = ~speech_mask

    # 5) VAD probability contour for display
    vad_times_in_audio, vad_probs = _compute_vad_contour(audio_f32)

    # 6) Time mapping
    elapsed = time.time() - _t_start
    offset = elapsed - total_sec
    lld_times = times_in_audio + offset
    vad_display_times = vad_times_in_audio + offset
    wave_times = np.linspace(0, total_sec, len(audio)) + offset

    if elapsed > DISPLAY_SEC:
        x_min, x_max = elapsed - DISPLAY_SEC, elapsed
    else:
        x_min, x_max = 0, DISPLAY_SEC

    for ax in axes:
        ax.set_xlim(x_min, x_max)

    # 7) Waveform
    line_wave.set_data(wave_times, audio_f32)

    # 8) VAD contour
    line_vad.set_data(vad_display_times, vad_probs)
    if fill_vad[0] is not None:
        fill_vad[0].remove()
    if len(vad_probs) > 0:
        fill_vad[0] = ax_vad.fill_between(
            vad_display_times, 0, vad_probs, alpha=0.25, color="#88FF88", linewidth=0
        )
    else:
        fill_vad[0] = None

    # 9) Feature strips — VAD-gated
    for i, (key, label, color, is_nz, ylim) in enumerate(FEATURES):
        vals = df[key].values.astype(np.float32)
        if is_nz:
            vals = vals.copy()
            vals[vals <= 0] = np.nan
            vals[silence_mask] = np.nan   # VAD gate
        lines[i].set_data(lld_times, vals)

        if not is_nz:
            if fills[i] is not None:
                fills[i].remove()
            fills[i] = feature_axes[i].fill_between(
                lld_times, 0, vals, alpha=0.3, color=color, linewidth=0
            )

    # 10) Status
    f0 = df["F0semitoneFrom27.5Hz_sma3nz"].values
    n_voiced = np.count_nonzero(f0 > 0)
    n_speech = int(np.count_nonzero(speech_mask))
    fig.suptitle(
        f"LLD+VAD — {total_sec:.1f}s buf, "
        f"speech={n_speech}/{len(speech_mask)}, "
        f"voiced={n_voiced}, peak={peak:.4f}",
        fontsize=9, color="white",
    )


# ── Cleanup & signal handling ───────────────────────────────────────
# On macOS, closing the matplotlib window while a PortAudio callback is
# in-flight can leave the process stuck in an uninterruptible kernel call
# (especially on a FUSE filesystem like Google Drive).  We register cleanup
# so the audio stream is always torn down BEFORE the process tries to exit.

stream: sd.InputStream | None = None
anim: FuncAnimation | None = None

def _cleanup():
    """Stop audio stream — must run before process exit."""
    global stream
    if stream is not None:
        try:
            stream.abort()   # abort is faster than stop — drops pending buffers
            stream.close()
        except Exception:
            pass
        stream = None
    print("\nAudio stream closed. Clean exit.")

def _sig_handler(signum, frame):
    """Handle Ctrl-C and SIGTERM gracefully."""
    _cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT,  _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)

# ── Start ───────────────────────────────────────────────────────────
print("Starting real-time LLD+VAD display… speak into the mic!")
print("Close the window or press Ctrl-C to stop.")

try:
    stream = sd.InputStream(
        samplerate=SR, channels=1, dtype="float32",
        blocksize=int(SR * 0.05), callback=_audio_callback,
    )
    stream.start()

    anim = FuncAnimation(fig, _update, interval=UPDATE_MS, blit=False, cache_frame_data=False)
    plt.tight_layout()
    plt.show()          # blocks until window is closed
finally:
    _cleanup()

print("Done.")

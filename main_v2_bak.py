#!/usr/bin/env python3
"""
Real-time Speech Analysis — Emotion + Prosody + VAD

Integrates:
  - emotion2vec (9-class utterance-level emotion, background thread)
  - openSMILE eGeMAPSv02 LLD (frame-level prosody, main thread)
  - Silero VAD (voice activity detection, main thread)

Architecture (from proven test_lld_vad_realtime.py):
  - sounddevice callback → shared audio accumulator
  - FuncAnimation: full buffer → openSMILE LLD + VAD (main thread)
  - Background thread: audio window → emotion2vec → result deque

Two independently toggleable groups with window-size controls:
  EMOTION: 9-class probability step functions (toggle + analysis window)
  PROSODY: 5 LLD contours, VAD-gated (toggle + display window)

Usage:
    conda activate ML311
    python main_v2.py
"""

from __future__ import annotations

# TkAgg MUST be set before any other import — the macOS native Cocoa
# backend conflicts with openSMILE's pre-compiled C binaries (shared
# LLVM/Qt libs), silently corrupting PortAudio audio callbacks.
import matplotlib
matplotlib.use("TkAgg")

import numpy as np
import sounddevice as sd
import opensmile
import torch
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button, TextBox
import collections
import threading
import time
import signal
import sys


# ════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════
SR            = 16000
MAX_AUDIO_SEC = 15.0        # audio ring-buffer ceiling
UPDATE_MS     = 250         # FuncAnimation period (ms)
VAD_THRESHOLD = 0.3         # Silero speech-probability gate

# Defaults (editable at runtime via text inputs)
EMO_WIN_DEFAULT  = 2.0      # seconds of audio per emotion2vec inference
DISP_SEC_DEFAULT = 10.0     # scrolling display window (shared X axis)

# ── Emotion labels & palette ───────────────────────────────────────
EMO_DIMS = [
    "angry", "disgusted", "fearful", "happy", "neutral",
    "other", "sad", "surprised", "unknown",
]
EMO_COLORS = {
    "angry":     "#FF4444",
    "disgusted": "#88AA00",
    "fearful":   "#AA44FF",
    "happy":     "#FFD700",
    "neutral":   "#4488FF",
    "other":     "#888888",
    "sad":       "#5566CC",
    "surprised": "#FF8800",
    "unknown":   "#AAAAAA",
}

# ── Prosody LLD features (openSMILE eGeMAPSv02) ───────────────────
PROS = [
    # (column_key,                   label,         colour,  is_nz, y_range)
    ("F0semitoneFrom27.5Hz_sma3nz", "F0 (st)",     "cyan",   True,  (0, 50)),
    ("Loudness_sma3",               "Loudness",     "green",  False, (0, 2.5)),
    ("jitterLocal_sma3nz",          "Jitter",       "pink",   True,  (0, 0.35)),
    ("shimmerLocaldB_sma3nz",       "Shimmer (dB)", "orange", True,  (0, 30)),
    ("HNRdBACF_sma3nz",            "HNR (dB)",     "violet", True,  (0, 15)),
]


# ════════════════════════════════════════════════════════════════════
# Audio accumulator (proven sounddevice callback pattern)
# ════════════════════════════════════════════════════════════════════
_lock   = threading.Lock()
_chunks: list[np.ndarray] = []

# PyTorch is NOT thread-safe on Apple Silicon — concurrent inference
# (Silero VAD on main thread + emotion2vec on bg thread) crashes with
# "code fragment does not contain the given arm address".  Serialize.
_torch_lock = threading.Lock()


def _audio_cb(indata, frames, time_info, status):
    """sounddevice callback — runs on the audio thread."""
    with _lock:
        _chunks.append(indata[:, 0].copy())


# ════════════════════════════════════════════════════════════════════
# openSMILE — frame-level LLD extraction
# ════════════════════════════════════════════════════════════════════
_smile = opensmile.Smile(
    feature_set=opensmile.FeatureSet.eGeMAPSv02,
    feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
)


# ════════════════════════════════════════════════════════════════════
# Silero VAD
# ════════════════════════════════════════════════════════════════════
print("Loading Silero VAD …")
_vad_model, _vad_utils = torch.hub.load(
    "snakers4/silero-vad", "silero_vad",
    trust_repo=True, force_reload=False,
)
_get_speech_timestamps = _vad_utils[0]
print("VAD ready.")


def _vad_mask(audio: np.ndarray, frame_times: np.ndarray) -> np.ndarray:
    """Boolean mask (True = speech) aligned to LLD frame midpoints."""
    with _torch_lock:
        timestamps = _get_speech_timestamps(
            torch.from_numpy(audio).float(), _vad_model,
            threshold=VAD_THRESHOLD, sampling_rate=SR,
            min_speech_duration_ms=250,
        )
        _vad_model.reset_states()
    mask = np.zeros(len(frame_times), dtype=bool)
    for seg in timestamps:
        t0, t1 = seg["start"] / SR, seg["end"] / SR
        mask |= (frame_times >= t0) & (frame_times <= t1)
    return mask


def _vad_contour(audio: np.ndarray):
    """Per-window VAD probabilities at ~32 ms resolution."""
    window = 512
    n = len(audio) // window
    if n == 0:
        return np.array([]), np.array([])
    probs = np.empty(n, np.float32)
    times = np.empty(n, np.float32)
    with _torch_lock:
        for i in range(n):
            chunk = audio[i * window : (i + 1) * window]
            probs[i] = _vad_model(torch.from_numpy(chunk).float(), SR).item()
            times[i] = (i * window + window / 2) / SR
        _vad_model.reset_states()
    return times, probs


# ════════════════════════════════════════════════════════════════════
# emotion2vec
# ════════════════════════════════════════════════════════════════════
print("Loading emotion2vec …")
from emotion_model import Emotion2VecModel

_emo_model = Emotion2VecModel(
    model_name="iic/emotion2vec_plus_base", device="cpu",
)
print(f"emotion2vec ready — {len(_emo_model.dimensions)} classes")


# ════════════════════════════════════════════════════════════════════
# Mutable runtime state (single-element lists for widget callbacks)
# ════════════════════════════════════════════════════════════════════
_emo_on   = [True]           # emotion group toggle
_pros_on  = [True]           # prosody group toggle
_emo_win  = [EMO_WIN_DEFAULT]   # emotion analysis window (seconds)
_disp_sec = [DISP_SEC_DEFAULT]  # scrolling display window (seconds)


# ════════════════════════════════════════════════════════════════════
# Emotion inference — background thread
# ════════════════════════════════════════════════════════════════════
# Each entry: (wall_time, scores_dict, label_str, confidence_float)
_emo_hist: collections.deque = collections.deque(maxlen=500)
_emo_stop = threading.Event()


def _emotion_loop():
    """Periodically run emotion2vec on the latest audio window."""
    last_t = 0.0
    while not _emo_stop.is_set():
        time.sleep(0.1)              # poll interval
        if not _emo_on[0]:
            continue
        now = time.time()
        win_s = _emo_win[0]
        if now - last_t < win_s:     # haven't waited long enough
            continue

        # Snapshot audio under lock
        with _lock:
            if not _chunks:
                continue
            buf = np.concatenate(list(_chunks))

        need = int(win_s * SR)
        if len(buf) < int(0.5 * SR):   # need ≥0.5 s of audio
            continue
        chunk = buf[-need:] if len(buf) >= need else buf

        try:
            with _torch_lock:
                r = _emo_model.predict(chunk, sr=SR)
            _emo_hist.append((now, r["scores"], r["label"], r["confidence"]))
        except Exception as exc:
            print(f"[emotion] {exc}", file=sys.stderr)

        last_t = now


# ════════════════════════════════════════════════════════════════════
# Figure layout
# ════════════════════════════════════════════════════════════════════
BG     = "#1a1a2e"
N_EMO  = len(EMO_DIMS)
N_PROS = len(PROS)
N_ROWS = 2 + N_EMO + N_PROS     # waveform + VAD + 9 emotion + 5 prosody

heights = [2.5, 1] + [0.6] * N_EMO + [1] * N_PROS

fig, axes = plt.subplots(
    N_ROWS, 1,
    figsize=(15, 18),
    sharex=True,
    gridspec_kw={"height_ratios": heights},
)
fig.patch.set_facecolor(BG)
fig.subplots_adjust(left=0.18, right=0.96, top=0.97, bottom=0.03, hspace=0.3)

_t0 = time.time()      # timeline origin


# ── Row 0: Waveform ────────────────────────────────────────────────
ax_wave = axes[0]
ax_wave.set_facecolor(BG)
ax_wave.set_ylabel("Waveform", color="white", fontsize=8)
ax_wave.set_ylim(-0.15, 0.15)
ax_wave.tick_params(colors="gray", labelsize=6)
_ln_wave, = ax_wave.plot([], [], color="gray", lw=0.3)


# ── Row 1: VAD probability ─────────────────────────────────────────
ax_vad = axes[1]
ax_vad.set_facecolor(BG)
ax_vad.set_ylabel("VAD", color="#88FF88", fontsize=8)
ax_vad.set_ylim(0, 1.05)
ax_vad.tick_params(colors="gray", labelsize=6)
_ln_vad, = ax_vad.plot([], [], color="#88FF88", lw=1.2)
ax_vad.axhline(VAD_THRESHOLD, color="#FF8888", lw=0.8, ls="--", alpha=0.6)
_fill_vad = [None]


# ── Rows 2–10: Emotion strips ──────────────────────────────────────
_emo_axes  = []
_emo_lines = []
_emo_fills = []      # mutable [fill_obj] per strip

for i, dim in enumerate(EMO_DIMS):
    ax = axes[2 + i]
    ax.set_facecolor(BG)
    colour = EMO_COLORS.get(dim, "#4488FF")
    ax.set_ylabel(dim.capitalize(), color=colour, fontsize=7)
    ax.set_ylim(0, 1.05)
    ax.tick_params(colors="gray", labelsize=5)
    ln, = ax.plot([], [], color=colour, lw=1.5, drawstyle="steps-post")
    _emo_axes.append(ax)
    _emo_lines.append(ln)
    _emo_fills.append([None])

axes[2].set_title(
    "── EMOTION ──", color="#FFD700", fontsize=8, loc="left", pad=2,
)


# ── Rows 11–15: Prosody strips ─────────────────────────────────────
_pros_axes  = []
_pros_lines = []
_pros_fills = [None] * N_PROS

for i, (key, label, colour, is_nz, ylim) in enumerate(PROS):
    ax = axes[2 + N_EMO + i]
    ax.set_facecolor(BG)
    ax.set_ylabel(label, color=colour, fontsize=8)
    ax.set_ylim(ylim)
    ax.tick_params(colors="gray", labelsize=6)
    ln, = ax.plot([], [], color=colour, lw=1.5)
    _pros_axes.append(ax)
    _pros_lines.append(ln)

axes[2 + N_EMO].set_title(
    "── PROSODY ──", color="cyan", fontsize=8, loc="left", pad=2,
)


# ── Common axis styling ────────────────────────────────────────────
for ax in axes:
    ax.set_xlim(0, DISP_SEC_DEFAULT)
    ax.grid(True, alpha=0.12, color="gray")
    for sp in ax.spines.values():
        sp.set_color("#333")
axes[-1].set_xlabel("Time (s)", color="white", fontsize=9)


# ════════════════════════════════════════════════════════════════════
# Control widgets — toggle button + window text input per group
# ════════════════════════════════════════════════════════════════════

def _make_group_controls(anchor_ax, label, default_win, on_state, win_state, colour):
    """Create ON/OFF button + window-size text input left of *anchor_ax*.

    Returns (button, textbox) — must be kept alive to avoid garbage collection.
    """
    pos = anchor_ax.get_position()     # Bbox in figure coordinates

    # ── Toggle button ──
    btn_ax = fig.add_axes([0.01, pos.y1 - 0.022, 0.10, 0.018])
    btn_ax.set_facecolor(BG)
    btn = Button(btn_ax, f"● {label}", color=colour, hovercolor="#555")
    for txt in btn_ax.texts:
        txt.set_fontsize(7)

    # ── "Win (s):" label ──
    lbl_ax = fig.add_axes([0.01, pos.y1 - 0.044, 0.10, 0.010])
    lbl_ax.set_facecolor(BG)
    lbl_ax.axis("off")
    lbl_ax.text(
        0.5, 0.5, "Win (s):", color="gray", fontsize=6,
        ha="center", va="center", transform=lbl_ax.transAxes,
    )

    # ── Numeric text input ──
    txt_ax = fig.add_axes([0.025, pos.y1 - 0.063, 0.06, 0.016])
    txt_ax.set_facecolor("#2a2a4e")
    tb = TextBox(txt_ax, "", initial=f"{default_win:.1f}",
                 color="#2a2a4e", hovercolor="#3a3a5e")
    tb.text_disp.set_color("white")
    tb.text_disp.set_fontsize(8)

    # ── Callbacks ──
    def _toggle(_event):
        on_state[0] = not on_state[0]
        marker = "●" if on_state[0] else "○"
        btn.label.set_text(f"{marker} {label}")
        btn.color = colour if on_state[0] else "#444"

    def _submit(text):
        try:
            val = float(text)
            if 0.3 <= val <= 60.0:
                win_state[0] = val
        except ValueError:
            pass

    btn.on_clicked(_toggle)
    tb.on_submit(_submit)
    return btn, tb


# Create controls (keep references so widgets stay alive)
_emo_btn, _emo_tb = _make_group_controls(
    axes[2], "EMOTION", EMO_WIN_DEFAULT, _emo_on, _emo_win, "#FFD700",
)
_pros_btn, _pros_tb = _make_group_controls(
    axes[2 + N_EMO], "PROSODY", DISP_SEC_DEFAULT, _pros_on, _disp_sec, "cyan",
)


# ════════════════════════════════════════════════════════════════════
# Animation update
# ════════════════════════════════════════════════════════════════════

def _update(_frame):
    """Called every UPDATE_MS by FuncAnimation (main thread)."""

    # ── 1. Audio buffer ────────────────────────────────────────────
    with _lock:
        if not _chunks:
            return
        audio = np.concatenate(list(_chunks))

    max_samp = int(MAX_AUDIO_SEC * SR)
    if len(audio) > max_samp:
        audio = audio[-max_samp:]
        with _lock:
            _chunks.clear()
            _chunks.append(audio.copy())

    total_sec = len(audio) / SR
    audio_f32 = audio.astype(np.float32)
    peak = np.max(np.abs(audio_f32)) if len(audio_f32) else 0.0

    # ── 2. Time mapping ────────────────────────────────────────────
    elapsed = time.time() - _t0
    disp = _disp_sec[0]
    x_min = max(0, elapsed - disp)
    x_max = max(disp, elapsed)
    offset = elapsed - total_sec        # maps audio-relative → display time

    for ax in axes:
        ax.set_xlim(x_min, x_max)

    # ── 3. Waveform (always on) ────────────────────────────────────
    wave_t = np.linspace(0, total_sec, len(audio)) + offset
    _ln_wave.set_data(wave_t, audio_f32)

    # ── 4. VAD contour (always on) ─────────────────────────────────
    vad_t, vad_p = _vad_contour(audio_f32)
    vad_disp = vad_t + offset
    _ln_vad.set_data(vad_disp, vad_p)
    if _fill_vad[0] is not None:
        _fill_vad[0].remove()
    if len(vad_p) > 0:
        _fill_vad[0] = ax_vad.fill_between(
            vad_disp, 0, vad_p, alpha=0.25, color="#88FF88", lw=0,
        )
    else:
        _fill_vad[0] = None

    # ── 5. Prosody LLD (if enabled) ───────────────────────────────
    if _pros_on[0]:
        try:
            df = _smile.process_signal(audio_f32, sampling_rate=SR)
        except Exception:
            df = None

        if df is not None:
            starts = np.array([t.total_seconds()
                               for t in df.index.get_level_values("start")])
            ends = np.array([t.total_seconds()
                             for t in df.index.get_level_values("end")])
            frame_t = (starts + ends) / 2.0
            speech_mask = _vad_mask(audio_f32, frame_t)
            silence = ~speech_mask
            lld_t = frame_t + offset

            for i, (key, _label, colour, is_nz, _ylim) in enumerate(PROS):
                vals = df[key].values.astype(np.float32).copy()
                if is_nz:
                    vals[vals <= 0] = np.nan
                    vals[silence] = np.nan
                _pros_lines[i].set_data(lld_t, vals)

                if not is_nz:
                    if _pros_fills[i] is not None:
                        _pros_fills[i].remove()
                    _pros_fills[i] = _pros_axes[i].fill_between(
                        lld_t, 0, vals, alpha=0.3, color=colour, lw=0,
                    )
    else:
        # Prosody OFF — clear strips
        for i in range(N_PROS):
            _pros_lines[i].set_data([], [])
            if _pros_fills[i] is not None:
                _pros_fills[i].remove()
                _pros_fills[i] = None

    # ── 6. Emotion (from background thread deque) ──────────────────
    if _emo_on[0] and _emo_hist:
        # Snapshot the deque (thread-safe for CPython deque iteration)
        snap = list(_emo_hist)
        # Keep only points within the visible window
        snap = [h for h in snap if (h[0] - _t0) >= x_min]

        if snap:
            ts = np.array([h[0] - _t0 for h in snap])

            for i, dim in enumerate(EMO_DIMS):
                vals = np.array([h[1].get(dim, 0.0) for h in snap])
                _emo_lines[i].set_data(ts, vals)

                colour = EMO_COLORS.get(dim, "#4488FF")
                if _emo_fills[i][0] is not None:
                    _emo_fills[i][0].remove()
                _emo_fills[i][0] = _emo_axes[i].fill_between(
                    ts, 0, vals, step="post", alpha=0.25, color=colour, lw=0,
                )

            # Show dominant emotion in group title
            last = snap[-1]
            axes[2].set_title(
                f"── EMOTION ── {last[2].upper()} ({last[3]:.0%})",
                color="#FFD700", fontsize=8, loc="left", pad=2,
            )
    elif not _emo_on[0]:
        # Emotion OFF — clear strips
        for i in range(N_EMO):
            _emo_lines[i].set_data([], [])
            if _emo_fills[i][0] is not None:
                _emo_fills[i][0].remove()
                _emo_fills[i][0] = None
        axes[2].set_title(
            "── EMOTION (OFF) ──", color="#666", fontsize=8, loc="left", pad=2,
        )

    # ── 7. Status bar ──────────────────────────────────────────────
    n_emo_pts = len(_emo_hist)
    emo_tag = f"emo={n_emo_pts}pts" if _emo_on[0] else "emo=OFF"
    pros_tag = "pros=ON" if _pros_on[0] else "pros=OFF"
    fig.suptitle(
        f"Speech Analysis — {total_sec:.1f}s buf  "
        f"peak={peak:.4f}  {emo_tag}  {pros_tag}",
        fontsize=9, color="white",
    )


# ════════════════════════════════════════════════════════════════════
# Cleanup & signal handling
# ════════════════════════════════════════════════════════════════════
_stream: sd.InputStream | None = None


def _cleanup():
    """Tear down audio + threads — must run before exit."""
    global _stream
    _emo_stop.set()
    if _stream is not None:
        try:
            _stream.abort()
            _stream.close()
        except Exception:
            pass
        _stream = None
    print("\nAudio stream closed. Clean exit.")


def _sig_handler(signum, _frame):
    _cleanup()
    sys.exit(0)


signal.signal(signal.SIGINT,  _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)


# ════════════════════════════════════════════════════════════════════
# Go
# ════════════════════════════════════════════════════════════════════
print("Starting … speak into the mic!")
print("Close the window or press Ctrl-C to stop.\n")

try:
    _stream = sd.InputStream(
        samplerate=SR, channels=1, dtype="float32",
        blocksize=int(SR * 0.05), callback=_audio_cb,
    )
    _stream.start()

    # Emotion inference in background
    _emo_thread = threading.Thread(target=_emotion_loop, daemon=True)
    _emo_thread.start()

    _anim = FuncAnimation(
        fig, _update, interval=UPDATE_MS,
        blit=False, cache_frame_data=False,
    )
    plt.show()      # blocks until window closed

finally:
    _cleanup()

print("Done.")

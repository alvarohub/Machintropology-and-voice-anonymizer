#!/usr/bin/env python3
"""
Real-time Speech Analysis — Emotion + Prosody + VAD  (v2)

Integrates:
  - emotion2vec (9-class utterance-level emotion, background thread)
  - openSMILE eGeMAPSv02 LLD (frame-level prosody, main thread)
  - Silero VAD (voice activity detection, main thread)

Architecture (from proven test_lld_vad_realtime.py):
  - sounddevice callback → shared audio accumulator
  - FuncAnimation: full buffer → openSMILE LLD + VAD (main thread)
  - Background thread: audio window → emotion2vec → result deque

Layout (8 rows):
  Waveform (tall) — raw audio + VAD-shaded speech regions
  Emotion (tall)  — 9 overlaid colour-coded step curves, 0–1 range
  F0 / Loudness / Jitter / Shimmer / HNR — 5 prosody LLD strips

Each group (EMOTION, PROSODY) has a toggle button + numeric text input
for the window size, placed to the left of the group's first axis.

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
UPDATE_MS     = 200         # FuncAnimation period (ms)
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


def _vad_segments(audio: np.ndarray):
    """Return list of (start_sec, end_sec) speech segments.

    Uses get_speech_timestamps — ONE forward pass for the entire buffer,
    much faster than the per-window approach (~375 calls → 1 call).
    """
    with _torch_lock:
        timestamps = _get_speech_timestamps(
            torch.from_numpy(audio).float(), _vad_model,
            threshold=VAD_THRESHOLD, sampling_rate=SR,
            min_speech_duration_ms=250,
        )
        _vad_model.reset_states()
    return [(seg["start"] / SR, seg["end"] / SR) for seg in timestamps]


def _vad_mask_from_segs(segs, frame_times: np.ndarray) -> np.ndarray:
    """Boolean mask (True = speech) from pre-computed segments."""
    mask = np.zeros(len(frame_times), dtype=bool)
    for t0, t1 in segs:
        mask |= (frame_times >= t0) & (frame_times <= t1)
    return mask


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
_emo_on   = [True]              # emotion group toggle
_pros_on  = [True]              # prosody group toggle
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
# Figure layout  (8 rows: waveform + emotion + 5 prosody)
# ════════════════════════════════════════════════════════════════════
BG       = "#1a1a2e"
N_PROS   = len(PROS)
N_ROWS   = 2 + N_PROS   # waveform, emotion (single), 5 prosody

heights  = [2, 3] + [1] * N_PROS

fig, axes = plt.subplots(
    N_ROWS, 1,
    figsize=(14, 11),
    sharex=True,
    gridspec_kw={"height_ratios": heights},
)
fig.patch.set_facecolor(BG)
fig.subplots_adjust(left=0.12, right=0.96, top=0.96, bottom=0.04, hspace=0.35)

_t0 = time.time()      # timeline origin


# ── Row 0: Waveform + VAD spans ────────────────────────────────────
ax_wave = axes[0]
ax_wave.set_facecolor(BG)
ax_wave.set_ylabel("Waveform", color="white", fontsize=8)
ax_wave.set_ylim(-0.15, 0.15)
ax_wave.tick_params(colors="gray", labelsize=6)
_ln_wave, = ax_wave.plot([], [], color="gray", lw=0.3)
_vad_spans = []     # list of axvspan artists (cleared each frame)


# ── Row 1: Emotion — single axis, 9 overlaid curves ───────────────
ax_emo = axes[1]
ax_emo.set_facecolor(BG)
ax_emo.set_ylabel("Emotion", color="#FFD700", fontsize=8)
ax_emo.set_ylim(0, 1.05)
ax_emo.tick_params(colors="gray", labelsize=6)

_emo_lines = {}     # dim_name → Line2D
for dim in EMO_DIMS:
    colour = EMO_COLORS.get(dim, "#4488FF")
    ln, = ax_emo.plot([], [], color=colour, lw=1.5, alpha=0.7,
                      drawstyle="steps-post", label=dim.capitalize())
    _emo_lines[dim] = ln

# Compact legend inside the axis
ax_emo.legend(
    loc="upper right", fontsize=5, ncol=3,
    framealpha=0.3, labelcolor="linecolor",
    facecolor=BG, edgecolor="#444",
)


# ── Rows 2–6: Prosody strips ──────────────────────────────────────
_pros_axes  = []
_pros_lines = []
_pros_fills = [None] * N_PROS

for i, (key, label, colour, is_nz, ylim) in enumerate(PROS):
    ax = axes[2 + i]
    ax.set_facecolor(BG)
    ax.set_ylabel(label, color=colour, fontsize=8)
    ax.set_ylim(ylim)
    ax.tick_params(colors="gray", labelsize=6)
    ln, = ax.plot([], [], color=colour, lw=1.5)
    _pros_axes.append(ax)
    _pros_lines.append(ln)


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

    Returns (button, textbox) — must be kept alive to avoid GC.
    """
    pos = anchor_ax.get_position()     # Bbox in figure coords

    # ── Toggle button ──
    btn_ax = fig.add_axes([0.005, pos.y1 - 0.03, 0.07, 0.025])
    btn_ax.set_facecolor(BG)
    btn = Button(btn_ax, f"● {label}", color=colour, hovercolor="#555")
    for txt in btn_ax.texts:
        txt.set_fontsize(7)

    # ── "Win (s):" label ──
    lbl_ax = fig.add_axes([0.005, pos.y1 - 0.055, 0.07, 0.012])
    lbl_ax.set_facecolor(BG)
    lbl_ax.axis("off")
    lbl_ax.text(
        0.5, 0.5, "Win (s):", color="gray", fontsize=6,
        ha="center", va="center", transform=lbl_ax.transAxes,
    )

    # ── Numeric text input ──
    txt_ax = fig.add_axes([0.015, pos.y1 - 0.078, 0.05, 0.018])
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
    ax_emo, "EMOTION", EMO_WIN_DEFAULT, _emo_on, _emo_win, "#FFD700",
)
_pros_btn, _pros_tb = _make_group_controls(
    axes[2], "PROSODY", DISP_SEC_DEFAULT, _pros_on, _disp_sec, "cyan",
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

    # ── 3. VAD segments (one fast call) ────────────────────────────
    vad_segs = _vad_segments(audio_f32)

    # ── 4. Waveform + VAD shading ──────────────────────────────────
    wave_t = np.linspace(0, total_sec, len(audio)) + offset
    _ln_wave.set_data(wave_t, audio_f32)

    # Remove old VAD spans, draw new ones
    for sp in _vad_spans:
        sp.remove()
    _vad_spans.clear()
    for t0, t1 in vad_segs:
        sp = ax_wave.axvspan(t0 + offset, t1 + offset,
                             alpha=0.15, color="#88FF88", lw=0)
        _vad_spans.append(sp)

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
            speech_mask = _vad_mask_from_segs(vad_segs, frame_t)
            silence = ~speech_mask
            lld_t = frame_t + offset

            for i, (key, _label, colour, is_nz, _ylim) in enumerate(PROS):
                vals = df[key].values.astype(np.float32).copy()
                if is_nz:
                    vals[vals <= 0] = np.nan
                    vals[silence] = np.nan
                _pros_lines[i].set_data(lld_t, vals)

                # Loudness fill (only non-nz feature)
                if not is_nz:
                    if _pros_fills[i] is not None:
                        _pros_fills[i].remove()
                    _pros_fills[i] = _pros_axes[i].fill_between(
                        lld_t, 0, vals, alpha=0.3, color=colour, lw=0,
                    )
    else:
        for i in range(N_PROS):
            _pros_lines[i].set_data([], [])
            if _pros_fills[i] is not None:
                _pros_fills[i].remove()
                _pros_fills[i] = None

    # ── 6. Emotion — single axis, all 9 curves ────────────────────
    if _emo_on[0] and _emo_hist:
        snap = list(_emo_hist)
        snap = [h for h in snap if (h[0] - _t0) >= x_min]

        if snap:
            ts = np.array([h[0] - _t0 for h in snap])
            for dim in EMO_DIMS:
                vals = np.array([h[1].get(dim, 0.0) for h in snap])
                _emo_lines[dim].set_data(ts, vals)

            # Highlight dominant with thick line, dim others
            last = snap[-1]
            top_dim = last[2]
            for dim in EMO_DIMS:
                is_top = (dim == top_dim)
                _emo_lines[dim].set_linewidth(2.5 if is_top else 1.0)
                _emo_lines[dim].set_alpha(1.0 if is_top else 0.4)

            ax_emo.set_ylabel(
                f"Emotion — {top_dim.upper()} {last[3]:.0%}",
                color=EMO_COLORS.get(top_dim, "#FFD700"), fontsize=8,
            )
    elif not _emo_on[0]:
        for dim in EMO_DIMS:
            _emo_lines[dim].set_data([], [])
        ax_emo.set_ylabel("Emotion (OFF)", color="#666", fontsize=8)

    # ── 7. Status bar ──────────────────────────────────────────────
    n_emo_pts = len(_emo_hist)
    emo_tag = f"emo={n_emo_pts}pts" if _emo_on[0] else "emo=OFF"
    pros_tag = "pros=ON" if _pros_on[0] else "pros=OFF"
    n_speech = len(vad_segs)
    fig.suptitle(
        f"Speech Analysis — {total_sec:.1f}s buf  "
        f"peak={peak:.4f}  {emo_tag}  {pros_tag}  "
        f"vad_segs={n_speech}",
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

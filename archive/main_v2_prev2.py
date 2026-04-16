#!/usr/bin/env python3
"""
Real-time Speech Analysis — Emotion + Prosody + VAD  (v2)

Architecture — three decoupled layers:

  1. COMPUTATION (background threads, run at their own pace):
     - emotion2vec thread → writes to _latest_emo dict
     - prosody/VAD thread → writes to _latest_pros dict + _latest_vad

  2. SAMPLER (fixed-rate timer in FuncAnimation):
     - Every 1/SAMPLE_HZ seconds, reads latest values → builds 15-d vector
     - Appends to scan buffer (fixed-length ring)
     - Portable: this vector can be saved, sent as OSC/MIDI, etc.

  3. DISPLAY (scan-mode EEG):
     - Single axis, 15 channels stacked like EEG bands
     - Fills left to right; clears and restarts on overflow
     - One set_data() per channel per frame — minimal draw cost

Channel map (top → bottom):
  VAD | Angry Disgusted Fearful Happy Neutral Other Sad Surprised Unknown | F0 Loud Jitter Shimmer HNR

Usage:
    conda activate ML311
    python main_v2.py
"""

from __future__ import annotations

import matplotlib
matplotlib.use("TkAgg")

import numpy as np
import sounddevice as sd
import opensmile
import torch
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button, TextBox
import threading
import time
import signal
import sys


# ════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════
SR            = 16000
MAX_AUDIO_SEC = 15.0
VAD_THRESHOLD = 0.3
SAMPLE_HZ     = 20          # sampler rate (display resolution)

# Defaults (editable at runtime)
EMO_WIN_SEC   = 2.0          # emotion2vec analysis window
PROS_WIN_SEC  = 2.0          # openSMILE processing window
SCAN_SEC      = 5.0          # display scan duration


# ════════════════════════════════════════════════════════════════════
# Channel definitions — single flat list drives everything
# ════════════════════════════════════════════════════════════════════
#  (name, color, source, key, lo, hi)
CHANNELS = [
    # VAD
    ("VAD",       "#88FF88", "vad",  "vad",                          0, 1),
    # Emotions
    ("Angry",     "#FF4444", "emo",  "angry",                        0, 1),
    ("Disgusted", "#88AA00", "emo",  "disgusted",                    0, 1),
    ("Fearful",   "#AA44FF", "emo",  "fearful",                      0, 1),
    ("Happy",     "#FFD700", "emo",  "happy",                        0, 1),
    ("Neutral",   "#4488FF", "emo",  "neutral",                      0, 1),
    ("Other",     "#888888", "emo",  "other",                        0, 1),
    ("Sad",       "#5566CC", "emo",  "sad",                          0, 1),
    ("Surprised", "#FF8800", "emo",  "surprised",                    0, 1),
    ("Unknown",   "#AAAAAA", "emo",  "unknown",                      0, 1),
    # Prosody LLD (openSMILE eGeMAPSv02)
    ("F0",        "cyan",    "pros", "F0semitoneFrom27.5Hz_sma3nz",  0, 50),
    ("Loudness",  "green",   "pros", "Loudness_sma3",                0, 2.5),
    ("Jitter",    "pink",    "pros", "jitterLocal_sma3nz",           0, 0.35),
    ("Shimmer",   "orange",  "pros", "shimmerLocaldB_sma3nz",        0, 30),
    ("HNR",       "violet",  "pros", "HNRdBACF_sma3nz",             0, 15),
]
N_CH = len(CHANNELS)  # 15

# nz features: openSMILE reports 0 when unvoiced — treat as NaN
_NZ_KEYS = {
    "F0semitoneFrom27.5Hz_sma3nz", "jitterLocal_sma3nz",
    "shimmerLocaldB_sma3nz", "HNRdBACF_sma3nz",
}

# Group boundary indices (for separator lines)
_EMO_START = 1     # first emotion channel index
_PROS_START = 10   # first prosody channel index


# ════════════════════════════════════════════════════════════════════
# Audio accumulator
# ════════════════════════════════════════════════════════════════════
_lock   = threading.Lock()
_chunks: list[np.ndarray] = []

# PyTorch thread-safety lock (Apple Silicon ARM crash prevention)
_torch_lock = threading.Lock()


def _audio_cb(indata, frames, time_info, status):
    with _lock:
        _chunks.append(indata[:, 0].copy())


def _get_audio(max_sec: float) -> np.ndarray | None:
    """Snapshot latest audio (up to max_sec). Returns None if empty."""
    with _lock:
        if not _chunks:
            return None
        buf = np.concatenate(list(_chunks))
    need = int(max_sec * SR)
    if len(buf) < int(0.3 * SR):
        return None
    return buf[-need:].astype(np.float32) if len(buf) >= need else buf.astype(np.float32)


def _trim_audio():
    """Keep audio buffer under MAX_AUDIO_SEC."""
    with _lock:
        if not _chunks:
            return
        buf = np.concatenate(_chunks)
        max_s = int(MAX_AUDIO_SEC * SR)
        if len(buf) > max_s:
            _chunks.clear()
            _chunks.append(buf[-max_s:])


# ════════════════════════════════════════════════════════════════════
# Silero VAD
# ════════════════════════════════════════════════════════════════════
print("Loading Silero VAD …")
_vad_model, _vad_utils = torch.hub.load(
    "snakers4/silero-vad", "silero_vad",
    trust_repo=True, force_reload=False,
)
_get_speech_ts = _vad_utils[0]
print("VAD ready.")


# ════════════════════════════════════════════════════════════════════
# emotion2vec
# ════════════════════════════════════════════════════════════════════
print("Loading emotion2vec …")
from emotion_model import Emotion2VecModel
_emo_model = Emotion2VecModel(model_name="iic/emotion2vec_plus_base", device="cpu")
print(f"emotion2vec ready — {len(_emo_model.dimensions)} classes")


# ════════════════════════════════════════════════════════════════════
# Shared latest values  (written by bg threads, read by sampler)
# ════════════════════════════════════════════════════════════════════
_latest_emo:  dict[str, float] = {}      # dim → score
_latest_pros: dict[str, float] = {}      # openSMILE col → value
_latest_vad  = [0.0]                     # 0.0 or 1.0
_latest_label = [""]                     # dominant emotion name
_latest_conf  = [0.0]                    # dominant emotion confidence

# Runtime toggles / params (mutable via widgets)
_emo_on   = [True]
_pros_on  = [True]
_emo_win  = [EMO_WIN_SEC]
_pros_win = [PROS_WIN_SEC]
_scan_sec = [SCAN_SEC]
_stop     = threading.Event()


# ════════════════════════════════════════════════════════════════════
# Background thread: emotion2vec
# ════════════════════════════════════════════════════════════════════

def _emotion_loop():
    last_t = 0.0
    while not _stop.is_set():
        time.sleep(0.08)
        if not _emo_on[0]:
            continue
        now = time.time()
        if now - last_t < _emo_win[0]:
            continue

        audio = _get_audio(_emo_win[0])
        if audio is None:
            continue

        try:
            with _torch_lock:
                r = _emo_model.predict(audio, sr=SR)
            _latest_emo.update(r["scores"])
            _latest_label[0] = r["label"]
            _latest_conf[0]  = r["confidence"]
        except Exception as exc:
            print(f"[emotion] {exc}", file=sys.stderr)

        last_t = now


# ════════════════════════════════════════════════════════════════════
# Background thread: prosody LLD + VAD
# ════════════════════════════════════════════════════════════════════

def _prosody_loop():
    # Thread-local openSMILE instance (C lib has process-global state)
    smile = opensmile.Smile(
        feature_set=opensmile.FeatureSet.eGeMAPSv02,
        feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
    )

    while not _stop.is_set():
        time.sleep(0.08)
        if not _pros_on[0]:
            continue

        audio = _get_audio(_pros_win[0])
        if audio is None:
            continue

        # ── openSMILE LLD ──
        try:
            df = smile.process_signal(audio, sampling_rate=SR)
        except Exception:
            continue
        if len(df) == 0:
            continue

        # ── VAD (single call, fast) ──
        with _torch_lock:
            segs = _get_speech_ts(
                torch.from_numpy(audio).float(), _vad_model,
                threshold=VAD_THRESHOLD, sampling_rate=SR,
                min_speech_duration_ms=250,
            )
            _vad_model.reset_states()
        has_speech = len(segs) > 0
        _latest_vad[0] = 1.0 if has_speech else 0.0

        # ── Extract latest values (mean of last ~5 frames = 100ms) ──
        n_tail = min(5, len(df))
        tail = df.iloc[-n_tail:]

        for _, _, src, key, _, _ in CHANNELS:
            if src != "pros":
                continue
            vals = tail[key].values
            if key in _NZ_KEYS:
                if has_speech:
                    voiced = vals[vals > 0]
                    _latest_pros[key] = float(np.mean(voiced)) if len(voiced) > 0 else np.nan
                else:
                    _latest_pros[key] = np.nan
            else:
                _latest_pros[key] = float(np.mean(vals))


# ════════════════════════════════════════════════════════════════════
# Sampler — builds the 15-d vector (decoupled from display)
# ════════════════════════════════════════════════════════════════════
_n_samples = [int(SCAN_SEC * SAMPLE_HZ)]
_buf       = [np.full((_n_samples[0], N_CH), np.nan)]
_cursor    = [0]
_x_fixed   = [np.linspace(0, SCAN_SEC, _n_samples[0])]


def _reset_scan():
    """Reinitialise scan buffer (on wrap or param change)."""
    n = int(_scan_sec[0] * SAMPLE_HZ)
    _n_samples[0] = n
    _buf[0] = np.full((n, N_CH), np.nan)
    _cursor[0] = 0
    _x_fixed[0] = np.linspace(0, _scan_sec[0], n)


def _sample_vector() -> np.ndarray:
    """Read latest values into a 15-d vector. Pure query, no compute."""
    vec = np.full(N_CH, np.nan)
    for j, (_, _, src, key, _, _) in enumerate(CHANNELS):
        if src == "vad":
            vec[j] = _latest_vad[0]
        elif src == "emo":
            vec[j] = _latest_emo.get(key, np.nan) if _emo_on[0] else np.nan
        elif src == "pros":
            vec[j] = _latest_pros.get(key, np.nan) if _pros_on[0] else np.nan
    return vec


# ════════════════════════════════════════════════════════════════════
# Display — one figure, one axis, EEG-style stacked bands
# ════════════════════════════════════════════════════════════════════
BG  = "#1a1a2e"
PAD = 0.05          # vertical padding within each band
BH  = 1.0 - 2*PAD   # usable band height

fig = plt.figure(figsize=(14, 10))
fig.patch.set_facecolor(BG)
ax = fig.add_axes([0.09, 0.07, 0.89, 0.88])
ax.set_facecolor(BG)
ax.set_xlim(0, SCAN_SEC)
ax.set_ylim(0, N_CH)
ax.set_xlabel("seconds", color="gray", fontsize=8)
ax.tick_params(axis="x", colors="gray", labelsize=7)

# ── Band dividers ──
for y in range(N_CH + 1):
    lw = 1.2 if y == (N_CH - _PROS_START) else 0.3
    c  = "#666" if y == (N_CH - _PROS_START) else "#333"
    ax.axhline(y, color=c, lw=lw)

# Thin line between VAD and emotion group
ax.axhline(N_CH - _EMO_START, color="#555", lw=0.8)

# ── Y-axis band labels (drawn once) ──
ax.set_yticks([N_CH - 1 - j + 0.5 for j in range(N_CH)])
ax.set_yticklabels([c[0] for c in CHANNELS])
for j, label in enumerate(ax.get_yticklabels()):
    label.set_color(CHANNELS[j][1])
    label.set_fontsize(7)
ax.tick_params(axis="y", length=0)   # hide tick marks, keep labels

# ── Group labels ──
emo_mid = (N_CH - _EMO_START + N_CH - _PROS_START) / 2 / N_CH
pros_mid = (N_CH - _PROS_START) / 2 / N_CH
ax.text(-0.065, emo_mid, "EMOTION", transform=ax.transAxes,
        rotation=90, color="#FFD700", fontsize=8, ha="center", va="center",
        fontweight="bold")
ax.text(-0.065, pros_mid, "PROSODY", transform=ax.transAxes,
        rotation=90, color="cyan", fontsize=8, ha="center", va="center",
        fontweight="bold")

# ── Grid ──
ax.grid(True, axis="x", alpha=0.12, color="gray")
for sp in ax.spines.values():
    sp.set_color("#333")

# ── Pre-create lines (one per channel) ──
_lines = []
for j, (_, color, *_rest) in enumerate(CHANNELS):
    ln, = ax.plot([], [], color=color, lw=1.2)
    _lines.append(ln)

# ── Scan cursor (vertical line) ──
_cursor_line = ax.axvline(0, color="#ffffff", lw=0.8, alpha=0.3)


# ════════════════════════════════════════════════════════════════════
# Control widgets — bottom row
# ════════════════════════════════════════════════════════════════════
_widgets = []   # prevent garbage collection

def _make_toggle(x, label, on_state, color):
    bx = fig.add_axes([x, 0.012, 0.07, 0.028])
    bx.set_facecolor(BG)
    b = Button(bx, f"● {label}", color=color, hovercolor="#555")
    for t in bx.texts:
        t.set_fontsize(7)

    def _cb(_ev):
        on_state[0] = not on_state[0]
        mk = "●" if on_state[0] else "○"
        b.label.set_text(f"{mk} {label}")
        b.color = color if on_state[0] else "#444"
    b.on_clicked(_cb)
    _widgets.append(b)
    return b

def _make_textbox(x, label_text, initial, state_list, lo=0.3, hi=60.0):
    # Label
    lx = fig.add_axes([x, 0.012, 0.04, 0.028])
    lx.set_facecolor(BG); lx.axis("off")
    lx.text(0.9, 0.5, label_text, color="gray", fontsize=6,
            ha="right", va="center", transform=lx.transAxes)

    # TextBox
    tx = fig.add_axes([x + 0.04, 0.012, 0.04, 0.028])
    tx.set_facecolor("#2a2a4e")
    tb = TextBox(tx, "", initial=f"{initial:.1f}",
                 color="#2a2a4e", hovercolor="#3a3a5e")
    tb.text_disp.set_color("white")
    tb.text_disp.set_fontsize(8)

    def _cb(text):
        try:
            v = float(text)
            if lo <= v <= hi:
                state_list[0] = v
        except ValueError:
            pass
    tb.on_submit(_cb)
    _widgets.append(tb)
    return tb

# Layout: toggle, win input, toggle, win input, scan input
_make_toggle(0.09,  "EMO",  _emo_on,  "#FFD700")
_make_textbox(0.17, "win:", EMO_WIN_SEC,  _emo_win)
_make_toggle(0.30,  "PROS", _pros_on, "cyan")
_make_textbox(0.38, "win:", PROS_WIN_SEC, _pros_win)
_make_textbox(0.55, "scan:", SCAN_SEC,    _scan_sec)


# ════════════════════════════════════════════════════════════════════
# Animation update — sample + draw (main thread)
# ════════════════════════════════════════════════════════════════════
_last_scan = [SCAN_SEC]


def _update(_frame):
    # ── Handle scan duration change ──
    if _scan_sec[0] != _last_scan[0]:
        _reset_scan()
        ax.set_xlim(0, _scan_sec[0])
        _last_scan[0] = _scan_sec[0]

    # ── Trim audio buffer ──
    _trim_audio()

    # ── Sample ──
    vec = _sample_vector()
    idx = _cursor[0]
    n   = _n_samples[0]

    _buf[0][idx] = vec
    _cursor[0] = idx + 1

    # ── Wrap: clear buffer on overflow ──
    if _cursor[0] >= n:
        _buf[0][:] = np.nan
        _cursor[0] = 0

    # ── Update cursor line ──
    cx = _cursor[0] / SAMPLE_HZ
    _cursor_line.set_xdata([cx, cx])

    # ── Update lines ──
    x = _x_fixed[0]
    data = _buf[0]                          # (n_samples, N_CH)
    for j in range(N_CH):
        _, _, _, _, lo, hi = CHANNELS[j]
        raw = data[:, j]
        norm = np.clip((raw - lo) / (hi - lo + 1e-9), 0, 1)
        y = (N_CH - 1 - j) + PAD + BH * norm
        _lines[j].set_data(x, y)

    # ── Title: dominant emotion + status ──
    lbl = _latest_label[0] or "—"
    conf = _latest_conf[0]
    vad_str = "speech" if _latest_vad[0] > 0.5 else "silence"
    fig.suptitle(
        f"{lbl.upper()} {conf:.0%}  |  {vad_str}  |  "
        f"emo_win={_emo_win[0]:.1f}s  pros_win={_pros_win[0]:.1f}s  "
        f"scan={_scan_sec[0]:.0f}s",
        fontsize=9, color="white",
    )


# ════════════════════════════════════════════════════════════════════
# Cleanup & signal handling
# ════════════════════════════════════════════════════════════════════
_stream: sd.InputStream | None = None


def _cleanup():
    global _stream
    _stop.set()
    if _stream is not None:
        try:
            _stream.abort(); _stream.close()
        except Exception:
            pass
        _stream = None
    print("\nClean exit.")


def _sig(signum, _f):
    _cleanup(); sys.exit(0)

signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


# ════════════════════════════════════════════════════════════════════
# Go
# ════════════════════════════════════════════════════════════════════
print("Starting … speak into the mic!")
print("Close the window or Ctrl-C to stop.\n")

try:
    _stream = sd.InputStream(
        samplerate=SR, channels=1, dtype="float32",
        blocksize=int(SR * 0.05), callback=_audio_cb,
    )
    _stream.start()

    threading.Thread(target=_emotion_loop, daemon=True).start()
    threading.Thread(target=_prosody_loop, daemon=True).start()

    _anim = FuncAnimation(
        fig, _update, interval=int(1000 / SAMPLE_HZ),
        blit=False, cache_frame_data=False,
    )
    plt.show()
finally:
    _cleanup()

print("Done.")

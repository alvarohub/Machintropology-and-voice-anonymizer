#!/usr/bin/env python3
"""
Real-time Speech Analysis — Emotion + Prosody + VAD  (v2)

Architecture — four decoupled layers:

  1. COMPUTE  (background threads, own pace)
     emotion2vec → _latest_emo | openSMILE+VAD → _latest_pros, _latest_vad

  2. SAMPLER  (fixed-rate, main thread)
     Every 1/SAMPLE_HZ: reads latest values → 15-d vector → scan buffer

  3. OUTPUT   (driven by sampler, pluggable)
     - CSV logging (toggle)
     - OSC streaming (toggle)
     - Save-as snapshot

  4. DISPLAY  (scan-mode EEG, one axis, minimal draw)
     Small compact graph; right-side control panel

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
import collections
import threading
import time
import signal
import sys
import os
import csv
from datetime import datetime


# ════════════════════════════════════════════════════════════════════
# 0. CONFIGURATION
# ════════════════════════════════════════════════════════════════════
SR            = 16000
MAX_AUDIO_SEC = 15.0
VAD_THRESHOLD = 0.3
SAMPLE_HZ     = 10           # sampler/display rate (Hz)

# Defaults (editable at runtime via text inputs)
EMO_WIN_SEC   = 2.0          # emotion2vec analysis window
PROS_WIN_SEC  = 0.5          # openSMILE processing window
SCAN_SEC      = 5.0          # display scan window (seconds)

OUTPUT_DIR    = "output"
OSC_IP        = "127.0.0.1"
OSC_PORT      = 9000
OSC_PREFIX    = "/speech"     # → /speech/emo, /speech/pros, /speech/vad


# ════════════════════════════════════════════════════════════════════
# 1a. CHANNEL MAP  — drives sampling, display, logging
# ════════════════════════════════════════════════════════════════════
#  (name, color, source, key, lo, hi)
CHANNELS = [
    ("VAD",       "#88FF88", "vad",  "vad",                          0, 1),
    ("Angry",     "#FF4444", "emo",  "angry",                        0, 1),
    ("Disgusted", "#88AA00", "emo",  "disgusted",                    0, 1),
    ("Fearful",   "#AA44FF", "emo",  "fearful",                      0, 1),
    ("Happy",     "#FFD700", "emo",  "happy",                        0, 1),
    ("Neutral",   "#4488FF", "emo",  "neutral",                      0, 1),
    ("Other",     "#888888", "emo",  "other",                        0, 1),
    ("Sad",       "#5566CC", "emo",  "sad",                          0, 1),
    ("Surprised", "#FF8800", "emo",  "surprised",                    0, 1),
    ("Unknown",   "#AAAAAA", "emo",  "unknown",                      0, 1),
    ("F0",        "cyan",    "pros", "F0semitoneFrom27.5Hz_sma3nz",  0, 50),
    ("Loudness",  "green",   "pros", "Loudness_sma3",                0, 2.5),
    ("Jitter",    "pink",    "pros", "jitterLocal_sma3nz",           0, 0.35),
    ("Shimmer",   "orange",  "pros", "shimmerLocaldB_sma3nz",        0, 30),
    ("HNR",       "violet",  "pros", "HNRdBACF_sma3nz",             0, 15),
]
N_CH = len(CHANNELS)
CH_NAMES = [c[0] for c in CHANNELS]

_NZ_KEYS = {
    "F0semitoneFrom27.5Hz_sma3nz", "jitterLocal_sma3nz",
    "shimmerLocaldB_sma3nz", "HNRdBACF_sma3nz",
}
_EMO_START  = 1
_PROS_START = 10


# ════════════════════════════════════════════════════════════════════
# 1b. AUDIO ACCUMULATOR
# ════════════════════════════════════════════════════════════════════
_lock   = threading.Lock()
_chunks: list[np.ndarray] = []
_torch_lock = threading.Lock()       # Apple Silicon ARM crash guard


def _audio_cb(indata, frames, time_info, status):
    with _lock:
        _chunks.append(indata[:, 0].copy())


def _get_audio(max_sec: float) -> np.ndarray | None:
    with _lock:
        if not _chunks:
            return None
        buf = np.concatenate(list(_chunks))
    need = int(max_sec * SR)
    if len(buf) < int(0.3 * SR):
        return None
    return buf[-need:].astype(np.float32) if len(buf) >= need else buf.astype(np.float32)


def _trim_audio():
    with _lock:
        if not _chunks:
            return
        buf = np.concatenate(_chunks)
        max_s = int(MAX_AUDIO_SEC * SR)
        if len(buf) > max_s:
            _chunks.clear()
            _chunks.append(buf[-max_s:])


# ════════════════════════════════════════════════════════════════════
# 1c. MODEL LOADING
# ════════════════════════════════════════════════════════════════════
print("Loading Silero VAD …")
_vad_model, _vad_utils = torch.hub.load(
    "snakers4/silero-vad", "silero_vad",
    trust_repo=True, force_reload=False,
)
_get_speech_ts = _vad_utils[0]
print("VAD ready.")

print("Loading emotion2vec …")
from emotion_model import Emotion2VecModel
_emo_model = Emotion2VecModel(model_name="iic/emotion2vec_plus_base", device="cpu")
print(f"emotion2vec ready — {len(_emo_model.dimensions)} classes")


# ════════════════════════════════════════════════════════════════════
# 1d. SHARED STATE  (written by compute, read by sampler)
# ════════════════════════════════════════════════════════════════════
_latest_emo:   dict[str, float] = {}
_latest_pros:  dict[str, float] = {}
_latest_vad   = [0.0]
_latest_label = [""]
_latest_conf  = [0.0]

# Runtime toggles (mutable via widgets)
_emo_on   = [True]
_pros_on  = [True]
_emo_win  = [EMO_WIN_SEC]
_pros_win = [PROS_WIN_SEC]
_scan_sec = [SCAN_SEC]
_stop     = threading.Event()


# ════════════════════════════════════════════════════════════════════
# 2. COMPUTE THREADS
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


def _prosody_loop():
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
        try:
            df = smile.process_signal(audio, sampling_rate=SR)
        except Exception:
            continue
        if len(df) == 0:
            continue
        with _torch_lock:
            segs = _get_speech_ts(
                torch.from_numpy(audio).float(), _vad_model,
                threshold=VAD_THRESHOLD, sampling_rate=SR,
                min_speech_duration_ms=250,
            )
            _vad_model.reset_states()
        has_speech = len(segs) > 0
        _latest_vad[0] = 1.0 if has_speech else 0.0
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
# 3. SAMPLER  — pure data, independent of display
# ════════════════════════════════════════════════════════════════════
_n_samples = [int(SCAN_SEC * SAMPLE_HZ)]
_buf       = [np.full((_n_samples[0], N_CH), np.nan)]
_cursor    = [0]
_x_fixed   = [np.linspace(0, SCAN_SEC, _n_samples[0])]


def _reset_scan():
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
# 4. OUTPUT SINKS  — CSV logger  +  OSC streamer
# ════════════════════════════════════════════════════════════════════

# ── CSV Logger ─────────────────────────────────────────────────────
_log_on    = [False]
_log_file  = [None]     # open file handle
_log_writer = [None]    # csv.writer


def log_start():
    """Open a timestamped CSV in OUTPUT_DIR."""
    if _log_file[0] is not None:
        return  # already open
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"track_{ts}.csv")
    f = open(path, "w", newline="")
    w = csv.writer(f)
    w.writerow(["timestamp", "dominant_emo", "confidence"] + CH_NAMES)
    _log_file[0] = f
    _log_writer[0] = w
    _log_on[0] = True
    print(f"[LOG] recording → {path}")


def log_stop():
    _log_on[0] = False
    if _log_file[0] is not None:
        _log_file[0].close()
        print("[LOG] stopped")
    _log_file[0] = None
    _log_writer[0] = None


def log_vector(vec: np.ndarray):
    """Append one sample to the CSV (called from sampler)."""
    if not _log_on[0] or _log_writer[0] is None:
        return
    row = [
        f"{time.time():.3f}",
        _latest_label[0],
        f"{_latest_conf[0]:.3f}",
    ] + [f"{v:.4f}" if not np.isnan(v) else "" for v in vec]
    _log_writer[0].writerow(row)


# ── OSC Streamer ───────────────────────────────────────────────────
_osc_on     = [False]
_osc_client = [None]


def osc_start():
    try:
        from pythonosc.udp_client import SimpleUDPClient
        _osc_client[0] = SimpleUDPClient(OSC_IP, OSC_PORT)
        _osc_on[0] = True
        print(f"[OSC] streaming → {OSC_IP}:{OSC_PORT}")
    except ImportError:
        print("[OSC] python-osc not installed. Run: pip install python-osc")
        _osc_on[0] = False


def osc_stop():
    _osc_on[0] = False
    _osc_client[0] = None
    print("[OSC] stopped")


def osc_send(vec: np.ndarray):
    """Send one sample as OSC messages."""
    if not _osc_on[0] or _osc_client[0] is None:
        return
    c = _osc_client[0]
    try:
        # emotion scores as a single list
        emo_vals = [float(v) if not np.isnan(v) else 0.0
                    for v in vec[_EMO_START : _PROS_START]]
        c.send_message(f"{OSC_PREFIX}/emo", emo_vals)

        # dominant emotion
        c.send_message(f"{OSC_PREFIX}/label",
                       [_latest_label[0], float(_latest_conf[0])])

        # prosody as a single list
        pros_vals = [float(v) if not np.isnan(v) else 0.0
                     for v in vec[_PROS_START:]]
        c.send_message(f"{OSC_PREFIX}/pros", pros_vals)

        # VAD
        c.send_message(f"{OSC_PREFIX}/vad", [float(vec[0])])
    except Exception as exc:
        print(f"[OSC] {exc}", file=sys.stderr)


# ── Save-as snapshot ───────────────────────────────────────────────
def save_snapshot():
    """Save current scan buffer to CSV (manual trigger)."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"snapshot_{ts}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sample_idx"] + CH_NAMES)
        data = _buf[0]
        for i in range(len(data)):
            row = [str(i)] + [f"{v:.4f}" if not np.isnan(v) else "" for v in data[i]]
            w.writerow(row)
    print(f"[SAVE] snapshot → {path}")


# ════════════════════════════════════════════════════════════════════
# 5. DISPLAY  — compact graph + right control panel
# ════════════════════════════════════════════════════════════════════
BG  = "#1a1a2e"
PAD = 0.05
BH  = 1.0 - 2 * PAD

fig = plt.figure(figsize=(14, 8))
fig.patch.set_facecolor(BG)

# Main graph: left 75% of canvas, vertically centered
ax = fig.add_axes([0.07, 0.10, 0.64, 0.82])
ax.set_facecolor(BG)
ax.set_xlim(0, SCAN_SEC)
ax.set_ylim(0, N_CH)
ax.set_xlabel("seconds", color="gray", fontsize=8)
ax.tick_params(axis="x", colors="gray", labelsize=7)

# Band dividers
for y in range(N_CH + 1):
    lw = 1.2 if y in (N_CH - _PROS_START, N_CH - _EMO_START) else 0.3
    c  = "#555" if lw > 0.5 else "#333"
    ax.axhline(y, color=c, lw=lw)

# Y labels
ax.set_yticks([N_CH - 1 - j + 0.5 for j in range(N_CH)])
ax.set_yticklabels(CH_NAMES)
for j, label in enumerate(ax.get_yticklabels()):
    label.set_color(CHANNELS[j][1])
    label.set_fontsize(7)
ax.tick_params(axis="y", length=0)

# Group labels
emo_mid = (N_CH - _EMO_START + N_CH - _PROS_START) / 2 / N_CH
pros_mid = (N_CH - _PROS_START) / 2 / N_CH
ax.text(-0.08, emo_mid, "EMOTION", transform=ax.transAxes,
        rotation=90, color="#FFD700", fontsize=8, ha="center", va="center",
        fontweight="bold")
ax.text(-0.08, pros_mid, "PROSODY", transform=ax.transAxes,
        rotation=90, color="cyan", fontsize=8, ha="center", va="center",
        fontweight="bold")
ax.text(-0.08, (N_CH - 0.5) / N_CH, "VAD", transform=ax.transAxes,
        rotation=90, color="#88FF88", fontsize=8, ha="center", va="center",
        fontweight="bold")

ax.grid(True, axis="x", alpha=0.12, color="gray")
for sp in ax.spines.values():
    sp.set_color("#333")

# Lines (one per channel)
_lines = []
for j, (_, color, *_) in enumerate(CHANNELS):
    ln, = ax.plot([], [], color=color, lw=1.2)
    _lines.append(ln)

# Scan cursor
_cursor_line = ax.axvline(0, color="#ffffff", lw=0.8, alpha=0.3)


# ════════════════════════════════════════════════════════════════════
# 5b. CONTROLS — right panel (stacked buttons + text inputs)
# ════════════════════════════════════════════════════════════════════
_widgets = []

# ── Helper factories ───────────────────────────────────────────────
def _btn(rect, label, color, cb):
    bx = fig.add_axes(rect)
    bx.set_facecolor(BG)
    b = Button(bx, label, color=color, hovercolor="#555")
    for t in bx.texts:
        t.set_fontsize(7)
    b.on_clicked(cb)
    _widgets.append(b)
    return b

def _tbox(rect, initial, on_submit, lo=0.1, hi=60.0):
    tx = fig.add_axes(rect)
    tx.set_facecolor("#2a2a4e")
    tb = TextBox(tx, "", initial=f"{initial:.1f}",
                 color="#2a2a4e", hovercolor="#3a3a5e")
    tb.text_disp.set_color("white")
    tb.text_disp.set_fontsize(8)
    def _cb(text):
        try:
            v = float(text)
            if lo <= v <= hi:
                on_submit(v)
        except ValueError:
            pass
    tb.on_submit(_cb)
    _widgets.append(tb)
    return tb

def _label(rect, text, color="gray", fontsize=7):
    lx = fig.add_axes(rect)
    lx.set_facecolor(BG)
    lx.axis("off")
    lx.text(0.5, 0.5, text, color=color, fontsize=fontsize,
            ha="center", va="center", transform=lx.transAxes)
    return lx


# Column layout: x=0.76..0.95, stacked from top
RX = 0.76;  RW = 0.09;  TW = 0.06
_row_y = 0.90   # current row (decrements)
_ROW_H = 0.035
_GAP   = 0.005

def _next_y():
    global _row_y
    y = _row_y
    _row_y -= (_ROW_H + _GAP)
    return y

# ── Toggles ────────────────────────────────────────────────────────
_label([RX, _next_y(), 0.18, 0.02], "─── COMPUTE ───", "#FFD700", 7)

def _mk_toggle(label, on_state, color):
    y = _next_y()
    def _cb(_ev):
        on_state[0] = not on_state[0]
        mk = "●" if on_state[0] else "○"
        b.label.set_text(f"{mk} {label}")
        b.color = color if on_state[0] else "#444"
    b = _btn([RX, y, RW, _ROW_H], f"● {label}", color, _cb)
    return b

_mk_toggle("EMO",  _emo_on,  "#FFD700")
_mk_toggle("PROS", _pros_on, "cyan")

# ── Window sizes ───────────────────────────────────────────────────
_label([RX, _next_y(), 0.18, 0.02], "─── WINDOWS ───", "gray", 7)

y = _next_y()
_label([RX, y, 0.06, _ROW_H], "emo win", "#FFD700", 6)
_tbox([RX + 0.06, y, TW, _ROW_H], EMO_WIN_SEC, lambda v: _emo_win.__setitem__(0, v))

y = _next_y()
_label([RX, y, 0.06, _ROW_H], "pros win", "cyan", 6)
_tbox([RX + 0.06, y, TW, _ROW_H], PROS_WIN_SEC, lambda v: _pros_win.__setitem__(0, v))

y = _next_y()
_label([RX, y, 0.06, _ROW_H], "scan", "gray", 6)
_tbox([RX + 0.06, y, TW, _ROW_H], SCAN_SEC, lambda v: _scan_sec.__setitem__(0, v))

y = _next_y()
_label([RX, y, 0.06, _ROW_H], "samp Hz", "gray", 6)
_sample_hz = [SAMPLE_HZ]
def _set_hz(v):
    _sample_hz[0] = max(1, int(v))
    _reset_scan()     # recompute n_samples
_tbox([RX + 0.06, y, TW, _ROW_H], float(SAMPLE_HZ), _set_hz, lo=1, hi=100)

# ── Output ─────────────────────────────────────────────────────────
_label([RX, _next_y(), 0.18, 0.02], "─── OUTPUT ─────", "#FF8800", 7)

def _toggle_log(_ev):
    if _log_on[0]:
        log_stop()
        _log_btn.label.set_text("○ LOG")
        _log_btn.color = "#444"
    else:
        log_start()
        _log_btn.label.set_text("● LOG")
        _log_btn.color = "#FF8800"

_log_btn = _btn([RX, _next_y(), RW, _ROW_H], "○ LOG", "#444", _toggle_log)

def _toggle_osc(_ev):
    if _osc_on[0]:
        osc_stop()
        _osc_btn.label.set_text("○ OSC")
        _osc_btn.color = "#444"
    else:
        osc_start()
        _osc_btn.label.set_text("● OSC")
        _osc_btn.color = "#FF8800"

_osc_btn = _btn([RX, _next_y(), RW, _ROW_H], "○ OSC", "#444", _toggle_osc)

_btn([RX, _next_y(), RW, _ROW_H], "SAVE AS", "#888", lambda _: save_snapshot())


# ════════════════════════════════════════════════════════════════════
# 6. ANIMATION UPDATE — sample → output → draw
# ════════════════════════════════════════════════════════════════════
_last_scan = [SCAN_SEC]
_last_hz   = [SAMPLE_HZ]


def _update(_frame):
    # ── Handle param changes ──
    if _scan_sec[0] != _last_scan[0] or _sample_hz[0] != _last_hz[0]:
        _reset_scan()
        ax.set_xlim(0, _scan_sec[0])
        _last_scan[0] = _scan_sec[0]
        _last_hz[0]   = _sample_hz[0]

    _trim_audio()

    # ── Sample ──
    vec = _sample_vector()
    idx = _cursor[0]
    n   = _n_samples[0]

    _buf[0][idx] = vec
    _cursor[0] = idx + 1

    # ── Output sinks ──
    log_vector(vec)
    osc_send(vec)

    # ── Wrap ──
    if _cursor[0] >= n:
        _buf[0][:] = np.nan
        _cursor[0] = 0

    # ── Cursor ──
    cx = _cursor[0] / max(_sample_hz[0], 1)
    _cursor_line.set_xdata([cx, cx])

    # ── Update lines ──
    x = _x_fixed[0]
    data = _buf[0]
    for j in range(N_CH):
        _, _, _, _, lo, hi = CHANNELS[j]
        raw = data[:, j]
        norm = np.clip((raw - lo) / (hi - lo + 1e-9), 0, 1)
        y = (N_CH - 1 - j) + PAD + BH * norm
        _lines[j].set_data(x, y)

    # ── Title ──
    lbl = _latest_label[0] or "—"
    conf = _latest_conf[0]
    vad = "speech" if _latest_vad[0] > 0.5 else "silence"
    log_tag = "LOG" if _log_on[0] else ""
    osc_tag = "OSC" if _osc_on[0] else ""
    tags = " ".join(t for t in [log_tag, osc_tag] if t)
    fig.suptitle(
        f"{lbl.upper()} {conf:.0%}  |  {vad}  |  "
        f"{_sample_hz[0]}Hz  scan={_scan_sec[0]:.0f}s  "
        f"{tags}",
        fontsize=9, color="white",
    )


# ════════════════════════════════════════════════════════════════════
# 7. CLEANUP
# ════════════════════════════════════════════════════════════════════
_stream: sd.InputStream | None = None


def _cleanup():
    global _stream
    _stop.set()
    log_stop()
    osc_stop()
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
# 8. GO
# ════════════════════════════════════════════════════════════════════
print("Starting … speak into the mic!")
print(f"  Sample rate: {SAMPLE_HZ} Hz | Scan: {SCAN_SEC}s")
print(f"  Emo win: {EMO_WIN_SEC}s | Pros win: {PROS_WIN_SEC}s")
print(f"  OSC target: {OSC_IP}:{OSC_PORT}")
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

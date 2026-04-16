#!/usr/bin/env python3
"""
Real-time Speech Analysis — Emotion + Prosody + VAD  (v2)

Simple architecture:
  - 3 compute threads update shared "latest" dicts (hold last value)
  - Main loop ticks at SAMPLE_HZ:
      1. Read latest values → one 15-d vector
      2. If VAD gate ON and VAD=0 → NaN all non-VAD channels
      3. Paint one image column (colored rectangles)
      4. If LOG on → write one CSV row
      5. If OSC on → send one OSC bundle

Display = RGBA bitmap, one column per tick, staircase by construction.
CSV row = exactly the same vector as the image column.

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
from matplotlib.colors import to_rgb
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button, TextBox
import threading, time, signal, sys, os, csv
from datetime import datetime


# ════════════════════════════════════════════════════════════════════
# 0. CONFIG
# ════════════════════════════════════════════════════════════════════
SR            = 16000
MAX_AUDIO_SEC = 15.0
VAD_THRESHOLD = 0.3

SAMPLE_HZ     = 10        # main loop rate (Hz) — display + log + OSC
SCAN_SEC      = 10.0      # display window length (s)
EMO_WIN_SEC   = 0.5       # emotion2vec analysis window (s)
PROS_WIN_SEC  = 0.5       # openSMILE window (s)
VAD_WIN_SEC   = 0.5       # Silero VAD window (s)

OUTPUT_DIR    = "output"
OSC_IP, OSC_PORT, OSC_PFX = "127.0.0.1", 9000, "/speech"


# ════════════════════════════════════════════════════════════════════
# 1. CHANNEL MAP  (name, color, source, key, lo, hi, unit)
# ════════════════════════════════════════════════════════════════════
CHANNELS = [
    ("VAD",            "#88FF88", "vad",  "vad",                         0, 1,    ""),
    ("Angry",          "#FF4444", "emo",  "angry",                       0, 1,    ""),
    ("Disgusted",      "#88AA00", "emo",  "disgusted",                   0, 1,    ""),
    ("Fearful",        "#AA44FF", "emo",  "fearful",                     0, 1,    ""),
    ("Happy",          "#FFD700", "emo",  "happy",                       0, 1,    ""),
    ("Neutral",        "#4488FF", "emo",  "neutral",                     0, 1,    ""),
    ("Other",          "#888888", "emo",  "other",                       0, 1,    ""),
    ("Sad",            "#5566CC", "emo",  "sad",                         0, 1,    ""),
    ("Surprised",      "#FF8800", "emo",  "surprised",                   0, 1,    ""),
    ("Unknown",        "#AAAAAA", "emo",  "unknown",                     0, 1,    ""),
    ("F0",             "cyan",    "pros", "F0semitoneFrom27.5Hz_sma3nz", 0, 50,   "st"),
    ("Loudness",       "green",   "pros", "Loudness_sma3",               0, 2.5,  ""),
    ("Jitter",         "pink",    "pros", "jitterLocal_sma3nz",          0, 0.35, ""),
    ("Shimmer",        "orange",  "pros", "shimmerLocaldB_sma3nz",       0, 30,   "dB"),
    ("HNR",            "violet",  "pros", "HNRdBACF_sma3nz",            0, 15,   "dB"),
]
N_CH      = len(CHANNELS)
CH_NAMES  = [c[0] for c in CHANNELS]
_CH_RGB   = [tuple(int(c * 255) for c in to_rgb(ch[1])) for ch in CHANNELS]
_NZ_KEYS  = {"F0semitoneFrom27.5Hz_sma3nz", "jitterLocal_sma3nz",
             "shimmerLocaldB_sma3nz", "HNRdBACF_sma3nz"}
F0_IDX    = 10   # index of F0 in CHANNELS

BAND_H    = 16   # pixels per channel band in image
ERASE_W   = 3    # columns cleared ahead of cursor


# ════════════════════════════════════════════════════════════════════
# 2. AUDIO ACCUMULATOR
# ════════════════════════════════════════════════════════════════════
_lock       = threading.Lock()
_chunks: list[np.ndarray] = []
_torch_lock = threading.Lock()        # Apple Silicon ARM safety


def _audio_cb(indata, frames, ti, status):
    with _lock:
        _chunks.append(indata[:, 0].copy())


def _get_audio(sec: float) -> np.ndarray | None:
    with _lock:
        if not _chunks:
            return None
        buf = np.concatenate(list(_chunks))
    if len(buf) < int(0.3 * SR):
        return None
    need = int(sec * SR)
    return buf[-need:].astype(np.float32) if len(buf) >= need else buf.astype(np.float32)


def _trim_audio():
    with _lock:
        if not _chunks:
            return
        buf = np.concatenate(_chunks)
        mx = int(MAX_AUDIO_SEC * SR)
        if len(buf) > mx:
            _chunks.clear()
            _chunks.append(buf[-mx:])


# ════════════════════════════════════════════════════════════════════
# 3. MODEL LOADING
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
# 4. SHARED STATE  (compute → sampler read)
# ════════════════════════════════════════════════════════════════════
_latest_emo:  dict[str, float] = {}
_latest_pros: dict[str, float] = {}
_latest_vad   = [0.0]
_latest_label = [""]
_latest_conf  = [0.0]

# Runtime controls
_emo_on    = [True]
_pros_on   = [True]
_vad_gate  = [True]         # gate all channels by VAD
_emo_win   = [EMO_WIN_SEC]
_pros_win  = [PROS_WIN_SEC]
_vad_win   = [VAD_WIN_SEC]
_scan_sec  = [SCAN_SEC]
_sample_hz = [SAMPLE_HZ]
_stop      = threading.Event()


# ════════════════════════════════════════════════════════════════════
# 5. COMPUTE THREADS  (each holds its last value until overwritten)
# ════════════════════════════════════════════════════════════════════

def _vad_loop():
    """Silero VAD — own thread, own window."""
    while not _stop.is_set():
        time.sleep(0.05)
        audio = _get_audio(_vad_win[0])
        if audio is None:
            continue
        try:
            with _torch_lock:
                segs = _get_speech_ts(
                    torch.from_numpy(audio).float(), _vad_model,
                    threshold=VAD_THRESHOLD, sampling_rate=SR,
                    min_speech_duration_ms=250,
                )
                _vad_model.reset_states()
            _latest_vad[0] = 1.0 if len(segs) > 0 else 0.0
        except Exception as exc:
            print(f"[VAD] {exc}", file=sys.stderr)


def _emotion_loop():
    last_t = 0.0
    while not _stop.is_set():
        time.sleep(0.05)
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
            print(f"[EMO] {exc}", file=sys.stderr)
        last_t = now


def _prosody_loop():
    smile = opensmile.Smile(
        feature_set=opensmile.FeatureSet.eGeMAPSv02,
        feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
    )
    while not _stop.is_set():
        time.sleep(0.05)
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
        has_speech = _latest_vad[0] > 0.5
        n_tail = min(5, len(df))
        tail = df.iloc[-n_tail:]
        for _, _, src, key, _, _, _ in CHANNELS:
            if src != "pros":
                continue
            vals = tail[key].values
            if key in _NZ_KEYS:
                if has_speech:
                    voiced = vals[vals > 0]
                    _latest_pros[key] = float(np.mean(voiced)) if len(voiced) else np.nan
                else:
                    _latest_pros[key] = np.nan
            else:
                _latest_pros[key] = float(np.mean(vals))


# ════════════════════════════════════════════════════════════════════
# 6. SAMPLER  — one vector per tick, same for display + CSV + OSC
# ════════════════════════════════════════════════════════════════════
_t0 = [None]   # first-sample wall clock


def _elapsed_ms() -> int:
    now = time.time()
    if _t0[0] is None:
        _t0[0] = now
    return int((now - _t0[0]) * 1000)


def _sample_vector() -> np.ndarray:
    """Read latest values → 15-d.  Apply VAD gate if enabled."""
    vec = np.full(N_CH, np.nan)
    vad_val = _latest_vad[0]
    gated = _vad_gate[0] and vad_val < 0.5

    for j, (_, _, src, key, _, _, _) in enumerate(CHANNELS):
        if src == "vad":
            vec[j] = vad_val
        elif gated:
            vec[j] = np.nan          # gate closed → NaN
        elif src == "emo":
            vec[j] = _latest_emo.get(key, np.nan) if _emo_on[0] else np.nan
        elif src == "pros":
            vec[j] = _latest_pros.get(key, np.nan) if _pros_on[0] else np.nan
    return vec


# ════════════════════════════════════════════════════════════════════
# 7. IMAGE BUFFER — the bitmap that IS the display
# ════════════════════════════════════════════════════════════════════
_BG     = (10, 10, 18, 255)
_GREY   = (40, 40, 50, 255)   # gated column colour
_SEP    = (70, 70, 80, 255)   # band separator colour
_GSEP   = (110, 110, 120, 255)  # group separator colour


def _make_image(n_cols: int) -> np.ndarray:
    img = np.zeros((N_CH * BAND_H, n_cols, 4), dtype=np.uint8)
    img[:, :] = _BG
    return img


def _n_cols() -> int:
    return max(10, int(_scan_sec[0] * _sample_hz[0]))


_ncols  = [_n_cols()]
_img    = [_make_image(_ncols[0])]
_cursor = [0]


def _paint_col(img: np.ndarray, c: int, vec: np.ndarray):
    """Paint column c of the RGBA image from the sample vector."""
    vad_val = vec[0]
    gated   = _vad_gate[0] and (np.isnan(vad_val) or vad_val < 0.5)

    for j in range(N_CH):
        name, _, src, key, lo, hi, _ = CHANNELS[j]
        r, g, b = _CH_RGB[j]
        y0 = j * BAND_H
        y1 = y0 + BAND_H - 1        # last row = separator

        val = vec[j]

        if gated:
            # Gate closed: ALL channels grey (including VAD)
            img[y0:y1, c] = _GREY
        elif np.isnan(val):
            # No data
            img[y0:y1, c] = _BG
        elif j == F0_IDX:
            # F0 — MIDI-note style: dark bg + bright bar at pitch position
            img[y0:y1, c] = (15, 15, 25, 255)
            norm = np.clip((val - lo) / (hi - lo + 1e-9), 0, 1)
            py = y0 + int((1.0 - norm) * (BAND_H - 3))
            py = max(y0, min(py, y1 - 2))
            img[py:py + 2, c] = (r, g, b, 255)
        else:
            # Filled rectangle: colour intensity = normalised value
            norm = np.clip((val - lo) / (hi - lo + 1e-9), 0, 1)
            # Minimum brightness so non-zero values are visible
            bright = 0.15 + 0.85 * norm
            img[y0:y1, c] = (int(r * bright), int(g * bright),
                             int(b * bright), 255)

        # Separator pixel
        sep = _GSEP if j in (0, 9) else _SEP   # thicker after VAD & last emo
        img[y1, c] = sep

    # Erase ahead (EKG sweep gap)
    nc = img.shape[1]
    for dc in range(1, ERASE_W + 1):
        ec = (c + dc) % nc
        img[:, ec] = _BG


# ════════════════════════════════════════════════════════════════════
# 8. OUTPUT SINKS  — CSV + OSC
# ════════════════════════════════════════════════════════════════════

# ── CSV Logger ─────────────────────────────────────────────────────
_log_on     = [False]
_log_file   = [None]
_log_writer = [None]
_log_t0     = [None]     # wall-clock of first logged sample


def log_start():
    if _log_file[0] is not None:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"track_{ts}.csv")
    f = open(path, "w", newline="")
    w = csv.writer(f)
    w.writerow(["time_ms", "dominant_emo", "confidence"] + CH_NAMES)
    _log_file[0]   = f
    _log_writer[0] = w
    _log_t0[0]     = time.time()
    _log_on[0]     = True
    print(f"[LOG] → {path}")


def log_stop():
    _log_on[0] = False
    if _log_file[0]:
        _log_file[0].close()
        print("[LOG] stopped")
    _log_file[0] = _log_writer[0] = _log_t0[0] = None


def log_row(vec: np.ndarray):
    if not _log_on[0] or _log_writer[0] is None:
        return
    ms = int((time.time() - _log_t0[0]) * 1000)
    row = [str(ms), _latest_label[0], f"{_latest_conf[0]:.3f}"]
    for v in vec:
        row.append(f"{v:.4f}" if not np.isnan(v) else "")
    _log_writer[0].writerow(row)


# ── OSC Streamer ───────────────────────────────────────────────────
_osc_on     = [False]
_osc_client = [None]


def osc_start():
    try:
        from pythonosc.udp_client import SimpleUDPClient
        _osc_client[0] = SimpleUDPClient(OSC_IP, OSC_PORT)
        _osc_on[0] = True
        print(f"[OSC] → {OSC_IP}:{OSC_PORT}")
    except ImportError:
        print("[OSC] python-osc not installed.  pip install python-osc")
        _osc_on[0] = False


def osc_stop():
    _osc_on[0] = False
    _osc_client[0] = None
    print("[OSC] stopped")


def osc_send(vec: np.ndarray):
    if not _osc_on[0] or _osc_client[0] is None:
        return
    c = _osc_client[0]
    try:
        c.send_message(f"{OSC_PFX}/vad",   [float(vec[0]) if not np.isnan(vec[0]) else 0.0])
        c.send_message(f"{OSC_PFX}/emo",    [float(v) if not np.isnan(v) else 0.0 for v in vec[1:10]])
        c.send_message(f"{OSC_PFX}/label",  [_latest_label[0], float(_latest_conf[0])])
        c.send_message(f"{OSC_PFX}/pros",   [float(v) if not np.isnan(v) else 0.0 for v in vec[10:]])
    except Exception as exc:
        print(f"[OSC] {exc}", file=sys.stderr)


# ── Snapshot ───────────────────────────────────────────────────────
_snap_buf: list[list] = []   # rows collected while running


def save_snapshot():
    if not _snap_buf:
        print("[SAVE] nothing to save"); return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"snapshot_{ts}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_ms", "dominant_emo", "confidence"] + CH_NAMES)
        for row in _snap_buf:
            w.writerow(row)
    print(f"[SAVE] → {path}")


# ════════════════════════════════════════════════════════════════════
# 9. DISPLAY SETUP
# ════════════════════════════════════════════════════════════════════
BG = "#0a0a12"

fig = plt.figure(figsize=(14, 8))
fig.patch.set_facecolor(BG)

ax = fig.add_axes([0.08, 0.06, 0.62, 0.88])
ax.set_facecolor(BG)
ax.set_xlim(0, SCAN_SEC)
ax.set_ylim(0, N_CH)
ax.set_xlabel("time (s)", color="gray", fontsize=8)
ax.tick_params(axis="x", colors="gray", labelsize=7)
ax.invert_yaxis()

# imshow — the bitmap
_im = ax.imshow(
    _img[0], aspect="auto",
    extent=[0, SCAN_SEC, N_CH, 0],
    interpolation="nearest", origin="upper",
)

# Band labels (with units where applicable)
yticks = [j + 0.5 for j in range(N_CH)]
ylabels = []
for name, _, _, _, _, _, unit in CHANNELS:
    ylabels.append(f"{name} ({unit})" if unit else name)
ax.set_yticks(yticks)
ax.set_yticklabels(ylabels)
for j, lbl in enumerate(ax.get_yticklabels()):
    lbl.set_color(CHANNELS[j][1])
    lbl.set_fontsize(7)
ax.tick_params(axis="y", length=0)

# Group separators (heavier lines)
for y in [1, 10]:          # after VAD, after last emotion
    ax.axhline(y, color="#777", lw=1.5)
for y in range(N_CH + 1):  # faint for all others
    if y not in (1, 10):
        ax.axhline(y, color="#333", lw=0.3)

# Group labels on left margin
ax.text(-0.09, 0.5 / N_CH, "VAD", transform=ax.transAxes,
        rotation=90, color="#88FF88", fontsize=7, ha="center", va="center",
        fontweight="bold")
ax.text(-0.09, (1 + 10) / 2 / N_CH, "EMOTIONS", transform=ax.transAxes,
        rotation=90, color="#FFD700", fontsize=8, ha="center", va="center",
        fontweight="bold")
ax.text(-0.09, (10 + 15) / 2 / N_CH, "PROSODY", transform=ax.transAxes,
        rotation=90, color="cyan", fontsize=8, ha="center", va="center",
        fontweight="bold")

for sp in ax.spines.values():
    sp.set_color("#333")

# Cursor line
_cursor_line = ax.axvline(0, color="#ffffff", lw=0.6, alpha=0.25)

# Vertical grid: one tick per sample
_sample_ticks = np.arange(0, SCAN_SEC, 1.0 / SAMPLE_HZ)
ax.set_xticks(_sample_ticks, minor=True)
ax.set_xticks(np.arange(0, SCAN_SEC + 0.01, 1.0))   # major = 1s
ax.grid(True, axis="x", which="minor", alpha=0.06, color="gray", lw=0.3)
ax.grid(True, axis="x", which="major", alpha=0.15, color="gray", lw=0.5)


# ════════════════════════════════════════════════════════════════════
# 10. CONTROLS — right panel
# ════════════════════════════════════════════════════════════════════
_widgets = []

RX    = 0.76
RW    = 0.10
TW    = 0.06
ROW_H = 0.028
GAP   = 0.004
_ry   = 0.92


def _ny():
    global _ry; y = _ry; _ry -= (ROW_H + GAP); return y


def _lbl(rect, txt, col="gray", fs=6):
    a = fig.add_axes(rect); a.set_facecolor(BG); a.axis("off")
    a.text(0.5, 0.5, txt, color=col, fontsize=fs,
           ha="center", va="center", transform=a.transAxes)


def _btn(rect, txt, col, cb):
    a = fig.add_axes(rect); a.set_facecolor(BG)
    b = Button(a, txt, color=col, hovercolor="#555")
    for t in a.texts:
        t.set_fontsize(6)
    b.on_clicked(cb)
    _widgets.append(b)
    return b


def _tbox(rect, init, cb, lo=0.1, hi=60.0):
    a = fig.add_axes(rect); a.set_facecolor("#2a2a4e")
    tb = TextBox(a, "", initial=f"{init:.1f}",
                 color="#2a2a4e", hovercolor="#3a3a5e")
    tb.text_disp.set_color("white"); tb.text_disp.set_fontsize(7)
    def _s(text):
        try:
            v = float(text)
            if lo <= v <= hi: cb(v)
        except ValueError:
            pass
    tb.on_submit(_s)
    _widgets.append(tb)
    return tb


def _toggle(label, state, color):
    y = _ny()
    def _cb(_):
        state[0] = not state[0]
        mk = "●" if state[0] else "○"
        b.label.set_text(f"{mk} {label}")
        b.color = color if state[0] else "#444"
    b = _btn([RX, y, RW, ROW_H],
             f"{'●' if state[0] else '○'} {label}",
             color if state[0] else "#444", _cb)
    return b


def _win_row(label, col, init, setter):
    y = _ny()
    _lbl([RX, y, 0.05, ROW_H], label, col, 6)
    _tbox([RX + 0.05, y, TW, ROW_H], init, setter)


# ─── EMOTIONS ──────────────────────────────────────────────────────
_lbl([RX, _ny(), 0.18, 0.015], "── EMOTIONS ──", "#FFD700", 6)
_toggle("EMOTIONS", _emo_on, "#FFD700")
_win_row("win (s)", "#FFD700", EMO_WIN_SEC, lambda v: _emo_win.__setitem__(0, v))

# ─── PROSODY ───────────────────────────────────────────────────────
_lbl([RX, _ny(), 0.18, 0.015], "── PROSODY ───", "cyan", 6)
_toggle("PROSODY", _pros_on, "cyan")
_win_row("win (s)", "cyan", PROS_WIN_SEC, lambda v: _pros_win.__setitem__(0, v))

# ─── VAD ───────────────────────────────────────────────────────────
_lbl([RX, _ny(), 0.18, 0.015], "──── VAD ─────", "#88FF88", 6)
_toggle("VAD GATE", _vad_gate, "#88FF88")
_win_row("win (s)", "#88FF88", VAD_WIN_SEC, lambda v: _vad_win.__setitem__(0, v))

# ─── DISPLAY ──────────────────────────────────────────────────────
_lbl([RX, _ny(), 0.18, 0.015], "── SAMPLING ──", "gray", 6)

def _set_scan(v):
    _scan_sec[0] = v
def _set_period(v):
    _sample_hz[0] = max(1, int(round(1.0 / v)))

_win_row("window (s)", "gray", SCAN_SEC, _set_scan)
_win_row("period (s)", "gray", 1.0 / SAMPLE_HZ, _set_period)

# ─── OUTPUT ───────────────────────────────────────────────────────
_lbl([RX, _ny(), 0.18, 0.015], "── OUTPUT ────", "#FF8800", 6)


def _tog_log(_):
    if _log_on[0]:
        log_stop(); _lb.label.set_text("○ LOG"); _lb.color = "#444"
    else:
        log_start(); _lb.label.set_text("● LOG"); _lb.color = "#FF8800"

_lb = _btn([RX, _ny(), RW, ROW_H], "○ LOG", "#444", _tog_log)


def _tog_osc(_):
    if _osc_on[0]:
        osc_stop(); _ob.label.set_text("○ OSC"); _ob.color = "#444"
    else:
        osc_start(); _ob.label.set_text("● OSC"); _ob.color = "#FF8800"

_ob = _btn([RX, _ny(), RW, ROW_H], "○ OSC", "#444", _tog_osc)
_btn([RX, _ny(), RW, ROW_H], "SAVE AS", "#666", lambda _: save_snapshot())


# ════════════════════════════════════════════════════════════════════
# 11. ANIMATION UPDATE  — the main loop
# ════════════════════════════════════════════════════════════════════
_prev_scan = [SCAN_SEC]
_prev_hz   = [SAMPLE_HZ]


def _update(_frame):
    # Detect parameter changes → rebuild image buffer
    cur_scan = _scan_sec[0]
    cur_hz   = _sample_hz[0]
    if cur_scan != _prev_scan[0] or cur_hz != _prev_hz[0]:
        nc = max(10, int(cur_scan * cur_hz))
        _ncols[0]  = nc
        _img[0]    = _make_image(nc)
        _cursor[0] = 0
        _prev_scan[0] = cur_scan
        _prev_hz[0]   = cur_hz
        ax.set_xlim(0, cur_scan)
        _im.set_extent([0, cur_scan, N_CH, 0])
        ax.set_xticks(np.arange(0, cur_scan + 0.01, 1.0))
        ax.set_xticks(np.arange(0, cur_scan, 1.0 / cur_hz), minor=True)
        # Also update FuncAnimation interval
        _anim.event_source.interval = int(1000 / cur_hz)

    _trim_audio()

    # ── 1. Sample ──
    vec = _sample_vector()

    # ── 2. Paint column ──
    c   = _cursor[0]
    nc  = _ncols[0]
    _paint_col(_img[0], c, vec)
    _im.set_data(_img[0])

    # ── 3. Snapshot buffer (ring) ──
    ms = _elapsed_ms()
    row = [str(ms), _latest_label[0], f"{_latest_conf[0]:.3f}"]
    row += [f"{v:.4f}" if not np.isnan(v) else "" for v in vec]
    if len(_snap_buf) >= nc:
        _snap_buf.clear()
    _snap_buf.append(row)

    # ── 4. Log ──
    log_row(vec)

    # ── 5. OSC ──
    osc_send(vec)

    # ── 6. Advance cursor (clear image on wrap) ──
    nxt = (c + 1) % nc
    if nxt < c:  # wrapped
        _img[0][:] = _BG
    _cursor[0] = nxt
    cx = (nxt) / max(cur_hz, 1)
    _cursor_line.set_xdata([cx, cx])

    # ── Title ──
    lbl  = _latest_label[0] or "—"
    conf = _latest_conf[0]
    vad  = "SPEECH" if _latest_vad[0] > 0.5 else "silence"
    tags = " ".join(t for t in
                    ["LOG" if _log_on[0] else "",
                     "OSC" if _osc_on[0] else ""] if t)
    gate = "gate ON" if _vad_gate[0] else "gate OFF"
    fig.suptitle(
        f"{lbl.upper()} {conf:.0%}  │  {vad}  │  {gate}  │  "
        f"T={1.0/cur_hz:.2f}s   win={cur_scan:.0f}s   {tags}",
        fontsize=9, color="white",
    )


# ════════════════════════════════════════════════════════════════════
# 12. CLEANUP + GO
# ════════════════════════════════════════════════════════════════════
_stream: sd.InputStream | None = None


def _cleanup():
    global _stream
    _stop.set()
    log_stop(); osc_stop()
    if _stream:
        try: _stream.abort(); _stream.close()
        except Exception: pass
        _stream = None
    print("\nClean exit.")


signal.signal(signal.SIGINT,  lambda *_: (_cleanup(), sys.exit(0)))
signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(0)))

print(f"Starting … SAMPLE_HZ={SAMPLE_HZ}  SCAN={SCAN_SEC}s")
print(f"  emo_win={EMO_WIN_SEC}s  pros_win={PROS_WIN_SEC}s  vad_win={VAD_WIN_SEC}s")
print("Close window or Ctrl-C to stop.\n")

try:
    _stream = sd.InputStream(
        samplerate=SR, channels=1, dtype="float32",
        blocksize=int(SR * 0.05), callback=_audio_cb,
    )
    _stream.start()

    threading.Thread(target=_vad_loop,     daemon=True).start()
    threading.Thread(target=_emotion_loop, daemon=True).start()
    threading.Thread(target=_prosody_loop, daemon=True).start()

    _anim = FuncAnimation(
        fig, _update,
        interval=int(1000 / SAMPLE_HZ),
        blit=False, cache_frame_data=False,
    )
    plt.show()
finally:
    _cleanup()

print("Done.")

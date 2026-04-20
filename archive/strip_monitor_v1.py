#!/usr/bin/env python3
"""
Real-time Speech Analysis — Prosody LLD + VAD + Emotion

Architecture (from the proven test_lld_vad_realtime.py):
  - sounddevice callback → audio accumulator (list of chunks)
  - Full-buffer openSMILE LLD extraction (no chunking — solves F0 fragmentation)
  - Silero VAD gating on nz features
  - Optional emotion2vec (base or seed model, selectable)
  - Optional matplotlib display (toggle off for headless / RPi)
  - CSV logging with start/stop
  - OSC streaming with configurable IP/port

Usage:
    conda activate ML311
    python strip_monitor.py                     # with display
    python strip_monitor.py --no-display        # headless (log/OSC only)
    python strip_monitor.py --emotion-model seed  # smaller model for RPi
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import signal
import threading
import time
from datetime import datetime

import numpy as np
import sounddevice as sd
import opensmile
import torch


# ═══════════════════════════════════════════════════════════════════
# 0. ARGUMENT PARSING
# ═══════════════════════════════════════════════════════════════════
def parse_args():
    p = argparse.ArgumentParser(description="Real-time speech analysis")
    p.add_argument("--no-display", action="store_true",
                   help="Run headless (no matplotlib window)")
    p.add_argument("--no-emotion", action="store_true",
                   help="Disable emotion2vec (prosody + VAD only)")
    p.add_argument("--emotion-model", choices=["base", "seed"], default="base",
                   help="Emotion model size: base (~90M) or seed (~20M)")
    p.add_argument("--osc-ip", default="127.0.0.1")
    p.add_argument("--osc-port", type=int, default=9000)
    p.add_argument("--osc-prefix", default="/speech")
    p.add_argument("--osc-autostart", action="store_true",
                   help="Start OSC streaming automatically on launch")
    p.add_argument("--ctrl-port", type=int, default=9001,
                   help="Port to listen for remote control OSC commands")
    return p.parse_args()

ARGS = parse_args()

# Matplotlib — only import if display enabled
if not ARGS.no_display:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Button, TextBox


# ═══════════════════════════════════════════════════════════════════
# 1. CONFIG
# ═══════════════════════════════════════════════════════════════════
SR             = 16000
DISPLAY_SEC    = 10.0
MAX_AUDIO_SEC  = 15.0
UPDATE_MS      = 250       # display + extraction tick
VAD_THRESHOLD  = 0.3
EMO_INTERVAL_S = 2.0       # emotion inference every N seconds
OUTPUT_DIR     = "output"

# Mutable window-size refs (settable from UI)
_emo_window = [EMO_INTERVAL_S]   # seconds: how much audio for emotion
_display_window = [DISPLAY_SEC]  # seconds: visible time span on strips

# Smoothed emotion values (shared between display and logger)
_EMO_DECAY  = 0.5    # per-frame retention (0 = instant, 1 = frozen)
_emo_smooth = None   # initialised after EMOTION_DIMS is known

# LLD features to extract and stream
FEATURES = [
    ("F0semitoneFrom27.5Hz_sma3nz", "F0 (st)",     "cyan",   True,  (0, 50)),
    ("Loudness_sma3",               "Loudness",     "green",  False, (0, 2.5)),
    ("jitterLocal_sma3nz",          "Jitter",       "pink",   True,  (0, 0.35)),
    ("shimmerLocaldB_sma3nz",       "Shimmer (dB)", "orange", True,  (0, 30)),
    ("HNRdBACF_sma3nz",            "HNR (dB)",     "violet", True,  (0, 15)),
]

EMOTION_DIMS = [
    "angry", "disgusted", "fearful", "happy", "neutral",
    "other", "sad", "surprised", "unknown",
]

_emo_smooth = np.zeros(len(EMOTION_DIMS), dtype=np.float64)


# ═══════════════════════════════════════════════════════════════════
# 2. AUDIO ACCUMULATOR
# ═══════════════════════════════════════════════════════════════════
_audio_lock = threading.Lock()
_audio_chunks: list[np.ndarray] = []


def _audio_callback(indata, frames, time_info, status):
    with _audio_lock:
        _audio_chunks.append(indata[:, 0].copy())


def _get_full_audio() -> np.ndarray | None:
    """Return full accumulated buffer (trimmed to MAX_AUDIO_SEC)."""
    with _audio_lock:
        if not _audio_chunks:
            return None
        audio = np.concatenate(_audio_chunks)
        max_samples = int(MAX_AUDIO_SEC * SR)
        if len(audio) > max_samples:
            audio = audio[-max_samples:]
            _audio_chunks.clear()
            _audio_chunks.append(audio.copy())
    return audio.astype(np.float32)


def _get_recent_audio(sec: float) -> np.ndarray | None:
    """Return last N seconds of audio (for emotion inference)."""
    with _audio_lock:
        if not _audio_chunks:
            return None
        buf = np.concatenate(_audio_chunks)
    need = int(sec * SR)
    if len(buf) < int(0.3 * SR):
        return None
    chunk = buf[-need:] if len(buf) >= need else buf
    return chunk.astype(np.float32)


# ═══════════════════════════════════════════════════════════════════
# 3. OPENSMILE
# ═══════════════════════════════════════════════════════════════════
smile = opensmile.Smile(
    feature_set=opensmile.FeatureSet.eGeMAPSv02,
    feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
)


# ═══════════════════════════════════════════════════════════════════
# 4. SILERO VAD
# ═══════════════════════════════════════════════════════════════════
print("Loading Silero VAD …")
vad_model, vad_utils = torch.hub.load(
    "snakers4/silero-vad", "silero_vad",
    trust_repo=True, force_reload=False,
)
get_speech_timestamps = vad_utils[0]
print("VAD ready.")


def _compute_vad_mask(audio: np.ndarray, lld_times: np.ndarray) -> np.ndarray:
    tensor = torch.from_numpy(audio).float()
    timestamps = get_speech_timestamps(
        tensor, vad_model,
        threshold=VAD_THRESHOLD, sampling_rate=SR,
        min_speech_duration_ms=250,
    )
    vad_model.reset_states()
    mask = np.zeros(len(lld_times), dtype=bool)
    for seg in timestamps:
        t_start = seg["start"] / SR
        t_end = seg["end"] / SR
        mask |= (lld_times >= t_start) & (lld_times <= t_end)
    return mask


def _compute_vad_contour(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    window = 512
    n_windows = len(audio) // window
    if n_windows == 0:
        return np.array([]), np.array([])
    probs = np.zeros(n_windows, dtype=np.float32)
    times = np.zeros(n_windows, dtype=np.float32)
    for i in range(n_windows):
        chunk = audio[i * window : (i + 1) * window]
        tensor = torch.from_numpy(chunk).float()
        probs[i] = vad_model(tensor, SR).item()
        times[i] = (i * window + window / 2) / SR
    vad_model.reset_states()
    return times, probs


# ═══════════════════════════════════════════════════════════════════
# 5. EMOTION MODEL (optional)
# ═══════════════════════════════════════════════════════════════════
_emo_model = None
_emo_lock = threading.Lock()
_latest_emo: dict = {"label": "", "confidence": 0.0, "scores": {}}

if not ARGS.no_emotion:
    from src.emotion_model import Emotion2VecModel
    model_map = {
        "base": "iic/emotion2vec_plus_base",
        "seed": "iic/emotion2vec_plus_seed",
    }
    model_name = model_map[ARGS.emotion_model]
    print(f"Loading emotion2vec ({ARGS.emotion_model}: {model_name}) …")
    _emo_model = Emotion2VecModel(model_name=model_name, device="cpu")
    print(f"emotion2vec ready — {len(_emo_model.dimensions)} classes")


def _emotion_thread():
    """Background thread: run emotion inference periodically."""
    while not _stop_event.is_set():
        time.sleep(_emo_window[0])
        if not _proc_emotion[0]:
            continue
        audio = _get_recent_audio(_emo_window[0])
        if audio is None:
            continue
        # Gate: skip inference when VAD says no speech (reuse main-thread result)
        if _proc_vad[0] and not _has_speech[0]:
            continue
        try:
            r = _emo_model.predict(audio, sr=SR)
            with _emo_lock:
                _latest_emo.update(r)
        except Exception as e:
            print(f"[EMO] {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════
# 6. CSV LOGGING
# ═══════════════════════════════════════════════════════════════════
_log_on = False
_log_file = None
_log_writer = None
_log_t0 = None
_log_path = None


def _csv_header():
    cols = ["time_ms", "vad"]
    cols += [f[0] for f in FEATURES]
    if not ARGS.no_emotion:
        cols += ["emo_label", "emo_confidence"] + EMOTION_DIMS
    return cols


def log_start(path: str | None = None):
    global _log_on, _log_file, _log_writer, _log_t0, _log_path
    if _log_on:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(OUTPUT_DIR, f"track_{ts}.csv")
    _log_path = path
    _log_file = open(path, "w", newline="")
    _log_writer = csv.writer(_log_file)
    _log_writer.writerow(_csv_header())
    _log_t0 = time.time()
    _log_on = True
    print(f"[LOG] recording → {path}")


def log_stop():
    global _log_on, _log_file, _log_writer, _log_t0
    if not _log_on:
        return
    _log_on = False
    if _log_file:
        _log_file.close()
        print(f"[LOG] stopped → {_log_path}")
    _log_file = _log_writer = _log_t0 = None


def _log_frame(vad_speech: bool, feature_means: dict):
    """Log one summary row per update tick — uses decayed emotion values."""
    if not _log_on or _log_writer is None:
        return
    ms = int((time.time() - _log_t0) * 1000)
    row = [ms, 1 if vad_speech else 0]
    for key, _, _, is_nz, _ in FEATURES:
        v = feature_means.get(key, float("nan"))
        row.append(f"{v:.4f}" if not np.isnan(v) else "")
    if not ARGS.no_emotion:
        # Log the decayed (smoothed) values — same as what's displayed
        top_idx = int(np.argmax(_emo_smooth))
        top_val = _emo_smooth[top_idx]
        row.append(EMOTION_DIMS[top_idx] if top_val > 0.01 else "")
        row.append(f"{top_val:.4f}")
        for j in range(len(EMOTION_DIMS)):
            row.append(f"{_emo_smooth[j]:.4f}")
    _log_writer.writerow(row)


# ═══════════════════════════════════════════════════════════════════
# 7. OSC STREAMING
# ═══════════════════════════════════════════════════════════════════
_osc_on = False
_osc_client = None


def osc_start(ip: str = None, port: int = None):
    global _osc_on, _osc_client
    ip = ip or ARGS.osc_ip
    port = port or ARGS.osc_port
    try:
        from pythonosc.udp_client import SimpleUDPClient
        _osc_client = SimpleUDPClient(ip, port)
        _osc_on = True
        print(f"[OSC] streaming → {ip}:{port}")
    except ImportError:
        print("[OSC] python-osc not installed. Run: pip install python-osc")
        _osc_on = False


def osc_stop():
    global _osc_on, _osc_client
    _osc_on = False
    _osc_client = None
    print("[OSC] stopped")


def _osc_send(vad_speech: bool, feature_means: dict):
    if not _osc_on or _osc_client is None:
        return
    pfx = ARGS.osc_prefix
    try:
        _osc_client.send_message(f"{pfx}/vad", [1.0 if vad_speech else 0.0])
        # Prosody features
        for key, label, _, _, _ in FEATURES:
            v = feature_means.get(key, 0.0)
            val = float(v) if not np.isnan(v) else 0.0
            _osc_client.send_message(f"{pfx}/{key}", [val])
        # Emotion
        if not ARGS.no_emotion:
            with _emo_lock:
                scores = _latest_emo.get("scores", {})
                label = _latest_emo.get("label", "")
                conf = _latest_emo.get("confidence", 0.0)
                _osc_client.send_message(f"{pfx}/emo/label", [label, float(conf)])
                emo_vals = [float(scores.get(d, 0.0)) for d in EMOTION_DIMS]
                _osc_client.send_message(f"{pfx}/emo/scores", emo_vals)
    except Exception as e:
        print(f"[OSC] {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════
# 7b. REMOTE CONTROL LISTENER (OSC on CTRL_PORT)
# ═══════════════════════════════════════════════════════════════════
def _start_ctrl_listener():
    """Listen for remote control commands via OSC on --ctrl-port."""
    try:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import BlockingOSCUDPServer
    except ImportError:
        print("[CTRL] python-osc not installed — remote control disabled")
        return

    disp = Dispatcher()
    disp.map("/ctrl/osc_start", lambda addr, *a: osc_start())
    disp.map("/ctrl/osc_stop", lambda addr, *a: osc_stop())
    disp.map("/ctrl/log_start", lambda addr, *a: log_start())
    disp.map("/ctrl/log_stop", lambda addr, *a: log_stop())

    server = BlockingOSCUDPServer(("0.0.0.0", ARGS.ctrl_port), disp)
    print(f"[CTRL] listening on :{ARGS.ctrl_port}")
    server.serve_forever()


# ═══════════════════════════════════════════════════════════════════
# 8. SHARED STATE
# ═══════════════════════════════════════════════════════════════════
_stop_event = threading.Event()
_t_start = time.time()
_stream: sd.InputStream | None = None
_sample_index = 0

# Processing toggles (mutable lists for closure access)
_proc_vad = [True]
_proc_emotion = [True]
_proc_prosody = [True]
_display_on = [True]
_has_speech = [False]  # set by main tick, read by emotion thread


# ═══════════════════════════════════════════════════════════════════
# 9. CORE UPDATE — runs every tick (display or headless)
# ═══════════════════════════════════════════════════════════════════
def _process_tick():
    """Extract features from full buffer, return display data.

    Returns None if no audio yet, otherwise a dict with all data
    needed for display, logging, and OSC.
    """
    audio = _get_full_audio()
    if audio is None or len(audio) < int(0.3 * SR):
        return None

    total_sec = len(audio) / SR

    df = None
    times_in_audio = None
    speech_mask = None
    feature_means = {}
    has_speech = False

    # openSMILE — full buffer (skip if prosody toggled off)
    if _proc_prosody[0]:
        try:
            df = smile.process_signal(audio, sampling_rate=SR)
        except Exception as e:
            print(f"[openSMILE] {e}")
            df = None

    if df is not None and len(df) > 0:
        starts = np.array([t.total_seconds() for t in df.index.get_level_values("start")])
        ends = np.array([t.total_seconds() for t in df.index.get_level_values("end")])
        times_in_audio = (starts + ends) / 2.0

        # VAD mask aligned to LLD frames (binary)
        if _proc_vad[0]:
            speech_mask = _compute_vad_mask(audio, times_in_audio)
        else:
            speech_mask = np.ones(len(times_in_audio), dtype=bool)

        # Summary values for logging/OSC (mean of last ~0.5s of voiced frames)
        n_tail = min(25, len(df))
        tail_mask = speech_mask[-n_tail:]
        tail_df = df.iloc[-n_tail:]
        has_speech = bool(np.any(speech_mask[-n_tail:]))
        _has_speech[0] = has_speech

        for key, _, _, is_nz, _ in FEATURES:
            vals = tail_df[key].values.astype(np.float32)
            if is_nz:
                if has_speech:
                    voiced = vals[tail_mask & (vals > 0)]
                    feature_means[key] = float(np.mean(voiced)) if len(voiced) else float("nan")
                else:
                    feature_means[key] = float("nan")
            else:
                feature_means[key] = float(np.mean(vals))

    # ── Decay-smooth emotion values (shared with display + log) ──
    if not ARGS.no_emotion:
        if _proc_emotion[0] and has_speech:
            with _emo_lock:
                scores = _latest_emo.get("scores", {})
            for j, dim in enumerate(EMOTION_DIMS):
                raw = scores.get(dim, 0.0)
                if raw >= _emo_smooth[j]:
                    _emo_smooth[j] = raw
                else:
                    _emo_smooth[j] = _EMO_DECAY * _emo_smooth[j] + (1 - _EMO_DECAY) * raw
        else:
            # no speech or emotion off → decay toward zero
            for j in range(len(EMOTION_DIMS)):
                _emo_smooth[j] *= _EMO_DECAY
        # clamp tiny values
        _emo_smooth[_emo_smooth < 0.005] = 0.0

    # Log + OSC
    _log_frame(has_speech, feature_means)
    _osc_send(has_speech, feature_means)

    global _sample_index
    _sample_index += 1
    elapsed = time.time() - _t_start

    return {
        "audio": audio,
        "total_sec": total_sec,
        "elapsed": elapsed,
        "df": df,
        "times_in_audio": times_in_audio,
        "speech_mask": speech_mask,
        "has_speech": has_speech,
        "feature_means": feature_means,
    }


# ═══════════════════════════════════════════════════════════════════
# 10. DISPLAY (optional)
# ═══════════════════════════════════════════════════════════════════
if not ARGS.no_display:
    from matplotlib.gridspec import GridSpec

    EMOTION_COLORS_MAP = {
        "angry":     "#FF4444",
        "disgusted": "#88AA00",
        "fearful":   "#AA44FF",
        "happy":     "#FFD700",
        "neutral":   "#4488FF",
        "other":     "#888888",
        "sad":       "#6688CC",
        "surprised": "#FF8800",
        "unknown":   "#666666",
    }

    n_feature_strips = len(FEATURES)
    _has_emo_display = not ARGS.no_emotion

    # ── Build layout ──
    height_ratios = []
    if _has_emo_display:
        height_ratios.append(2.5)                     # emotion bars
    height_ratios += [2, 1] + [1] * n_feature_strips  # wave + VAD + features
    n_rows = len(height_ratios)

    fig = plt.figure(figsize=(12, 1.2 * n_rows + 1.0))
    fig.patch.set_facecolor("#1a1a2e")

    gs = GridSpec(n_rows, 1, figure=fig,
                  height_ratios=height_ratios,
                  left=0.14, right=0.98, top=0.94, bottom=0.10,
                  hspace=0.35)

    row_idx = 0

    # ── Emotion bar chart (vertical) ──
    ax_emo = None
    emo_bars = None
    if _has_emo_display:
        ax_emo = fig.add_subplot(gs[row_idx]); row_idx += 1
        ax_emo.set_facecolor("#1a1a2e")
        emo_x = np.arange(len(EMOTION_DIMS))
        emo_colors = [EMOTION_COLORS_MAP.get(d, "#888") for d in EMOTION_DIMS]
        emo_bars = ax_emo.bar(emo_x, [0] * len(EMOTION_DIMS),
                              color=emo_colors, width=0.75, alpha=0.7)
        ax_emo.set_xticks(emo_x)
        ax_emo.set_xticklabels([d.capitalize() for d in EMOTION_DIMS],
                               fontsize=6, color="white", rotation=0)
        ax_emo.set_ylim(0, 1.0)
        ax_emo.tick_params(axis="y", colors="gray", labelsize=6)
        ax_emo.tick_params(axis="x", length=0)
        for spine in ax_emo.spines.values():
            spine.set_color("#333")
        ax_emo.grid(True, axis="y", alpha=0.15, color="gray")

    # ── Waveform ──
    ax_wave = fig.add_subplot(gs[row_idx]); row_idx += 1
    ax_wave.set_facecolor("#1a1a2e")
    ax_wave.set_ylabel("Wave", color="white", fontsize=8)
    ax_wave.set_ylim(-0.1, 0.1)
    ax_wave.tick_params(colors="gray", labelsize=7)
    line_wave, = ax_wave.plot([], [], color="gray", linewidth=0.3)

    # ── VAD (binary filled — reusable line, no fill_between) ──
    ax_vad = fig.add_subplot(gs[row_idx], sharex=ax_wave); row_idx += 1
    ax_vad.set_facecolor("#1a1a2e")
    ax_vad.set_ylabel("VAD", color="#88FF88", fontsize=8)
    ax_vad.set_ylim(-0.05, 1.15)
    ax_vad.set_yticks([0, 1])
    ax_vad.set_yticklabels(["sil", "spk"], fontsize=6, color="#88FF88")
    ax_vad.tick_params(colors="gray", labelsize=7)
    line_vad, = ax_vad.plot([], [], color="#88FF88", linewidth=0,
                            drawstyle="steps-mid")
    # Pre-created fill polygon (reuse via set_xy)
    from matplotlib.patches import Polygon
    _vad_poly = Polygon(np.zeros((1, 2)), closed=True,
                        facecolor="#88FF88", alpha=0.5, edgecolor="none")
    ax_vad.add_patch(_vad_poly)

    # ── Feature strips ──
    lines = []
    feature_axes = []
    for i, (key, label, color, is_nz, ylim) in enumerate(FEATURES):
        ax = fig.add_subplot(gs[row_idx], sharex=ax_wave); row_idx += 1
        ax.set_facecolor("#1a1a2e")
        ax.set_ylabel(label, color=color, fontsize=8)
        ax.set_ylim(ylim)
        ax.tick_params(colors="gray", labelsize=7)
        ln, = ax.plot([], [], color=color, linewidth=1.5)
        lines.append(ln)
        feature_axes.append(ax)

    all_ts_axes = [ax_wave, ax_vad] + feature_axes
    for ax in all_ts_axes:
        ax.set_xlim(0, DISPLAY_SEC)
        ax.grid(True, alpha=0.15, color="gray")
        for spine in ax.spines.values():
            spine.set_color("#333")
    feature_axes[-1].set_xlabel("Time (s)", color="white", fontsize=9)

    # ── Toggle buttons (left margin) ──
    _btn_widgets = []

    _toggle_specs = []
    if _has_emo_display:
        _toggle_specs.append(("EMO", _proc_emotion, "#442200", "#886600"))
    _toggle_specs += [
        ("VAD", _proc_vad,     "#1a331a", "#338833"),
        ("PRS", _proc_prosody, "#1a2233", "#336688"),
        ("DSP", _display_on,   "#1a1a33", "#334466"),
    ]

    def _make_toggle_cb(flag, btn_ref, name, off_c, on_c):
        def cb(event):
            flag[0] = not flag[0]
            b = btn_ref[0]
            if flag[0]:
                b.label.set_text(f"■ {name}")
                b.color = on_c
                b.hovercolor = on_c
            else:
                b.label.set_text(f"○ {name}")
                b.color = off_c
                b.hovercolor = off_c
        return cb

    n_tgl = len(_toggle_specs)
    _toggle_y_positions = {}  # name → y_pos
    for idx, (name, flag_ref, off_color, on_color) in enumerate(_toggle_specs):
        y_pos = 0.88 - idx * (0.68 / max(n_tgl - 1, 1))
        _toggle_y_positions[name] = y_pos
        ax_btn = fig.add_axes([0.015, y_pos, 0.06, 0.035])
        ax_btn.set_facecolor(on_color)
        btn = Button(ax_btn, f"■ {name}", color=on_color, hovercolor=on_color)
        btn.label.set_fontsize(7)
        btn.label.set_color("white")
        btn_ref = [btn]  # mutable ref for closure
        btn.on_clicked(_make_toggle_cb(flag_ref, btn_ref, name, off_color, on_color))
        _btn_widgets.append(btn)

    # ── Window-size input boxes (below EMO and PRS toggles) ──
    def _make_window_input(y_pos, initial_val, target_ref, label_text):
        """Create a small labelled input below a toggle button."""
        y_input = y_pos - 0.035
        ax_lbl = fig.add_axes([0.005, y_input, 0.025, 0.025])
        ax_lbl.set_facecolor("#1a1a2e"); ax_lbl.axis("off")
        ax_lbl.text(0.9, 0.5, label_text, color="gray", fontsize=5,
                    ha="right", va="center", transform=ax_lbl.transAxes)
        ax_inp = fig.add_axes([0.03, y_input, 0.045, 0.025])
        ax_inp.set_facecolor("#2a2a4e")
        box = TextBox(ax_inp, "", initial=f"{initial_val:.1f}",
                      color="#2a2a4e", hovercolor="#3a3a5e")
        box.text_disp.set_color("white")
        box.text_disp.set_fontsize(6)
        def _on_submit(text):
            try:
                v = float(text)
                if 0.1 <= v <= 30.0:
                    target_ref[0] = v
            except ValueError:
                pass
        box.on_submit(_on_submit)
        _btn_widgets.append(box)

    if "EMO" in _toggle_y_positions:
        _make_window_input(_toggle_y_positions["EMO"], _emo_window[0], _emo_window, "sec")
    if "PRS" in _toggle_y_positions:
        _make_window_input(_toggle_y_positions["PRS"], _display_window[0], _display_window, "view")

    # ── Bottom control bar ──
    # LOG toggle
    ax_log = fig.add_axes([0.08, 0.02, 0.08, 0.04])
    btn_log = Button(ax_log, "● LOG", color="#444", hovercolor="#555")
    btn_log.label.set_fontsize(8)
    _log_btn_state = [False]

    def _toggle_log(event):
        if _log_btn_state[0]:
            log_stop()
            btn_log.label.set_text("● LOG")
            btn_log.color = "#444"
            _log_btn_state[0] = False
        else:
            log_start()
            btn_log.label.set_text("■ STOP LOG")
            btn_log.color = "#CC4444"
            _log_btn_state[0] = True
    btn_log.on_clicked(_toggle_log)
    _btn_widgets.append(btn_log)

    # SAVE AS
    ax_save = fig.add_axes([0.18, 0.02, 0.08, 0.04])
    btn_save = Button(ax_save, "SAVE AS", color="#666", hovercolor="#777")
    btn_save.label.set_fontsize(8)

    def _save_as(event):
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"track_{ts}.csv",
            initialdir=OUTPUT_DIR,
        )
        root.destroy()
        if path:
            if _log_btn_state[0]:
                log_stop()
                if _log_path and os.path.exists(_log_path):
                    os.rename(_log_path, path)
                    print(f"[SAVE] moved → {path}")
                btn_log.label.set_text("● LOG")
                btn_log.color = "#444"
                _log_btn_state[0] = False
            else:
                print(f"[SAVE] Start logging first, then save.")
    btn_save.on_clicked(_save_as)
    _btn_widgets.append(btn_save)

    # OSC toggle
    ax_osc = fig.add_axes([0.38, 0.02, 0.08, 0.04])
    btn_osc = Button(ax_osc, "● OSC", color="#444", hovercolor="#555")
    btn_osc.label.set_fontsize(8)
    _osc_btn_state = [False]

    def _toggle_osc(event):
        if _osc_btn_state[0]:
            osc_stop()
            btn_osc.label.set_text("● OSC")
            btn_osc.color = "#444"
            _osc_btn_state[0] = False
        else:
            ip = _osc_ip_box.text if _osc_ip_box else ARGS.osc_ip
            try:
                port = int(_osc_port_box.text) if _osc_port_box else ARGS.osc_port
            except ValueError:
                port = ARGS.osc_port
            osc_start(ip, port)
            btn_osc.label.set_text("■ STOP OSC")
            btn_osc.color = "#CC8800"
            _osc_btn_state[0] = True
    btn_osc.on_clicked(_toggle_osc)
    _btn_widgets.append(btn_osc)

    # OSC IP
    ax_ip_lbl = fig.add_axes([0.48, 0.02, 0.03, 0.04])
    ax_ip_lbl.set_facecolor("#1a1a2e")
    ax_ip_lbl.axis("off")
    ax_ip_lbl.text(0.5, 0.5, "IP:", color="gray", fontsize=7,
                   ha="center", va="center", transform=ax_ip_lbl.transAxes)

    ax_ip = fig.add_axes([0.51, 0.02, 0.12, 0.04])
    ax_ip.set_facecolor("#2a2a4e")
    _osc_ip_box = TextBox(ax_ip, "", initial=ARGS.osc_ip,
                          color="#2a2a4e", hovercolor="#3a3a5e")
    _osc_ip_box.text_disp.set_color("white")
    _osc_ip_box.text_disp.set_fontsize(8)
    _btn_widgets.append(_osc_ip_box)

    # OSC Port
    ax_port_lbl = fig.add_axes([0.64, 0.02, 0.03, 0.04])
    ax_port_lbl.set_facecolor("#1a1a2e")
    ax_port_lbl.axis("off")
    ax_port_lbl.text(0.5, 0.5, "Port:", color="gray", fontsize=7,
                     ha="center", va="center", transform=ax_port_lbl.transAxes)

    ax_port = fig.add_axes([0.67, 0.02, 0.06, 0.04])
    ax_port.set_facecolor("#2a2a4e")
    _osc_port_box = TextBox(ax_port, "", initial=str(ARGS.osc_port),
                            color="#2a2a4e", hovercolor="#3a3a5e")
    _osc_port_box.text_disp.set_color("white")
    _osc_port_box.text_disp.set_fontsize(8)
    _btn_widgets.append(_osc_port_box)

    # ── Animation update ──
    def _update_display(frame_num):
        data = _process_tick()
        if data is None:
            return

        if not _display_on[0]:
            return

        audio = data["audio"]
        total_sec = data["total_sec"]
        elapsed = data["elapsed"]
        df = data["df"]
        times_in_audio = data["times_in_audio"]
        speech_mask = data["speech_mask"]

        # Time mapping
        offset = elapsed - total_sec
        wave_times = np.linspace(0, total_sec, len(audio)) + offset
        lld_times = (times_in_audio + offset) if times_in_audio is not None else None

        if elapsed > _display_window[0]:
            x_min, x_max = elapsed - _display_window[0], elapsed
        else:
            x_min, x_max = 0, _display_window[0]

        for ax in all_ts_axes:
            ax.set_xlim(x_min, x_max)

        # ── Waveform ──
        line_wave.set_data(wave_times, audio)

        # ── Emotion bars (vertical — read shared _emo_smooth) ──
        if _has_emo_display and emo_bars is not None:
            # _emo_smooth already updated by _process_tick
            top_idx = int(np.argmax(_emo_smooth))
            for j, dim in enumerate(EMOTION_DIMS):
                emo_bars[j].set_height(_emo_smooth[j])
                if j == top_idx and _emo_smooth[j] > 0.01:
                    emo_bars[j].set_alpha(1.0)
                    emo_bars[j].set_edgecolor("white")
                    emo_bars[j].set_linewidth(1.5)
                else:
                    emo_bars[j].set_alpha(0.6)
                    emo_bars[j].set_edgecolor("none")
                    emo_bars[j].set_linewidth(0)

        # ── VAD (binary — reuse polygon) ──
        if _proc_vad[0] and speech_mask is not None and lld_times is not None:
            binary_vad = speech_mask.astype(float)
            # Build step polygon vertices
            xs = np.repeat(lld_times, 2)
            ys = np.repeat(binary_vad, 2)
            if len(xs) > 2:
                # shift to create step effect: xs & ys both length 2n
                xs = np.concatenate([[lld_times[0]], xs[1:-1], [lld_times[-1]]])
                ys = np.concatenate([[0], ys[:-2], [0]])
            else:
                xs = np.array([0])
                ys = np.array([0])
            _vad_poly.set_xy(np.column_stack([xs, ys]))
            _vad_poly.set_visible(True)
        else:
            _vad_poly.set_visible(False)

        # ── Feature strips ──
        if _proc_prosody[0] and df is not None and len(df) > 0 and lld_times is not None:
            silence_mask = ~speech_mask if speech_mask is not None else np.zeros(len(df), dtype=bool)
            for i, (key, label, color, is_nz, ylim) in enumerate(FEATURES):
                vals = df[key].values.astype(np.float32)
                if is_nz:
                    vals = vals.copy()
                    vals[vals <= 0] = np.nan
                    vals[silence_mask] = np.nan
                lines[i].set_data(lld_times, vals)
        else:
            for i in range(len(lines)):
                lines[i].set_data([], [])

        # ── Title: Timer + log rate + log count ──
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        timer_str = f"{h:02d}:{m:02d}:{s:02d}"
        logs_per_sec = 1000 / UPDATE_MS
        tags = []
        if _log_on:
            tags.append("LOG")
        if _osc_on:
            tags.append("OSC")
        fig.suptitle(
            f"⏱ {timer_str}  │  logs/sec: {logs_per_sec:.0f}  │  "
            f"log: #{_sample_index}"
            + (f"  [{' '.join(tags)}]" if tags else ""),
            fontsize=9, color="white",
        )


# ═══════════════════════════════════════════════════════════════════
# 11. HEADLESS LOOP
# ═══════════════════════════════════════════════════════════════════
def _headless_loop():
    """No display — just extract, log, OSC at UPDATE_MS rate."""
    print("Running headless. Press Ctrl-C to stop.")
    while not _stop_event.is_set():
        _process_tick()
        if _sample_index % 20 == 0:
            print(f"[headless] sample {_sample_index}", end="\r")
        time.sleep(UPDATE_MS / 1000.0)


# ═══════════════════════════════════════════════════════════════════
# 12. CLEANUP + MAIN
# ═══════════════════════════════════════════════════════════════════
def _cleanup():
    global _stream
    _stop_event.set()
    log_stop()
    if _osc_on:
        osc_stop()
    if _stream is not None:
        try:
            _stream.abort()
            _stream.close()
        except Exception:
            pass
        _stream = None
    print("\nClean exit.")


signal.signal(signal.SIGINT, lambda *_: (_cleanup(), sys.exit(0)))
signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(0)))


def main():
    global _stream

    print(f"Config: SR={SR}, display={'ON' if not ARGS.no_display else 'OFF'}, "
          f"emotion={'ON (' + ARGS.emotion_model + ')' if not ARGS.no_emotion else 'OFF'}")
    print("Starting… speak into the mic!")

    try:
        _stream = sd.InputStream(
            samplerate=SR, channels=1, dtype="float32",
            blocksize=int(SR * 0.05), callback=_audio_callback,
        )
        _stream.start()

        # Start emotion thread if enabled
        if _emo_model is not None:
            threading.Thread(target=_emotion_thread, daemon=True).start()

        # Start remote control listener
        threading.Thread(target=_start_ctrl_listener, daemon=True).start()

        # Auto-start OSC if requested
        if ARGS.osc_autostart:
            osc_start()

        if ARGS.no_display:
            _headless_loop()
        else:
            anim = FuncAnimation(
                fig, _update_display,
                interval=UPDATE_MS, blit=False, cache_frame_data=False,
            )
            plt.show()
    finally:
        _cleanup()

    print("Done.")
    os._exit(0)  # Force exit — TkAgg/sounddevice threads can hang otherwise


if __name__ == "__main__":
    main()

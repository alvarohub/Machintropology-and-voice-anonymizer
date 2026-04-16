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
    python app/main.py                          # with display
    python app/main.py --no-display             # headless (log/OSC only)
    python app/main.py --emotion-model seed     # smaller model for RPi
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

# Add parent dir to path so we can import from src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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
        time.sleep(EMO_INTERVAL_S)
        audio = _get_recent_audio(EMO_INTERVAL_S)
        if audio is None:
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
    """Log one summary row per update tick."""
    if not _log_on or _log_writer is None:
        return
    ms = int((time.time() - _log_t0) * 1000)
    row = [ms, 1 if vad_speech else 0]
    for key, _, _, is_nz, _ in FEATURES:
        v = feature_means.get(key, float("nan"))
        row.append(f"{v:.4f}" if not np.isnan(v) else "")
    if not ARGS.no_emotion:
        with _emo_lock:
            row.append(_latest_emo.get("label", ""))
            row.append(f"{_latest_emo.get('confidence', 0.0):.4f}")
            scores = _latest_emo.get("scores", {})
            for dim in EMOTION_DIMS:
                row.append(f"{scores.get(dim, 0.0):.4f}")
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
# 8. SHARED STATE
# ═══════════════════════════════════════════════════════════════════
_stop_event = threading.Event()
_t_start = time.time()
_stream: sd.InputStream | None = None


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

    # openSMILE — full buffer (the proven approach)
    try:
        df = smile.process_signal(audio, sampling_rate=SR)
    except Exception as e:
        print(f"[openSMILE] {e}")
        return None

    if len(df) == 0:
        return None

    starts = np.array([t.total_seconds() for t in df.index.get_level_values("start")])
    ends = np.array([t.total_seconds() for t in df.index.get_level_values("end")])
    times_in_audio = (starts + ends) / 2.0

    # VAD mask aligned to LLD frames
    speech_mask = _compute_vad_mask(audio, times_in_audio)

    # VAD probability contour
    vad_times, vad_probs = _compute_vad_contour(audio)

    # Summary values for logging/OSC (mean of last ~0.5s of voiced frames)
    n_tail = min(25, len(df))  # ~0.5s at 20ms hop
    tail_mask = speech_mask[-n_tail:]
    tail_df = df.iloc[-n_tail:]
    feature_means = {}
    has_speech = bool(np.any(speech_mask[-n_tail:]))

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

    # Log + OSC
    _log_frame(has_speech, feature_means)
    _osc_send(has_speech, feature_means)

    elapsed = time.time() - _t_start

    return {
        "audio": audio,
        "total_sec": total_sec,
        "elapsed": elapsed,
        "df": df,
        "times_in_audio": times_in_audio,
        "speech_mask": speech_mask,
        "vad_times": vad_times,
        "vad_probs": vad_probs,
        "has_speech": has_speech,
        "feature_means": feature_means,
    }


# ═══════════════════════════════════════════════════════════════════
# 10. DISPLAY (optional)
# ═══════════════════════════════════════════════════════════════════
if not ARGS.no_display:

    n_strips = len(FEATURES) + 2  # waveform + VAD + features
    fig, axes = plt.subplots(
        n_strips, 1, figsize=(14, 1.8 * n_strips), sharex=True,
        gridspec_kw={"height_ratios": [2, 1] + [1] * len(FEATURES)},
    )
    fig.patch.set_facecolor("#1a1a2e")

    # ── Control panel at bottom ──
    # Leave room for buttons
    fig.subplots_adjust(bottom=0.12)

    # Waveform
    ax_wave = axes[0]
    ax_wave.set_facecolor("#1a1a2e")
    ax_wave.set_ylabel("Waveform", color="white", fontsize=8)
    ax_wave.set_ylim(-0.1, 0.1)
    ax_wave.tick_params(colors="gray", labelsize=7)
    line_wave, = ax_wave.plot([], [], color="gray", linewidth=0.3)

    # VAD
    ax_vad = axes[1]
    ax_vad.set_facecolor("#1a1a2e")
    ax_vad.set_ylabel("VAD", color="#88FF88", fontsize=8)
    ax_vad.set_ylim(0, 1.05)
    ax_vad.tick_params(colors="gray", labelsize=7)
    line_vad, = ax_vad.plot([], [], color="#88FF88", linewidth=1.2)
    fill_vad = [None]
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

    # ── Emotion label display ──
    _emo_text = fig.text(0.5, 0.97, "", ha="center", va="top",
                         fontsize=11, color="#FFD700", fontweight="bold")

    # ── Buttons ──
    _btn_widgets = []

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
            # If currently logging, stop and rename
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

    # Display toggle
    _display_on = [True]
    ax_disp = fig.add_axes([0.82, 0.02, 0.10, 0.04])
    btn_disp = Button(ax_disp, "■ DISPLAY ON", color="#336", hovercolor="#448")
    btn_disp.label.set_fontsize(7)

    def _toggle_display(event):
        _display_on[0] = not _display_on[0]
        if _display_on[0]:
            btn_disp.label.set_text("■ DISPLAY ON")
            btn_disp.color = "#336"
        else:
            btn_disp.label.set_text("○ DISPLAY OFF")
            btn_disp.color = "#444"
    btn_disp.on_clicked(_toggle_display)
    _btn_widgets.append(btn_disp)

    # ── Animation update ──
    def _update_display(frame_num):
        data = _process_tick()
        if data is None:
            return

        if not _display_on[0]:
            # Still process (log/OSC) but skip rendering
            return

        audio = data["audio"]
        total_sec = data["total_sec"]
        elapsed = data["elapsed"]
        df = data["df"]
        times_in_audio = data["times_in_audio"]
        speech_mask = data["speech_mask"]
        vad_times = data["vad_times"]
        vad_probs = data["vad_probs"]

        # Time mapping
        offset = elapsed - total_sec
        lld_times = times_in_audio + offset
        vad_display_times = vad_times + offset
        wave_times = np.linspace(0, total_sec, len(audio)) + offset

        if elapsed > DISPLAY_SEC:
            x_min, x_max = elapsed - DISPLAY_SEC, elapsed
        else:
            x_min, x_max = 0, DISPLAY_SEC

        for ax in axes:
            ax.set_xlim(x_min, x_max)

        # Waveform
        line_wave.set_data(wave_times, audio)

        # VAD
        line_vad.set_data(vad_display_times, vad_probs)
        if fill_vad[0] is not None:
            fill_vad[0].remove()
        if len(vad_probs) > 0:
            fill_vad[0] = ax_vad.fill_between(
                vad_display_times, 0, vad_probs,
                alpha=0.25, color="#88FF88", linewidth=0,
            )
        else:
            fill_vad[0] = None

        # Feature strips
        silence_mask = ~speech_mask
        for i, (key, label, color, is_nz, ylim) in enumerate(FEATURES):
            vals = df[key].values.astype(np.float32)
            if is_nz:
                vals = vals.copy()
                vals[vals <= 0] = np.nan
                vals[silence_mask] = np.nan
            lines[i].set_data(lld_times, vals)

            if not is_nz:
                if fills[i] is not None:
                    fills[i].remove()
                fills[i] = feature_axes[i].fill_between(
                    lld_times, 0, vals, alpha=0.3, color=color, linewidth=0,
                )

        # Emotion label
        if not ARGS.no_emotion:
            with _emo_lock:
                lbl = _latest_emo.get("label", "")
                conf = _latest_emo.get("confidence", 0.0)
            if lbl:
                _emo_text.set_text(f"{lbl.upper()} {conf:.0%}")
            else:
                _emo_text.set_text("")

        # Title
        n_voiced = np.count_nonzero(df["F0semitoneFrom27.5Hz_sma3nz"].values > 0)
        n_speech = int(np.count_nonzero(speech_mask))
        peak = np.max(np.abs(audio))
        tags = []
        if _log_on:
            tags.append("LOG")
        if _osc_on:
            tags.append("OSC")
        fig.suptitle(
            f"LLD+VAD — {total_sec:.1f}s buf, "
            f"speech={n_speech}/{len(speech_mask)}, "
            f"voiced={n_voiced}, peak={peak:.4f}"
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

        if ARGS.no_display:
            _headless_loop()
        else:
            anim = FuncAnimation(
                fig, _update_display,
                interval=UPDATE_MS, blit=False, cache_frame_data=False,
            )
            plt.tight_layout(rect=[0, 0.08, 1, 0.95])
            plt.show()
    finally:
        _cleanup()

    print("Done.")


if __name__ == "__main__":
    main()

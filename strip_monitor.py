#!/usr/bin/env python3
"""
Real-time Speech Analysis — Prosody LLD + VAD + Emotion

Architecture:
  - sounddevice callback → rolling audio buffer
  - openSMILE thread: short-window + overlap → 10ms prosody frames → _frame_buf
  - Emotion thread: sliding window + hop → emotion predictions → _emo_buf
  - Logger thread: configurable rate → CSV + OSC (reads both buffers)
  - Display (FuncAnimation): reads both ring buffers for rendering

All parameters live in config.yaml; CLI args override.

Usage:
    conda activate ML311
    python strip_monitor.py                       # with display
    python strip_monitor.py --no-display          # headless (log/OSC only)
    python strip_monitor.py --emotion-model seed  # smaller model
"""

from __future__ import annotations

import argparse
import collections
import csv
import itertools
import os
import sys
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import opensmile
import torch
import yaml


# ═══════════════════════════════════════════════════════════════════
# 0. ARGUMENT PARSING + CONFIG LOADING
# ═══════════════════════════════════════════════════════════════════
def parse_args():
    p = argparse.ArgumentParser(description="Real-time speech analysis")
    p.add_argument("--config", default="config.yaml",
                   help="Path to YAML config file")
    p.add_argument("--no-display", action="store_true",
                   help="Run headless (no matplotlib window)")
    p.add_argument("--no-emotion", action="store_true",
                   help="Disable emotion2vec (prosody + VAD only)")
    p.add_argument("--emotion-model", choices=["base", "seed"], default=None,
                   help="Emotion model size: base (~90M) or seed (~20M)")
    p.add_argument("--osc-ip", default=None)
    p.add_argument("--osc-port", type=int, default=None)
    p.add_argument("--osc-prefix", default=None)
    p.add_argument("--osc-autostart", action="store_true",
                   help="Start OSC streaming automatically on launch")
    p.add_argument("--ctrl-port", type=int, default=9001,
                   help="Port to listen for remote control OSC commands")
    return p.parse_args()


def load_config(args) -> dict:
    """Load config.yaml, then overlay CLI args where provided."""
    cfg_path = Path(args.config)
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        print(f"[CFG] loaded {cfg_path}")
    else:
        cfg = {}
        print(f"[CFG] {cfg_path} not found — using defaults")

    # Defaults
    defaults = dict(
        sample_rate=16000,
        opensmile_interval=1.0,
        opensmile_margin=0.2,
        emo_window=2.0,
        emo_hop=0.5,
        vad_threshold=0.3,
        log_interval=0.25,
        display_n=500,
        display_refresh_ms=150,
        osc_ip="127.0.0.1",
        osc_port=9000,
        osc_prefix="/speech",
        emotion_model="base",
        output_dir="output",
    )
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    # CLI overrides
    if args.emotion_model is not None:
        cfg["emotion_model"] = args.emotion_model
    if args.osc_ip is not None:
        cfg["osc_ip"] = args.osc_ip
    if args.osc_port is not None:
        cfg["osc_port"] = args.osc_port
    if args.osc_prefix is not None:
        cfg["osc_prefix"] = args.osc_prefix

    return cfg


ARGS = parse_args()
CFG = load_config(ARGS)

# Matplotlib — only import if display enabled
if not ARGS.no_display:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Button, TextBox


# ═══════════════════════════════════════════════════════════════════
# 1. CONFIG — derived constants and mutable state
# ═══════════════════════════════════════════════════════════════════
SR = int(CFG["sample_rate"])
OUTPUT_DIR = CFG["output_dir"]

# Mutable refs (settable at runtime via config reload or future UI)
_opensmile_interval = [float(CFG["opensmile_interval"])]
_OPENSMILE_MARGIN = float(CFG["opensmile_margin"])
_emo_window = [float(CFG["emo_window"])]
_emo_hop = [float(CFG["emo_hop"])]
_vad_threshold = float(CFG["vad_threshold"])
_log_interval = [float(CFG["log_interval"])]
_display_n = [int(CFG["display_n"])]
_display_refresh_ms = int(CFG["display_refresh_ms"])

# Audio buffer: big enough for max consumer + margin
def _compute_buf_sec():
    opensmile_need = _opensmile_interval[0] + _OPENSMILE_MARGIN
    emo_need = _emo_window[0]
    return max(opensmile_need, emo_need) + 2.0

_buf_sec = [_compute_buf_sec()]

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
# 2. RING BUFFERS (shared between threads)
# ═══════════════════════════════════════════════════════════════════
# Prosody frame buffer — 10ms resolution, ~16 min at 100k entries
_frame_buf = collections.deque(maxlen=100_000)
_frame_lock = threading.Lock()

# Emotion buffer — ~0.5s resolution
_emo_buf = collections.deque(maxlen=10_000)
_emo_lock = threading.Lock()

# Tracking for incremental openSMILE (only append new frames)
_last_processed_elapsed = [0.0]


# ═══════════════════════════════════════════════════════════════════
# 3. AUDIO ACCUMULATOR
# ═══════════════════════════════════════════════════════════════════
_audio_lock = threading.Lock()
_audio_chunks: list[np.ndarray] = []


def _audio_callback(indata, frames, time_info, status):
    with _audio_lock:
        _audio_chunks.append(indata[:, 0].copy())


def _get_recent_audio(sec: float) -> np.ndarray | None:
    """Return last N seconds of audio from the rolling buffer."""
    with _audio_lock:
        if not _audio_chunks:
            return None
        buf = np.concatenate(_audio_chunks)
        # Trim buffer to max size
        max_samples = int(_buf_sec[0] * SR)
        if len(buf) > max_samples:
            buf = buf[-max_samples:]
            _audio_chunks.clear()
            _audio_chunks.append(buf.copy())
    need = int(sec * SR)
    if len(buf) < int(0.3 * SR):
        return None
    chunk = buf[-need:] if len(buf) >= need else buf
    return chunk.astype(np.float32)


def _get_full_audio() -> np.ndarray | None:
    """Return the full rolling buffer (for waveform display)."""
    with _audio_lock:
        if not _audio_chunks:
            return None
        buf = np.concatenate(_audio_chunks)
        max_samples = int(_buf_sec[0] * SR)
        if len(buf) > max_samples:
            buf = buf[-max_samples:]
            _audio_chunks.clear()
            _audio_chunks.append(buf.copy())
    return buf.astype(np.float32)


# ═══════════════════════════════════════════════════════════════════
# 4. OPENSMILE
# ═══════════════════════════════════════════════════════════════════
smile = opensmile.Smile(
    feature_set=opensmile.FeatureSet.eGeMAPSv02,
    feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
)


# ═══════════════════════════════════════════════════════════════════
# 5. SILERO VAD
# ═══════════════════════════════════════════════════════════════════
print("Loading Silero VAD …")
vad_model, vad_utils = torch.hub.load(
    "snakers4/silero-vad", "silero_vad",
    trust_repo=True, force_reload=False,
)
get_speech_timestamps = vad_utils[0]
print("VAD ready.")

# Lock for VAD model — not thread-safe
_vad_lock = threading.Lock()


def _compute_vad_mask(audio: np.ndarray, lld_times: np.ndarray) -> np.ndarray:
    """Compute binary speech mask aligned to LLD frame times."""
    tensor = torch.from_numpy(audio).float()
    with _vad_lock:
        timestamps = get_speech_timestamps(
            tensor, vad_model,
            threshold=_vad_threshold, sampling_rate=SR,
            min_speech_duration_ms=250,
        )
        vad_model.reset_states()
    mask = np.zeros(len(lld_times), dtype=bool)
    for seg in timestamps:
        t_start = seg["start"] / SR
        t_end = seg["end"] / SR
        mask |= (lld_times >= t_start) & (lld_times <= t_end)
    return mask


# ═══════════════════════════════════════════════════════════════════
# 6. EMOTION MODEL (optional)
# ═══════════════════════════════════════════════════════════════════
_emo_model = None

if not ARGS.no_emotion:
    from src.emotion_model import Emotion2VecModel
    model_map = {
        "base": "iic/emotion2vec_plus_base",
        "seed": "iic/emotion2vec_plus_seed",
    }
    model_name = model_map[CFG["emotion_model"]]
    print(f"Loading emotion2vec ({CFG['emotion_model']}: {model_name}) …")
    _emo_model = Emotion2VecModel(model_name=model_name, device="cpu")
    print(f"emotion2vec ready — {len(_emo_model.dimensions)} classes")


# ═══════════════════════════════════════════════════════════════════
# 7. CSV LOGGING
# ═══════════════════════════════════════════════════════════════════
_log_on = False
_log_file = None
_log_writer = None
_log_t0 = None
_log_path = None
_log_count = [0]

# Recording timer (accumulates across pause/resume; resets on RESET)
_rec_elapsed = [0.0]      # accumulated recording seconds
_rec_resume_t = [0.0]     # time.time() when current session started
_rec_total_rows = [0]     # total rows written since last reset


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
    _log_count[0] = 0
    _rec_resume_t[0] = time.time()
    _log_on = True
    print(f"[LOG] recording → {path}")


def log_stop():
    global _log_on, _log_file, _log_writer, _log_t0
    if not _log_on:
        return
    _log_on = False
    _rec_elapsed[0] += time.time() - _rec_resume_t[0]
    if _log_file:
        _log_file.close()
        print(f"[LOG] stopped → {_log_path}  ({_log_count[0]} rows)")
    _log_file = _log_writer = _log_t0 = None


# ═══════════════════════════════════════════════════════════════════
# 8. OSC STREAMING
# ═══════════════════════════════════════════════════════════════════
_osc_on = False
_osc_client = None


def osc_start(ip: str = None, port: int = None):
    global _osc_on, _osc_client
    ip = ip or CFG["osc_ip"]
    port = port or CFG["osc_port"]
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


def _osc_send(vad_speech: int, feature_means: dict, emo_scores: dict):
    if not _osc_on or _osc_client is None:
        return
    pfx = CFG["osc_prefix"]
    try:
        _osc_client.send_message(f"{pfx}/vad", [float(vad_speech)])
        for key, _, _, _, _ in FEATURES:
            v = feature_means.get(key, 0.0)
            val = float(v) if not np.isnan(v) else 0.0
            _osc_client.send_message(f"{pfx}/{key}", [val])
        if not ARGS.no_emotion and emo_scores:
            top_dim = max(emo_scores, key=emo_scores.get) if emo_scores else ""
            top_val = emo_scores.get(top_dim, 0.0)
            _osc_client.send_message(f"{pfx}/emo/label", [top_dim, float(top_val)])
            vals = [float(emo_scores.get(d, 0.0)) for d in EMOTION_DIMS]
            _osc_client.send_message(f"{pfx}/emo/scores", vals)
    except Exception as e:
        print(f"[OSC] {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════
# 8b. REMOTE CONTROL LISTENER (OSC on CTRL_PORT)
# ═══════════════════════════════════════════════════════════════════
def _start_ctrl_listener():
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

    try:
        server = BlockingOSCUDPServer(("0.0.0.0", ARGS.ctrl_port), disp)
        print(f"[CTRL] listening on :{ARGS.ctrl_port}")
        server.serve_forever()
    except OSError as e:
        print(f"[CTRL] port {ARGS.ctrl_port} unavailable: {e}")


# ═══════════════════════════════════════════════════════════════════
# 9. SHARED STATE
# ═══════════════════════════════════════════════════════════════════
_stop_event = threading.Event()
_t_start = time.time()
_stream: sd.InputStream | None = None

# Processing toggles (initial state from config, togglable at runtime)
_proc_vad = [CFG.get("vad_active", True)]
_proc_emotion = [CFG.get("emotion_active", True)]
_proc_prosody = [CFG.get("prosody_active", True)]
_display_on = [True]

# Rate tracking
_opensmile_rate = [0.0]   # actual frames/sec produced
_emo_rate = [0.0]         # actual predictions/sec
_logger_rate = [0.0]      # actual logger ticks/sec (data output rate)


# ═══════════════════════════════════════════════════════════════════
# 10. OPENSMILE PROCESSING THREAD
# ═══════════════════════════════════════════════════════════════════
def _opensmile_thread():
    """Process short audio windows and append new 10ms frames to _frame_buf."""
    last_call = time.time()
    frames_produced = 0
    rate_window_start = time.time()

    while not _stop_event.is_set():
        time.sleep(_opensmile_interval[0])
        if not _proc_prosody[0]:
            continue

        # Grab short window: interval + margin
        grab_sec = _opensmile_interval[0] + _OPENSMILE_MARGIN
        audio = _get_recent_audio(grab_sec)
        if audio is None or len(audio) < int(0.3 * SR):
            continue

        elapsed_now = time.time() - _t_start
        chunk_duration = len(audio) / SR

        # Run openSMILE on the short chunk
        try:
            df = smile.process_signal(audio, sampling_rate=SR)
        except Exception as e:
            print(f"[openSMILE] {e}", file=sys.stderr)
            continue

        if df is None or len(df) == 0:
            continue

        # Compute frame times in elapsed wall-clock coordinates
        starts = np.array([t.total_seconds() for t in df.index.get_level_values("start")])
        ends = np.array([t.total_seconds() for t in df.index.get_level_values("end")])
        times_in_audio = (starts + ends) / 2.0
        # Convert audio-relative → elapsed: audio ends at elapsed_now
        frame_elapsed = times_in_audio + (elapsed_now - chunk_duration)

        # VAD on this chunk
        if _proc_vad[0]:
            speech_mask = _compute_vad_mask(audio, times_in_audio)
        else:
            speech_mask = None  # VAD inactive → tri-state -1

        # Only keep NEW frames (beyond what we already processed)
        cutoff = _last_processed_elapsed[0]
        new_mask = frame_elapsed > cutoff
        if not np.any(new_mask):
            continue

        new_indices = np.where(new_mask)[0]
        new_count = 0

        with _frame_lock:
            for idx in new_indices:
                t = float(frame_elapsed[idx])
                # Tri-state VAD: -1 = VAD off, 0 = gate closed, 1 = gate open
                if speech_mask is None:
                    vad = -1
                    vad_open = True  # treat as open for feature gating
                else:
                    vad = 1 if bool(speech_mask[idx]) else 0
                    vad_open = (vad == 1)
                entry = {"time_s": t, "vad": vad}
                for key, _, _, is_nz, _ in FEATURES:
                    val = float(df.iloc[idx][key])
                    if is_nz and val <= 0:
                        val = float("nan")
                    if is_nz and not vad_open:
                        val = float("nan")
                    entry[key] = val
                _frame_buf.append(entry)
                new_count += 1

            if new_count > 0:
                _last_processed_elapsed[0] = float(frame_elapsed[new_indices[-1]])

        # Rate tracking
        frames_produced += new_count
        dt = time.time() - rate_window_start
        if dt >= 2.0:
            _opensmile_rate[0] = frames_produced / dt
            frames_produced = 0
            rate_window_start = time.time()


# ═══════════════════════════════════════════════════════════════════
# 11. EMOTION PROCESSING THREAD (sliding window)
# ═══════════════════════════════════════════════════════════════════
def _emotion_thread():
    """Sliding window emotion inference → _emo_buf."""
    preds_produced = 0
    rate_window_start = time.time()

    while not _stop_event.is_set():
        time.sleep(_emo_hop[0])
        if not _proc_emotion[0]:
            continue

        audio = _get_recent_audio(_emo_window[0])
        if audio is None:
            continue

        # Optional: gate on recent VAD activity from _frame_buf
        # (skip if no speech in recent frames)
        if _proc_vad[0]:
            has_recent_speech = False
            with _frame_lock:
                # Check last ~50 frames (~0.5s)
                check_n = min(50, len(_frame_buf))
                if check_n > 0:
                    recent = list(itertools.islice(
                        reversed(_frame_buf), check_n))
                    has_recent_speech = any(f["vad"] > 0 for f in recent)
            if not has_recent_speech:
                continue

        elapsed_now = time.time() - _t_start

        try:
            r = _emo_model.predict(audio, sr=SR)
        except Exception as e:
            print(f"[EMO] {e}", file=sys.stderr)
            continue

        scores = r.get("scores", {})
        label = r.get("label", "")
        confidence = r.get("confidence", 0.0)

        entry = {
            "time_s": elapsed_now,
            "label": label,
            "confidence": confidence,
            "scores": {d: float(scores.get(d, 0.0)) for d in EMOTION_DIMS},
        }

        with _emo_lock:
            _emo_buf.append(entry)

        preds_produced += 1
        dt = time.time() - rate_window_start
        if dt >= 2.0:
            _emo_rate[0] = preds_produced / dt
            preds_produced = 0
            rate_window_start = time.time()


# ═══════════════════════════════════════════════════════════════════
# 12. LOGGER THREAD
# ═══════════════════════════════════════════════════════════════════
def _logger_thread():
    """Write CSV rows and send OSC at configurable rate."""
    last_frame = None
    last_emo = {}
    ticks_produced = 0
    rate_window_start = time.time()

    while not _stop_event.is_set():
        time.sleep(_log_interval[0])
        ticks_produced += 1
        dt = time.time() - rate_window_start
        if dt >= 2.0:
            _logger_rate[0] = ticks_produced / dt
            ticks_produced = 0
            rate_window_start = time.time()

        # Read latest prosody frame
        with _frame_lock:
            if _frame_buf:
                last_frame = _frame_buf[-1].copy()

        # Read latest emotion
        with _emo_lock:
            if _emo_buf:
                last_emo = _emo_buf[-1].copy()

        if last_frame is None:
            continue

        now = time.time()
        vad = last_frame.get("vad", 0)  # tri-state: -1/0/1
        feature_means = {key: last_frame.get(key, float("nan"))
                         for key, _, _, _, _ in FEATURES}
        emo_scores = dict(last_emo.get("scores", {}))

        # Apply zero-on-silence: check recent VAD frames
        if CFG.get("emo_zero_on_silence", False) and _proc_vad[0]:
            with _frame_lock:
                check_n = min(20, len(_frame_buf))
                if check_n > 0:
                    recent = list(itertools.islice(
                        reversed(_frame_buf), check_n))
                    if not any(f["vad"] > 0 for f in recent):
                        emo_scores = {d: 0.0 for d in EMOTION_DIMS}

        # Apply exponential decay based on age of last prediction
        if CFG.get("emo_decay_active", False) and emo_scores:
            tau = CFG.get("emo_decay_tau", 2.0)
            emo_time = last_emo.get("time_s", 0.0)
            age = (now - _t_start) - emo_time
            if age > 0 and tau > 0:
                decay = np.exp(-age / tau)
                emo_scores = {d: v * decay for d, v in emo_scores.items()}

        # CSV
        if _log_on and _log_writer is not None:
            ms = int((now - _log_t0) * 1000)
            row = [ms, vad]  # tri-state: -1 (VAD off), 0 (silent), 1 (speech)
            for key, _, _, _, _ in FEATURES:
                v = feature_means.get(key, float("nan"))
                row.append(f"{v:.4f}" if not np.isnan(v) else "")
            if not ARGS.no_emotion:
                label = last_emo.get("label", "")
                conf = last_emo.get("confidence", 0.0)
                row.append(label)
                row.append(f"{conf:.4f}" if conf else "")
                for d in EMOTION_DIMS:
                    v = emo_scores.get(d, 0.0)
                    row.append(f"{v:.4f}")
            _log_writer.writerow(row)
            _log_count[0] += 1
            _rec_total_rows[0] += 1

        # OSC (receives processed scores — decay/zeroing already applied)
        _osc_send(vad, feature_means, emo_scores)


# ═══════════════════════════════════════════════════════════════════
# 13. DISPLAY (optional)
# ═══════════════════════════════════════════════════════════════════
if not ARGS.no_display:
    from matplotlib.gridspec import GridSpec
    from matplotlib.patches import Polygon

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

    # ── VAD (binary filled) ──
    ax_vad = fig.add_subplot(gs[row_idx], sharex=ax_wave); row_idx += 1
    ax_vad.set_facecolor("#1a1a2e")
    ax_vad.set_ylabel("VAD", color="#88FF88", fontsize=8)
    ax_vad.set_ylim(-0.05, 1.15)
    ax_vad.set_yticks([0, 1])
    ax_vad.set_yticklabels(["sil", "spk"], fontsize=6, color="#88FF88")
    ax_vad.tick_params(colors="gray", labelsize=7)
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
        ax.set_xlim(0, _display_n[0] * 0.01)
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
    for idx, (name, flag_ref, off_color, on_color) in enumerate(_toggle_specs):
        y_pos = 0.88 - idx * (0.68 / max(n_tgl - 1, 1))
        ax_btn = fig.add_axes([0.015, y_pos, 0.06, 0.035])
        ax_btn.set_facecolor(on_color)
        btn = Button(ax_btn, f"■ {name}", color=on_color, hovercolor=on_color)
        btn.label.set_fontsize(7)
        btn.label.set_color("white")
        btn_ref = [btn]
        btn.on_clicked(_make_toggle_cb(flag_ref, btn_ref, name, off_color, on_color))
        _btn_widgets.append(btn)

    # ── Bottom control bar ──
    # LOG toggle
    ax_log = fig.add_axes([0.08, 0.02, 0.10, 0.04])
    btn_log = Button(ax_log, "START LOG", color="#444", hovercolor="#444")
    btn_log.label.set_fontsize(8)
    _log_btn_state = [False]

    def _toggle_log(event):
        if _log_btn_state[0]:
            log_stop()
            btn_log.label.set_text("START LOG")
            btn_log.color = "#444"
            btn_log.hovercolor = "#444"
            _log_btn_state[0] = False
        else:
            log_start()
            btn_log.label.set_text("STOP LOG")
            btn_log.color = "#CC4444"
            btn_log.hovercolor = "#CC4444"
            _log_btn_state[0] = True
    btn_log.on_clicked(_toggle_log)
    _btn_widgets.append(btn_log)

    # RESET LOG — stop current log, start a fresh one (new file)
    ax_reset = fig.add_axes([0.19, 0.02, 0.08, 0.04])
    btn_reset = Button(ax_reset, "↺ RESET", color="#555", hovercolor="#555")
    btn_reset.label.set_fontsize(8)

    def _reset_log(event):
        if not _log_btn_state[0]:
            print("[LOG] not logging — nothing to reset")
            return
        log_stop()
        _rec_elapsed[0] = 0.0
        _rec_total_rows[0] = 0
        log_start()
        print("[LOG] reset — new file started")
    btn_reset.on_clicked(_reset_log)
    _btn_widgets.append(btn_reset)

    # SAVE AS
    ax_save = fig.add_axes([0.28, 0.02, 0.08, 0.04])
    btn_save = Button(ax_save, "SAVE AS", color="#666", hovercolor="#666")
    btn_save.label.set_fontsize(8)

    # Recording timer label (between SAVE AS and OSC)
    ax_rec_timer = fig.add_axes([0.37, 0.02, 0.12, 0.04])
    ax_rec_timer.set_facecolor("#1a1a2e")
    ax_rec_timer.axis("off")
    _rec_timer_text = ax_rec_timer.text(
        0.5, 0.5, "", color="#888888", fontsize=8,
        ha="center", va="center", transform=ax_rec_timer.transAxes,
        family="monospace")

    def _save_as(event):
        import tkinter as tk
        from tkinter import filedialog
        import shutil

        # Determine source file: current log (if recording) or last log
        if _log_btn_state[0]:
            # Currently recording — stop first to flush
            log_stop()
            btn_log.label.set_text("START LOG")
            btn_log.color = "#444"
            btn_log.hovercolor = "#444"
            _log_btn_state[0] = False
        src = _log_path
        if not src or not os.path.exists(src):
            print("[SAVE] No log file to save.")
            return

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
            shutil.copy2(src, path)
            print(f"[SAVE] copied → {path}")
    btn_save.on_clicked(_save_as)
    _btn_widgets.append(btn_save)

    # OSC toggle  — pushed right with a gap after LOG controls
    ax_osc = fig.add_axes([0.54, 0.02, 0.10, 0.04])
    btn_osc = Button(ax_osc, "● SEND OSC", color="#444", hovercolor="#444")
    btn_osc.label.set_fontsize(8)
    _osc_btn_state = [False]

    _osc_ip_box = None
    _osc_port_box = None

    def _toggle_osc(event):
        if _osc_btn_state[0]:
            osc_stop()
            btn_osc.label.set_text("● SEND OSC")
            btn_osc.color = "#444"
            btn_osc.hovercolor = "#444"
            _osc_btn_state[0] = False
        else:
            ip = _osc_ip_box.text if _osc_ip_box else CFG["osc_ip"]
            try:
                port = int(_osc_port_box.text) if _osc_port_box else CFG["osc_port"]
            except ValueError:
                port = CFG["osc_port"]
            osc_start(ip, port)
            btn_osc.label.set_text("■ STOP OSC")
            btn_osc.color = "#CC8800"
            btn_osc.hovercolor = "#CC8800"
            _osc_btn_state[0] = True
    btn_osc.on_clicked(_toggle_osc)
    _btn_widgets.append(btn_osc)

    # OSC IP
    ax_ip_lbl = fig.add_axes([0.64, 0.02, 0.03, 0.04])
    ax_ip_lbl.set_facecolor("#1a1a2e"); ax_ip_lbl.axis("off")
    ax_ip_lbl.text(0.5, 0.5, "IP:", color="gray", fontsize=7,
                   ha="center", va="center", transform=ax_ip_lbl.transAxes)

    ax_ip = fig.add_axes([0.67, 0.02, 0.12, 0.04])
    ax_ip.set_facecolor("#2a2a4e")
    _osc_ip_box = TextBox(ax_ip, "", initial=CFG["osc_ip"],
                          color="#2a2a4e", hovercolor="#3a3a5e")
    _osc_ip_box.text_disp.set_color("white")
    _osc_ip_box.text_disp.set_fontsize(8)
    _btn_widgets.append(_osc_ip_box)

    # OSC Port
    ax_port_lbl = fig.add_axes([0.80, 0.02, 0.03, 0.04])
    ax_port_lbl.set_facecolor("#1a1a2e"); ax_port_lbl.axis("off")
    ax_port_lbl.text(0.5, 0.5, "Port:", color="gray", fontsize=7,
                     ha="center", va="center", transform=ax_port_lbl.transAxes)

    ax_port = fig.add_axes([0.83, 0.02, 0.06, 0.04])
    ax_port.set_facecolor("#2a2a4e")
    _osc_port_box = TextBox(ax_port, "", initial=str(CFG["osc_port"]),
                            color="#2a2a4e", hovercolor="#3a3a5e")
    _osc_port_box.text_disp.set_color("white")
    _osc_port_box.text_disp.set_fontsize(8)
    _btn_widgets.append(_osc_port_box)

    # ── Animation update — reads from ring buffers ──
    def _update_display(frame_num):
        if not _display_on[0]:
            return

        elapsed = time.time() - _t_start

        # ── Snapshot prosody frames ──
        with _frame_lock:
            n_want = _display_n[0]
            n_have = len(_frame_buf)
            n_take = min(n_want, n_have)
            if n_take > 0:
                # Efficiently slice last n_take from deque
                frames = list(itertools.islice(
                    _frame_buf, max(0, n_have - n_take), n_have))
            else:
                frames = []

        # ── Snapshot latest emotion ──
        with _emo_lock:
            emo_entry = _emo_buf[-1].copy() if _emo_buf else None

        if not frames:
            return

        # Extract arrays from frame dicts
        f_times = np.array([f["time_s"] for f in frames])
        f_vad = np.array([f["vad"] for f in frames])

        # Time axis (elapsed seconds)
        if len(f_times) > 1:
            x_min, x_max = f_times[0], f_times[-1]
            # Ensure minimum visible span
            if x_max - x_min < 1.0:
                x_min = x_max - 1.0
        else:
            x_min = max(0, elapsed - 1.0)
            x_max = elapsed

        for ax in all_ts_axes:
            ax.set_xlim(x_min, x_max)

        # ── Waveform — raw audio matching the frame time span ──
        display_sec = x_max - x_min + 0.5
        audio = _get_recent_audio(display_sec)
        if audio is not None and len(audio) > 0:
            n_samples = len(audio)
            wave_t0 = elapsed - n_samples / SR
            wave_times = np.linspace(wave_t0, elapsed, n_samples)
            line_wave.set_data(wave_times, audio)
        else:
            line_wave.set_data([], [])

        # ── Emotion bars (with optional decay / zero-on-silence) ──
        if _has_emo_display and emo_bars is not None and emo_entry is not None:
            scores = emo_entry.get("scores", {})
            vals = [scores.get(d, 0.0) for d in EMOTION_DIMS]

            # Zero bars immediately when VAD gate is closed
            if CFG.get("emo_zero_on_silence", False) and _proc_vad[0]:
                # Use last ~20 frames (~0.2s) from the already-snapshotted data
                tail = f_vad[-20:] if len(f_vad) >= 20 else f_vad
                if len(tail) > 0 and not np.any(tail > 0):
                    vals = [0.0] * len(EMOTION_DIMS)

            # Exponential decay based on age of last prediction
            if CFG.get("emo_decay_active", False):
                tau = CFG.get("emo_decay_tau", 2.0)
                age = elapsed - emo_entry.get("time_s", elapsed)
                if age > 0 and tau > 0:
                    decay = np.exp(-age / tau)
                    vals = [v * decay for v in vals]

            top_idx = int(np.argmax(vals))
            for j, dim in enumerate(EMOTION_DIMS):
                emo_bars[j].set_height(vals[j])
                if j == top_idx and vals[j] > 0.01:
                    emo_bars[j].set_alpha(1.0)
                    emo_bars[j].set_edgecolor("white")
                    emo_bars[j].set_linewidth(1.5)
                else:
                    emo_bars[j].set_alpha(0.6)
                    emo_bars[j].set_edgecolor("none")
                    emo_bars[j].set_linewidth(0)
        elif _has_emo_display and emo_bars is not None:
            for j in range(len(EMOTION_DIMS)):
                emo_bars[j].set_height(0)

        # ── VAD strip ──
        if _proc_vad[0] and len(f_times) > 2:
            binary_vad = np.clip(f_vad, 0, 1).astype(float)
            xs = np.repeat(f_times, 2)
            ys = np.repeat(binary_vad, 2)
            xs = np.concatenate([[f_times[0]], xs[1:-1], [f_times[-1]]])
            ys = np.concatenate([[0], ys[:-2], [0]])
            _vad_poly.set_xy(np.column_stack([xs, ys]))
            _vad_poly.set_visible(True)
        else:
            _vad_poly.set_visible(False)

        # ── Feature strips ──
        if _proc_prosody[0]:
            for i, (key, label, color, is_nz, ylim) in enumerate(FEATURES):
                vals = np.array([f.get(key, float("nan")) for f in frames],
                                dtype=np.float32)
                lines[i].set_data(f_times, vals)
        else:
            for ln in lines:
                ln.set_data([], [])

        # ── Title bar (always visible: compute rates) ──
        prs_hz = _opensmile_rate[0]
        emo_hz = _emo_rate[0]
        avg_hz = _logger_rate[0]
        fig.suptitle(
            f"PRS: {prs_hz:.0f}/sec  │  EMO: {emo_hz:.1f}/sec  │  "
            f"AVG DATA: {avg_hz:.1f}/sec",
            fontsize=9, color="white",
        )

        # ── Recording timer (near LOG buttons) ──
        if _log_on:
            rec_secs = _rec_elapsed[0] + (time.time() - _rec_resume_t[0])
        else:
            rec_secs = _rec_elapsed[0]
        rh, rrem = divmod(int(rec_secs), 3600)
        rm, rs = divmod(rrem, 60)
        if _log_on:
            _rec_timer_text.set_text(f"⏺ {rh:02d}:{rm:02d}:{rs:02d}")
            _rec_timer_text.set_color("#FF4444")
        elif rec_secs > 0:
            _rec_timer_text.set_text(f"⏸ {rh:02d}:{rm:02d}:{rs:02d}")
            _rec_timer_text.set_color("#888888")
        else:
            _rec_timer_text.set_text("")
            _rec_timer_text.set_color("#888888")


# ═══════════════════════════════════════════════════════════════════
# 14. HEADLESS LOOP
# ═══════════════════════════════════════════════════════════════════
def _headless_loop():
    """No display — threads handle everything. Just wait."""
    print("Running headless. Press Ctrl-C to stop.")
    while not _stop_event.is_set():
        time.sleep(1.0)
        elapsed = time.time() - _t_start
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        with _frame_lock:
            n_frames = len(_frame_buf)
        with _emo_lock:
            n_emo = len(_emo_buf)
        avg_hz = _logger_rate[0]
        if _log_on:
            rec_secs = _rec_elapsed[0] + (time.time() - _rec_resume_t[0])
        else:
            rec_secs = _rec_elapsed[0]
        rh2, rrem2 = divmod(int(rec_secs), 3600)
        rm2, rs2 = divmod(rrem2, 60)
        rec_icon = "⏺" if _log_on else "⏸" if rec_secs > 0 else " "
        print(f"[{h:02d}:{m:02d}:{s:02d}] "
              f"PRS={_opensmile_rate[0]:.0f}/s  "
              f"EMO={_emo_rate[0]:.1f}/s  "
              f"AVG={avg_hz:.1f}/s  "
              f"{rec_icon}{rh2:02d}:{rm2:02d}:{rs2:02d}",
              end="\r")


# ═══════════════════════════════════════════════════════════════════
# 15. CLEANUP + MAIN
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
    global _stream, _t_start

    print(f"Config: SR={SR}, display={'ON' if not ARGS.no_display else 'OFF'}, "
          f"emotion={'ON (' + CFG['emotion_model'] + ')' if not ARGS.no_emotion else 'OFF'}")
    print(f"  openSMILE: interval={_opensmile_interval[0]}s, margin={_OPENSMILE_MARGIN}s")
    print(f"  Emotion:   window={_emo_window[0]}s, hop={_emo_hop[0]}s")
    print(f"  Logger:    interval={_log_interval[0]}s")
    print(f"  Display:   N={_display_n[0]} frames ({_display_n[0]*0.01:.1f}s)")
    print("Starting… speak into the mic!")

    try:
        _stream = sd.InputStream(
            samplerate=SR, channels=1, dtype="float32",
            blocksize=int(SR * 0.05), callback=_audio_callback,
        )
        _stream.start()
        _t_start = time.time()

        # Start processing threads
        threading.Thread(target=_opensmile_thread, daemon=True,
                         name="opensmile").start()

        if _emo_model is not None:
            threading.Thread(target=_emotion_thread, daemon=True,
                             name="emotion").start()

        threading.Thread(target=_logger_thread, daemon=True,
                         name="logger").start()

        # Start remote control listener
        threading.Thread(target=_start_ctrl_listener, daemon=True).start()

        # Auto-start log/OSC from config or CLI
        if CFG.get("log_active", False):
            log_start()
        if ARGS.osc_autostart or CFG.get("osc_active", False):
            osc_start()

        if ARGS.no_display:
            _headless_loop()
        else:
            anim = FuncAnimation(
                fig, _update_display,
                interval=_display_refresh_ms, blit=False,
                cache_frame_data=False,
            )
            plt.show()
    finally:
        _cleanup()

    print("Done.")
    os._exit(0)


if __name__ == "__main__":
    main()

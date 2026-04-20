#!/usr/bin/env python3
"""
Real-time Speech → Emotion track

    python main.py

Captures audio from the default microphone, runs emotion2vec inference
on overlapping windows, displays a live radar chart + scrolling timeline,
and optionally writes a timestamped CSV track to the output/ folder.

Prerequisites (macOS):
    brew install portaudio
    pip install -r requirements.txt
"""

from __future__ import annotations

# Force TkAgg BEFORE any other import — the macOS native Cocoa backend
# conflicts with openSMILE's pre-compiled binaries (shared LLVM/Qt libs),
# corrupting PortAudio callbacks and silently dropping audio frames.
import matplotlib
matplotlib.use("TkAgg")

import os
import sys
import time
import queue
import threading
from datetime import datetime

# ============================================================
# CONFIGURATION — edit these to taste
# ============================================================
CONFIG = {
    # Audio
    "sample_rate": 16000,
    "chunk_duration": 2.0,   # seconds per analysis window (adjustable via slider)
    "hop_duration": 2.0,     # seconds of *new* audio before next analysis (= window, no overlap)

    # Model
    # ── MODEL SWAP POINT ─────────────────────────────────────
    "model_name": "iic/emotion2vec_plus_base",
    "device": "cpu",                                # "cpu" | "cuda" | "mps"

    # Voice Activity Detection — skip inference on silence
    "use_vad": True,
    "vad_threshold": 0.3,        # Silero VAD speech probability threshold
    "vad_min_speech_ratio": 0.1, # min fraction of chunk that must be speech

    # Emotion inference — set False to isolate prosody-only for debugging
    "enable_emotion": True,

    # Prosody features — extract F0, energy, etc. alongside emotion
    "extract_prosody": True,
    "prosody_lld_interval": 0.5,  # seconds between frame-level LLD extractions

    # Embeddings — save emotion2vec 768-d latent vectors for offline analysis
    "save_embeddings": True,

    # Output
    "output_dir": "output",
    "save_csv": False,           # set True when ready to record

    # Display
    "radar_update_ms": 100,
    "radar_smoothing": 0.35,   # 0 = frozen, 1 = raw (no smoothing)
    "radar_trail": 3,          # ghosted previous frames (0 to disable)
    "timeline_seconds": 5.0,   # scrolling timeline window
}


# ── MODEL SWAP POINT ─────────────────────────────────────────
from src.emotion_model import Emotion2VecModel as EmotionModel

from src.audio_capture import AudioCapture
from src.track_writer import TrackWriter
from archive.radar_display import RadarDisplay


# ============================================================
# Inference thread
# ============================================================

def inference_loop(
    audio_capture: AudioCapture,
    model,
    result_queue: queue.Queue,
    track_writer,
    stop_event: threading.Event,
    vad=None,
    prosody_fn=None,
    extract_embedding: bool = False,
    display=None,
) -> None:
    """Background thread: pull audio → VAD gate → model → push results."""
    while not stop_event.is_set():
        chunk = audio_capture.get_chunk()
        if chunk is None:
            time.sleep(0.05)
            continue

        timestamp_ms = time.time() * 1000.0

        # ── VAD gate (respects display toggle) ──
        vad_active = vad is not None and (display is None or display.vad_enabled)
        if vad_active:
            ratio = vad.speech_ratio(chunk)
            if ratio < CONFIG["vad_min_speech_ratio"]:
                result_queue.put({"no_speech": True, "timestamp_ms": timestamp_ms})
                print(f"[        ··] VAD {ratio:.2f} — silence", end="\r")
                continue

        # ── Skip if emotion disabled via GUI toggle ──
        if display is not None and not display.emo_enabled:
            time.sleep(0.1)
            continue

        # ── Emotion inference ──
        try:
            result = model.predict(
                chunk, sr=CONFIG["sample_rate"],
                extract_embedding=extract_embedding,
            )
        except Exception as exc:
            print(f"[inference] error: {exc}", file=sys.stderr)
            continue

        result["timestamp_ms"] = timestamp_ms

        # ── Prosody features ──
        if prosody_fn is not None:
            try:
                prosody = prosody_fn(chunk, sr=CONFIG["sample_rate"])
                result["prosody"] = prosody
            except Exception as exc:
                print(f"[prosody] error: {exc}", file=sys.stderr)

        # Fan out to display + CSV writer
        result_queue.put(result)
        if track_writer is not None:
            track_writer.write(result, timestamp_ms)

        # Console one-liner
        dims = " ".join(
            f"{d[:3]}={result['scores'].get(d, 0):.2f}"
            for d in model.dimensions
        )
        prosody_str = ""
        if "prosody" in result:
            p = result["prosody"]
            # Show key eGeMAPSv02 features or fallback features
            f0 = p.get("F0semitoneFrom27.5Hz_sma3nz_amean", p.get("f0_mean", 0))
            loud = p.get("loudness_sma3_amean", 0)
            jit = p.get("jitterLocal_sma3nz_amean", 0)
            hnr = p.get("HNRdBACF_sma3nz_amean", 0)
            prosody_str = f"  F0={f0:.1f}st  L={loud:.2f}  J={jit:.4f}  HNR={hnr:.1f}"
        print(f"[{result['label']:>10s}] {result['confidence']:.0%}  {dims}{prosody_str}")


# ============================================================
# Prosody LLD thread (frame-level, decoupled from emotion)
# ============================================================

def prosody_lld_loop(
    audio_capture: AudioCapture,
    prosody_queue: queue.Queue,
    stop_event: threading.Event,
    sr: int = 16000,
    interval: float = 0.5,
    display=None,
) -> None:
    """Background thread: extract 20ms-frame LLD prosody independently.

    openSMILE's process_signal() is **stateless** — each call treats the
    audio as a standalone utterance with no memory of previous calls.
    Edge frames lose voicing confidence because the pitch tracker has no
    context beyond the chunk boundaries, causing F0/jitter/shimmer/HNR
    to drop to zero at every chunk edge → visible fragmentation.

    Fix: read audio with padding on BOTH sides of the interest window.
    openSMILE processes the full padded chunk (so edges have context),
    then we trim to keep only the middle frames.

        [LEFT_PAD | ──── interest (cur_interval) ──── | RIGHT_PAD]
         discard    ← ← ← keep these frames → → →      discard

    The RIGHT_PAD is audio we already displayed last iteration (slightly
    in the past).  The cost is cur_interval/2 of extra display latency,
    which at 0.5s interval = 0.25s — imperceptible.
    """
    from prosody import extract_prosody_lld
    CONTEXT_PAD = 0.25  # seconds of padding on EACH side

    while not stop_event.is_set():
        cur_interval = display.prosody_lld_interval if display is not None else interval
        time.sleep(cur_interval)
        if stop_event.is_set():
            break
        if display is not None and not display.pros_enabled:
            continue
        # Read padded audio: [LEFT_PAD + interest + RIGHT_PAD]
        read_duration = CONTEXT_PAD + cur_interval + CONTEXT_PAD
        audio = audio_capture.get_latest_audio(read_duration)
        if audio is None or len(audio) < int(sr * 0.1):
            continue
        try:
            lld = extract_prosody_lld(audio, sr=sr)
            if lld is not None:
                # Keep only the middle frames (discard both pads)
                times = lld["times"]
                total_dur = len(audio) / sr
                keep = (times >= CONTEXT_PAD) & (times <= total_dur - CONTEXT_PAD)
                if not keep.any():
                    continue
                lld["times"] = times[keep]
                lld["frames"] = {k: v[keep] for k, v in lld["frames"].items()}
                # Add fields expected by RadarDisplay._ingest_prosody_lld
                lld["wall_now"] = time.time()
                lld["audio_dur"] = total_dur
                # Timestamp reflects the MIDDLE of the interest window
                # (shifted back by RIGHT_PAD since we're reading older audio)
                lld["timestamp_ms"] = (time.time() - CONTEXT_PAD) * 1000.0
                lld["duration_s"] = cur_interval
                prosody_queue.put(lld)
        except Exception as exc:
            print(f"[prosody-lld] {exc}", file=sys.stderr)


# ============================================================
# Main
# ============================================================

def main() -> None:
    os.makedirs(CONFIG["output_dir"], exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(CONFIG["output_dir"], f"emotion_track_{ts}.csv")

    # ---- Load model ---------------------------------------------------------
    if CONFIG["enable_emotion"]:
        print("Loading emotion model …")
        model = EmotionModel(
            model_name=CONFIG["model_name"],
            device=CONFIG["device"],
        )
        print(f"Model ready — dimensions: {model.dimensions}")
    else:
        # Lightweight stub so display still has dimension names
        class _Stub:
            dimensions = ["angry","disgusted","fearful","happy","neutral","other","sad","surprised","unknown"]
        model = _Stub()
        print("⚠️  Emotion model NOT loaded (enable_emotion=False)")

    # ---- Load VAD -----------------------------------------------------------
    vad = None
    if CONFIG["use_vad"]:
        print("Loading Silero VAD …")
        try:
            from vad import SileroVAD
            vad = SileroVAD(
                threshold=CONFIG["vad_threshold"],
                sample_rate=CONFIG["sample_rate"],
            )
            print("VAD ready")
        except Exception as exc:
            print(f"[warn] VAD failed to load ({exc}), running without VAD")

    # ---- Prosody extractor --------------------------------------------------
    prosody_fn = None
    if CONFIG["extract_prosody"]:
        try:
            from prosody import extract_prosody
            prosody_fn = extract_prosody
            print("Prosody extraction enabled")
        except ImportError as exc:
            print(f"[warn] Prosody unavailable ({exc}), skipping")

    # ---- Set up components --------------------------------------------------
    audio = AudioCapture(
        sample_rate=CONFIG["sample_rate"],
        chunk_duration=CONFIG["chunk_duration"],
        hop_duration=CONFIG["hop_duration"],
    )

    # CSV writer (optional)
    save_emb = CONFIG.get("save_embeddings", False)
    writer = None
    if CONFIG["save_csv"]:
        from prosody import PROSODY_FEATURES
        extra_cols = PROSODY_FEATURES if prosody_fn else []
        writer = TrackWriter(
            csv_path, model.dimensions,
            extra_columns=extra_cols,
            save_embeddings=save_emb,
        )
        print(f"CSV saving → {csv_path}")
        if save_emb:
            print(f"Embeddings will be saved alongside CSV ({csv_path.replace('.csv', '_embeddings.npy')})")
    else:
        print("CSV saving disabled (set save_csv=True in CONFIG to enable)")

    result_q: queue.Queue = queue.Queue()
    prosody_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    display = RadarDisplay(
        dimensions=model.dimensions,
        update_interval_ms=CONFIG["radar_update_ms"],
        smoothing=CONFIG["radar_smoothing"],
        trail_frames=CONFIG["radar_trail"],
        timeline_seconds=CONFIG["timeline_seconds"],
        chunk_duration=CONFIG["chunk_duration"],
        vad_threshold=CONFIG["vad_threshold"],
        vad_enabled=CONFIG["use_vad"],
        prosody_lld_interval=CONFIG["prosody_lld_interval"],
    )
    display.set_queue(result_q)
    display.set_audio_capture(audio)
    if vad is not None:
        display.set_vad(vad)
    display.set_prosody_queue(prosody_q)

    # ---- Start capture & inference ------------------------------------------
    audio.start()
    print(f"🎙  Listening … (chunk={CONFIG['chunk_duration']}s, hop={CONFIG['hop_duration']}s)")

    if CONFIG["enable_emotion"]:
        inf_thread = threading.Thread(
            target=inference_loop,
            args=(audio, model, result_q, writer, stop, vad, prosody_fn),
            kwargs={"extract_embedding": save_emb and CONFIG["save_csv"], "display": display},
            daemon=True,
        )
        inf_thread.start()
    else:
        print("⚠️  Emotion inference DISABLED (enable_emotion=False)")

    pros_thread = threading.Thread(
        target=prosody_lld_loop,
        args=(audio, prosody_q, stop),
        kwargs={"sr": CONFIG["sample_rate"], "interval": CONFIG["prosody_lld_interval"],
                "display": display},
        daemon=True,
    )
    pros_thread.start()
    print(f"\U0001f52c Prosody LLD running (every {CONFIG['prosody_lld_interval']}s, 20ms frames)")

    # ---- Run display (blocks until window is closed) ------------------------
    try:
        display.run()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down …")
        stop.set()
        audio.stop()
        if writer is not None:
            writer.close()
            print(f"Track saved → {csv_path}")
        print("Done.")


if __name__ == "__main__":
    main()

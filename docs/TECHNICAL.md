# Technical Documentation

## Repository Structure

```
├── main.py                  # Entry point, configuration, thread orchestration
├── emotion_model.py         # emotion2vec wrapper (MODEL SWAP POINT)
├── audio_capture.py         # Mic → ring buffer with consumer/observer reads
├── prosody.py               # openSMILE eGeMAPSv02 (Functionals + LLD)
├── vad.py                   # Silero VAD wrapper
├── radar_display.py         # Live radar + scrolling timeline (matplotlib)
├── track_writer.py          # CSV + embedding (.npy) persistence
├── run.sh                   # Launch script with conda activation
├── requirements.txt
│
├── test/                    # Diagnostic test suite
│   ├── benchmark_pipeline.py
│   ├── test_emotion.py      # Synthetic pitch-sweep probes
│   ├── test_prosody.py      # openSMILE accuracy & calibration
│   └── test_vad.py          # VAD threshold & latency tests
│
├── chronicle/               # The machintropological record
│   ├── Journal.md           # The living chronicle (Entries 1–6+)
│   ├── notes.md             # Verbatim fragments from conversation
│   ├── Sparks.md            # Luminous asides — extracted from the Journal
│   ├── Gems.md              # Short passages that stopped us in our tracks
│   ├── SpinOffs.md          # Seeds — actionable project ideas from the process
│   ├── DomainsOfExpertise.txt  # Theoretical foundation for the chronicler
│   └── session_log_*.md     # Raw session context
│
├── docs/
│   ├── TECHNICAL.md         # Architecture & setup guide
│   └── MACHINTROPOLOGY.md   # The experiment explained
│
├── .github/agents/
│   └── chronicle.agent.md   # The Chronicler's identity and instructions
│
└── output/                  # Generated CSV tracks + embeddings (gitignored)
```

## Architecture

The system is a real-time pipeline with three decoupled threads sharing a ring buffer:

```
Microphone
    │
    ▼
┌──────────────────┐
│  AudioCapture    │  sounddevice callback → ring buffer
│  (audio thread)  │           │
└──────────────────┘           │
    │                          │
    │ get_chunk()              │ get_latest_audio()
    │ (consuming, every 2s)    │ (non-consuming, every 0.5s)
    ▼                          ▼
┌───────────────────┐    ┌───────────────────────┐
│  Emotion Thread   │    │  Prosody LLD Thread   │
│                   │    │                       │
│  Silero VAD gate  │    │  openSMILE eGeMAPSv02 │
│  emotion2vec      │    │  LowLevelDescriptors  │
│  → 9 classes      │    │  → 25 features        │
│  → 768-d embed    │    │  @ 20ms frames        │
│  → 88 functionals │    │                       │
└───────────────────┘    └───────────────────────┘
    │                          │
    │ result_queue             │ prosody_queue
    ▼                          ▼
┌──────────────────────────────────┐
│  RadarDisplay (main thread)      │
│                                  │
│  Radar chart + emotion strips    │
│  + prosody waveforms (scrolling) │
│  + VAD controls + window slider  │
└──────────────────────────────────┘
    │
    ▼
┌──────────────────┐
│  TrackWriter     │  CSV + .npy embeddings (optional)
└──────────────────┘
```

**Why two threads and two read methods?**

Emotion classification needs long windows (2–4 seconds) because emotional state is slow — it accumulates across phrases. Prosody features like F0 need short windows because vocal gesture is fast — a rising inflection happens in 200ms. Forcing both to share a clock destroys the temporal structure of prosody. The decoupled architecture lets each run at its natural cadence.

`get_chunk()` is a consuming read — it advances the ring buffer pointer. The emotion thread uses this to ensure each chunk is processed exactly once. `get_latest_audio()` is a non-consuming read — it copies the latest N seconds without moving the pointer. The prosody thread uses this to independently sample the audio stream.

---

## Quick Start

```bash
# Prerequisites (macOS)
brew install portaudio
# Linux: apt install portaudio19-dev

# Set up environment
conda create -n ML311 python=3.11 -y
conda activate ML311
pip install -r requirements.txt
pip install opensmile  # recommended: richer prosody features

# Run
python main.py
# or: ./run.sh
```

The emotion2vec model (~350 MB) downloads automatically on first run from ModelScope/HuggingFace.

**openSMILE** is optional but strongly recommended — without it, prosody falls back to pyworld (basic F0 + energy only). The `requirements.txt` installs: numpy, sounddevice, matplotlib, funasr, torch, torchaudio, modelscope, pyworld.

---

## Configuration

All settings live in the `CONFIG` dict at the top of `main.py`:

| Key                    | Default                     | Description                                                  |
| ---------------------- | --------------------------- | ------------------------------------------------------------ |
| `sample_rate`          | 16000                       | Audio sample rate (Hz)                                       |
| `chunk_duration`       | 2.0                         | Emotion analysis window (seconds, adjustable via GUI slider) |
| `hop_duration`         | 2.0                         | Hop between emotion chunks (= window, no overlap)            |
| `prosody_lld_interval` | 0.5                         | Seconds between LLD prosody extractions                      |
| `model_name`           | `iic/emotion2vec_plus_base` | ModelScope model ID                                          |
| `device`               | `cpu`                       | `cpu`, `cuda`, or `mps`                                      |
| `use_vad`              | True                        | Enable voice activity detection                              |
| `vad_threshold`        | 0.3                         | Silero VAD sensitivity (adjustable via GUI)                  |
| `vad_min_speech_ratio` | 0.1                         | Min speech fraction to trigger inference                     |
| `extract_prosody`      | True                        | Enable prosody feature extraction                            |
| `save_embeddings`      | True                        | Save 768-d embeddings alongside CSV                          |
| `save_csv`             | False                       | Enable CSV track recording                                   |
| `radar_update_ms`      | 100                         | Display refresh interval                                     |
| `radar_smoothing`      | 0.35                        | EMA smoothing (0=frozen, 1=raw)                              |
| `radar_trail`          | 3                           | Ghost trail frames on radar                                  |
| `timeline_seconds`     | 30.0                        | Scrolling timeline window                                    |

---

## File Reference

### Core pipeline

| File                                      | Purpose                                                                                        |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------- |
| [`main.py`](../main.py)                   | Entry point. CONFIG dict, inference_loop, prosody_lld_loop, orchestration.                     |
| [`audio_capture.py`](../audio_capture.py) | Mic → ring buffer. `get_chunk()` (consuming) and `get_latest_audio()` (non-consuming).         |
| [`emotion_model.py`](../emotion_model.py) | emotion2vec wrapper with MODEL SWAP GUIDE for replacing the classifier.                        |
| [`prosody.py`](../prosody.py)             | openSMILE eGeMAPSv02 — both Functionals (88 utterance-level) and LLD (25 frame-level at 20ms). |
| [`vad.py`](../vad.py)                     | Silero VAD wrapper. `speech_ratio()` and `has_speech()`.                                       |
| [`radar_display.py`](../radar_display.py) | Live GUI: radar chart, emotion strips, prosody waveforms, VAD controls, window slider.         |
| [`track_writer.py`](../track_writer.py)   | Append-only CSV + optional .npy embedding persistence.                                         |

### Test suite

| File                                                          | What it tests                                                 |
| ------------------------------------------------------------- | ------------------------------------------------------------- |
| [`test/benchmark_pipeline.py`](../test/benchmark_pipeline.py) | Full pipeline timing (emotion + prosody + VAD).               |
| [`test/test_emotion.py`](../test/test_emotion.py)             | Synthetic pitch sweeps exposing emotion2vec's frequency bias. |
| [`test/test_prosody.py`](../test/test_prosody.py)             | openSMILE accuracy, NaN check, F0 calibration data.           |
| [`test/test_vad.py`](../test/test_vad.py)                     | VAD threshold sensitivity and onset latency.                  |

---

## Models and Features

### emotion2vec_plus_base

- **Source**: [ModelScope iic/emotion2vec_plus_base](https://modelscope.cn/models/iic/emotion2vec_plus_base)
- **Dimensions**: angry, disgusted, fearful, happy, neutral, other, sad, surprised, unknown
- **Embeddings**: 768-dimensional self-supervised representations
- **Known limitation**: Strong pitch→emotion bias. Labels are unreliable for natural speech; embeddings capture meaningful acoustic structure. See [test/test_emotion.py](../test/test_emotion.py) for evidence.

### openSMILE eGeMAPSv02

- **Functionals**: 88 utterance-level features (means, percentiles, slopes). Used for CSV recording.
- **LowLevelDescriptors**: 25 frame-level features at 20ms windows / 10ms hop. Used for live display.
- **Key LLD features displayed**: F0 (semitones from 27.5Hz), Loudness, Jitter, Shimmer, HNR
- **F0 algorithm**: SWIPE' (Camacho & Harris, 2008) — not YIN

### Silero VAD

- Loaded via `torch.hub` (no extra pip package)
- 512-sample (32ms) internal windows
- Correctly rejects synthetic signals (trained on real speech signatures)
- Lower threshold (0.15–0.20) recommended for quiet/accented speakers

---

## GUI Controls

The display window has:

- **Radar chart** (left): 9-dimension emotion polar plot with EMA smoothing and ghost trails
- **Window slider** (1–8s): Changes the emotion analysis window size in real time
- **VAD threshold slider** (0.05–0.9): Adjusts voice activity detection sensitivity
- **VAD ON/OFF button**: Toggles VAD entirely (green = on, red = off)
- **Emotion strips** (right, top): 9 thin scrolling strips for each emotion dimension
- **Prosody waveforms** (right, bottom): 5 continuous waveforms — F0, Loudness, Jitter, Shimmer, HNR at frame-level resolution

---

## Output Format

When `save_csv=True`:

- `output/emotion_track_YYYYMMDD_HHMMSS.csv` — one row per analysis window, columns: timestamp, elapsed, 9 emotion scores, 88 prosody functionals
- `output/emotion_track_YYYYMMDD_HHMMSS_embeddings.npy` — Nx768 array of emotion2vec embeddings (when `save_embeddings=True`)

---

## Swapping the Emotion Model

The codebase is designed for model replacement. See the MODEL SWAP GUIDE in [`emotion_model.py`](../emotion_model.py). Any model that accepts a 16kHz audio array and returns a dict with `label`, `confidence`, `scores`, and optionally `embedding` will work.

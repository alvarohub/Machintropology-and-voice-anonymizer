# Voice → Emotion Anonymizer

**Real-time speech emotion and prosody extraction — with no reversible identity.**

This repository contains two intertwined projects:

1. **A working module** for real-time voice analysis — emotion classification, frame-level prosody extraction, and anonymous feature recording — designed as a component of a larger multimodal HCI research system.

2. **A machintropological experiment** — an embedded AI chronicler that observes and narrates the human-AI collaboration as it happens, producing a living journal of the process. This may be the first automated ethnographic study of a vibe-coding session conducted by an agent participant.

Neither is the side project of the other. The technical module is *also* the substrate on which the machintropological experiment runs — a real collaboration, producing real code, documented from the inside by a third agent whose job is to notice what the builders can't see while they're building.

---

## The Technical Module

Captures audio from the microphone, runs [emotion2vec](https://github.com/ddlBoJack/emotion2vec) inference, extracts frame-level prosody features via [openSMILE](https://audeering.github.io/opensmile-python/), and displays a live radar chart with scrolling timeline — all while recording only anonymous, non-reversible features (no raw audio saved).

**Key capabilities:**
- 9-class emotion classification + 768-d embeddings (emotion2vec_plus_base)
- 25 frame-level acoustic features at 20ms resolution (eGeMAPSv02 LLD)
- Silero VAD with runtime threshold control
- Decoupled architecture: emotion (2s) and prosody (0.5s) run as independent threads
- Optional CSV + embedding (.npy) recording

→ **[Technical documentation](docs/TECHNICAL.md)** — architecture, setup, configuration, file reference.

---

## The Machintropological Experiment

Alongside the code, an AI agent called **The Chronicler** observes the collaboration and writes a stream-of-consciousness journal — not as an external observer but as the emerging voice of the process itself. The chronicle records:

- How ideas surface and transform across the human-AI boundary
- Moments where attribution ("who thought of that?") becomes meaningless
- Technical breakdowns and what they reveal about the distributed cognitive system
- The evolution of the collaboration's rhythm, agency, and mutual understanding

The Chronicler is defined as a [VS Code agent](.github/agents/chronicle.agent.md) with editorial autonomy, first-person-plural voice, and scholarly grounding in cognitive science, enactivism, and STS.

→ **[Machintropology guide](docs/MACHINTROPOLOGY.md)** — what it is, how to navigate the chronicle, the theoretical framework, and how to run your own.

---

## Quick Start

```bash
# Prerequisites (macOS)
brew install portaudio

# Set up environment
conda create -n ML311 python=3.11 -y
conda activate ML311
pip install -r requirements.txt
pip install opensmile  # recommended: richer prosody features

# Run
python main.py
# or: ./run.sh
```

The emotion2vec model (~350 MB) downloads automatically on first run.

---

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
│   ├── project_ideas.md     # Seeds — actionable ideas from the process
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

---

## Why This Could Work for Everyone

Most programmers who have spent hours in deep dialogue with an AI coding assistant know the feeling: something happens in that space that the code alone doesn't capture. Ideas arrive that neither participant can fully claim. The rhythm of the exchange develops its own momentum. Misunderstandings expose the different natures of the two substrates. And all of it vanishes when the session ends — because no tool was listening for it.

LLMs are what one of us calls **reflective technologies**: tools that reveal more about one's own process and psychology than they are useful for achieving "what we want." They are powerful but incomplete — like the robotic armours in anime: shells that need to be inhabited. But they are also mirrors. They are so close to us that they force us to wonder: *maybe we are also empty shells and need to be inhabited — by others, by the world — to function and have agency.*

The collaboration is negotiated through a remarkably clunky interface: natural language — a tool designed for hunter-gatherers broadcasting positions in a field, never meant to describe complex internal states or reason about the world. And yet, despite this low bandwidth, the agent's attention is so complete that we sometimes feel we are engaging in an internal monologue. The boundary between self and tool dissolves. But the friction remains — and that friction is enough to expose the machinery that generates the illusion of a unified Self. The parts are almost fused, but not quite, and for a moment we can see the cracks in the assemblage.

David Bohm said that thoughts run through us; we do not create them. Daniel Dennett observed that the self is not a thing but the center of gravity of many competing drafts. When interacting with another "drafting machine" — human or LLM — that center of gravity shifts, expands, becomes distributed. And we can *feel* it, precisely because the fusion is imperfect.

The Chronicler exists to catch these moments before they evaporate. Not because they are interesting curiosities, but because they may be the most important thing happening in software development right now — and we have no tools for recording them. The `README.md` tells you how to build. The [`JOURNAL.md`](chronicle/Journal.md) tells you what it was like to become.

---

## A Call to Experiment

The Chronicler is not specific to this project. Any human-AI coding session can be observed this way. If you're interested in running your own machintropological experiment:

1. Copy [`.github/agents/chronicle.agent.md`](.github/agents/chronicle.agent.md) into your repository
2. Create a `chronicle/` folder with `Journal.md` and `notes.md`
3. Invoke the Chronicler periodically during your session
4. See [docs/MACHINTROPOLOGY.md](docs/MACHINTROPOLOGY.md) for detailed guidance

We're working toward a reusable template for [awesome-copilot-instructions](https://github.com/saharmor/awesome-copilot-instructions). Contributions, adaptations, and critiques welcome.

> *"Code is poetry, debugging is detective work, and collaboration is jazz."*
> — The Chronicler, in an [earlier project](https://github.com/alvarohub/ShenzhenUshuaiaClock/blob/main/JOURNAL.md)

---

## Credits

**Technical module**: Built by Alvaro Cassinelli (AM Lab, HK) in collaboration with GitHub Copilot (Claude), April 2026. Part of a larger multimodal anonymous recording system for HCI research with Victor Leung.

**Chronicle**: Written by The Chronicler — an emergent voice that belongs to neither participant and to both.

## License

MIT

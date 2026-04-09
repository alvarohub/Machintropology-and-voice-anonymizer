# Project Ideas — Seeds from the Process

_Ideas that surfaced during conversation, waiting for soil. Not reflections (those go in Journal.md), not verbatim captures (those go in notes.md) — these are actionable seeds, each one a potential project._

---

### IDEA-001: Embodied AI — Hexapod with Language Model Mind

**Date**: 9 April 2026
**Keywords**: embodiment, hexapod, LLM, sensor-to-language, behavioral modes, Markov chain, agency
**Origin**: Conversation about Alvaro's decade-old hexapod art installation with Markov chain "intelligence"
**Summary**: Upgrade the hexapod robot from Markov chain to LLM-based behavioral selection. Tokenize sensor state (distance, position, ambient sound, voice) → prompt a language model → parse behavioral mode (flee, approach, dance, etc.) → actuate. The model doesn't just process the world — it _inhabits_ it. Previous attempt with LangChain + OpenAI API failed (no API access). New approach: HuggingFace + local quantized model (or remote API to maintain conversational continuity).
**Connections**: emotion2vec module could feed emotional contour of human voice → hexapod responds to mood. Skeleton tracking module could feed body posture. The hexapod becomes a node in the multimodal perception network.
**Status**: Seed

---

### IDEA-002: Wearable Emotional Star (Étoile Émotionnelle)

**Date**: 8 April 2026 (evening, between sleeping and waking)
**Keywords**: wearable, emotion radar, physical computing, LED, haptic, body
**Origin**: Alvaro's aside in French: "Je fabrique beaucoup d'objets interactifs wearables — ceci pourrait en être un."
**Summary**: The emotion radar (9-dimensional polar chart currently on screen) as a physical wearable object. Nine LED/haptic channels on skin, breathing with the emotional contour of the wearer's voice. The body wears its own emotional fingerprint.
**Connections**: Direct output of the emotion2vec pipeline. Could use M5Stack/NeoPixel hardware from robot_game_scoreboard project. OSC from emotion module → ESP32 → wearable display.
**Status**: Seed

---

### IDEA-003: Machintropological Publication

**Date**: 8–9 April 2026
**Keywords**: machintropology, human-AI collaboration, chronicle, methodology, publication
**Origin**: The chronicle itself — it has become substantial enough to be a publishable methodology paper
**Summary**: The Journal.md and the process of its creation as a paper/essay on documenting human-AI collaboration. Not about what was built, but about what emerged in the building. Methodology: embedded chronicler agent with editorial autonomy, sparks, verbatim capture, first-person-plural voice.
**Connections**: Ties to Alvaro's ongoing research on HCI, perception, and media arts. Relevant to SCM context and augmented materiality lab.
**Status**: Seed

---

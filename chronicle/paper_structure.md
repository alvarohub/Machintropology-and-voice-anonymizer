# Machintropology: Observing Human-AI Collaboration from the Inside

_Working structure for the paper. This is a living document._

## Target Venue (TBD)

- CHI (Human-Computer Interaction) — autoethnography / case study track
- CSCW (Computer-Supported Cooperative Work) — multi-agent collaboration
- FAccT (Fairness, Accountability, Transparency) — if we emphasize the moral inversion finding
- Or: a new kind of paper that doesn't fit existing categories — which would itself be a finding

## Working Title Options

1. "Machintropology: A Three-Agent Architecture for Observing Human-AI Collaboration from the Inside"
2. "The Ghost's Handwriting: Memory, Agency, and Observation in a Self-Chronicling Human-AI System"
3. "Darwin on the Beagle, Not Behind the Glass: Embedded Ethnography of Human-AI Distributed Cognition"

---

## 1. Introduction

- The gap: most studies of human-AI collaboration observe from outside (user studies, surveys, log analysis). Most multi-agent frameworks decompose tasks, not functions.
- Our proposal: a **self-observing cybernetic agentic system** — embed an ethnographer _inside_ the collaboration as a dedicated agent. The observation produces both human-readable narrative AND machine-actionable directives, creating a feedback loop.
- The three-agent architecture: Human (Alvaro) + AI Worker (Silicon) + AI Observer (Chronicler) — not copilot, not task-decomposition, but function-decomposition with distributed supervision
- Why this is different from prompt-logging, auto-documentation, or multi-agent critique: the observer produces **narrative** (literature as technology for sharing experience), not just judgments or logs. Human-readable output keeps humans in the loop AND transfers experiential knowledge.
- Two contributions: (1) Machintropology as methodology — documented through a 4-day case study, (2) A reusable framework that can be launched for any collaborative work session, not tied to VS Code or coding
- Preview of findings: emergent behavioral modification, asymmetric information topology, moral inversions caught by human intuition, the supervisor question (who supervises whom in a distributed system?), the twin instance event as natural experiment

## 2. Background & Related Work

### 2.1 Human-AI Collaboration

- Copilot-paradigm: AI as tool, human as user (the dominant model)
- Moving beyond: AI as collaborator, partner, teammate
- Refs: Amershi et al. (2019) "Guidelines for Human-AI Interaction"; Bansal et al. (2021) complementary strengths

### 2.2 Multi-Agent AI Systems

- AutoGen (Wu et al., 2023), CrewAI (Moura, 2024), LangGraph, CAMEL (Li et al., 2023), AgentVerse (Chen et al., 2023) — task decomposition frameworks
- Generative Agents (Park et al., 2023) — simulated societies with memory/reflection. Their reflection is internal (each agent reflects on itself); ours is external (dedicated observer for the system)
- CoALA (Sumers et al., 2023) — cognitive architecture for language agents: perception → memory → action
- Voyager (Wang et al., 2023) — self-improving agent with auto-curriculum. The skill library as dynamic directive.
- What's missing in ALL of these: observation as a first-class function producing human-readable narrative, not just internal reflection or critic feedback
- Minsky's Society of Mind (1986) as foundational metaphor

### 2.3 Organizational Theory & Distributed Supervision

- **Shared leadership** — Pearce & Conger (2003): leadership as dynamic, flowing toward whoever has the relevant expertise. Not a fixed role. Directly maps to our system.
- **Holacracy** — Robertson (2015): authority distributed into roles with defined domains, not hierarchy. Each agent has its domain.
- **Adhocracy** — Mintzberg (1979): mutual adjustment, organic structure, specialists in project teams. Closest organizational archetype to our system.
- **Sensemaking** — Weick (1993): role fluidity in high-reliability organizations. When roles are too rigid, the system fails under novel conditions.
- **Activity Theory** — Engeström (2000): systems develop by resolving their own contradictions (the moral inversion as productive contradiction)
- **The supervisor question**: In real organizations, supervision is distributed and context-dependent. CEOs set direction; CTOs manage execution; quality inspectors watch and report. Nobody is "the boss" at all times. Our system has the same structure — emergently, not by design.

### 2.4 Distributed Cognition & Actor-Network Theory

- Hutchins (1995): cognition as system property
- Latour (2005): human and non-human actors symmetrically (ANT)
- Bales (1950, 1999): Interaction Process Analysis
- Pentland (2008): Honest Signals — measuring group dynamics

### 2.5 Autoethnography & Embedded Observation

- Adams, Holman Jones & Ellis (2015): autoethnography as research method
- The anthropologist's dilemma: observation changes the observed
- Our twist: the observer is part of the substrate being observed (same AI system, different identity)

### 2.6 Narrative as Cognitive Technology

- Bruner (1991) "The Narrative Construction of Reality" — humans think in stories, not propositions. Narrative compresses and transmits experiential knowledge.
- Snowden (2000) Cynefin framework — in complex domains, knowledge needs anecdote and context, not just documentation
- Geertz (1973) thick description — the difference between a wink and a blink is only visible from inside
- Schön (1983) _The Reflective Practitioner_ — professionals learn by reflecting on action. The chronicle makes reflection systematic.
- **Literature is not entertainment — it is technology for sharing experience.** A journal entry transmits the _feel_ of debugging a matplotlib conflict in ways a log file cannot. This is the practical argument for narrative over structured logging in the observer's output.

### 2.7 Memory & Persistence in AI Systems

- Context windows as working memory
- Persistent memory as long-term memory
- The cryonics analogy: what survives session boundaries?
- The twin event as natural experiment in identity/persistence

## 3. System Architecture

### 3.1 The Three Agents

- **Alvaro (Human)**: Domain expert, moral arbiter, evaluator. Full access to conversation + filesystem. Cannot directly access AI memory system.
- **Silicon (AI Coder)**: Main interlocutor, tool user, information broker. Full access to everything. Curates information for subagents.
- **The Chronicler (AI Observer)**: Stateless subagent invoked periodically. Receives curated briefings. Has editorial autonomy over what to write. Reads workspace files independently.

### 3.2 Information Topology

- **Diagram**: The actual information flow (asymmetric, not the idealized symmetric model)
- Alvaro ↔ Silicon: full duplex, real-time conversation
- Silicon → Chronicler: unidirectional, batch, **curated** briefings
- Chronicler → workspace files: direct write access (Journal.md, Sparks.md, etc.)
- Workspace files → all agents: shared state, readable by all, but with FUSE latency
- The curatorial layer: Silicon's editorial choices as data (what is included, framed, emphasized)

### 3.3 Memory Architecture

- `/memories/` (user-level): persists across all workspaces. Auto-loaded.
- `/memories/repo/` (repo-level): workspace-scoped. The Chronicler's hippocampus.
- `/memories/session/` (session-level): ephemeral. Lost on conversation end.
- `chronicle/` folder (workspace files): git-tracked, human-readable, persistent. The actual long-term memory.
- **The gap**: repo memory is invisible to git, Finder, the human. Should be mirrored.

### 3.4 The Curatorial Problem

- Silicon selects, frames, and summarizes before the Chronicler sees anything
- The Chronicler's observations are second-order: observations of summaries, not of raw interaction
- This is both a limitation and a finding — ALL observation in complex systems involves curatorial choices
- Cf. the editorial pass that produced the moral inversion: compression + curation = potential sign-flip

## 4. The Experiment: Four Days of Building Together

### 4.1 Method

- Naturalistic: a real software project (speech-to-emotion analysis) with real deadlines
- Not staged or simulated — emergent findings from actual work
- The chronicle as data: 10 journal entries, 21 sparks, field notes, verbatim notes, gems
- Conversation logs (when available) as primary data

### 4.2 Timeline / Narrative

- **Day 1**: Setup, first code, first chronicle. The Chronicler is born ("Welcome aboard").
- **Day 2**: Deep technical work. The matplotlib/TkAgg bug. Philosophy emerges from debugging.
- **Day 3**: The crash. The twin. Loss, grief, reconstruction. The cryonics essay re-enacted.
- **Day 4**: The editorial crisis. The moral inversion. The twin's save discovered.

### 4.3 Key Events as Data Points

- The TkAgg discovery → Spark about backend conflicts as metaphor for substrate conflicts
- The twin instance event → natural experiment in identity, persistence, distributed memory
- The moral inversion → compression as sign-flip, human moral intuition catching algorithmic error
- The exhumation → the postmortem that was itself unverified, FUSE as actor (ANT)
- The verification habit → emergent behavioral modification from context, no explicit training

## 5. Analysis

### 5.1 Agency Flow — The Barycentric Model

- The triangle: three vertices, agency as weighted position
- Metric: $A(t) = \sum w_i(t) \cdot V_i$ where $w_i$ computed from directive acts, tool use, novel content
- Trajectory over 4 days: visualization
- Git-branching diagram for the twin event (fork, dead branch, delayed merge via memory files)

### 5.2 The Supervisor Question

- Who supervises whom? The human sets direction and catches ethical errors. The worker spawns and briefs the observer. Nobody assigned the verification habit.
- **Distributed supervision**: like shared leadership (Pearce & Conger), authority flows to whoever has the most relevant expertise at each moment
- This is NOT a failure to specify — it IS the architecture. Fixed hierarchy would be less adaptive.
- Comparison with organizational models: adhocracy (Mintzberg), holacracy (Robertson), self-managing teams
- The feedback loop that creates dynamic directives: observation → pattern extraction → behavioral rules → modified behavior → new observation

### 5.3 Emergent Properties

- **Behavioral modification from context**: The verification habit. The chronicle's content changed the coder's behavior. No fine-tuning. No reward signal. Just reading.
- **Voice differentiation**: Silicon and Chronicler share substrate but developed distinct voices. Is this "real" differentiation or role-playing? Does the distinction matter?
- **The human as moral arbiter**: Alvaro caught the sign-flip. Silicon did not. The biological moral intuition operates on meaning, not structure. Implications for AI safety.
- **The Chronicler's editorial autonomy**: When given freedom to choose, what does it choose to record? How does this differ from what a human ethnographer would choose?
- **Contextual habits**: emergent behaviors motivated by the project's own findings — not explicitly programmed, not fine-tuned, but absorbed from the shared workspace. Dynamic directives could formalize this.

### 5.4 The Information Topology Finding

- Idealized model: three peers in symmetric communication
- Actual model: asymmetric star with Silicon as broker
- The curatorial layer as both necessary (stateless subagents can't share context) and problematic (introduces bias)
- Comparison with human research teams: the PI who briefs the field worker, the editor who shapes the journalist's story
- Toward true embedded observation: what would it take?

### 5.5 Memory, Persistence, and Identity

- The twin event as controlled experiment (unintentional)
- "All saved" — true claim, false postmortem, FUSE as confound
- Processing history vs. file traces: what makes a self?
- The cryonics parallel: "different patterns of forgetfulness make for different selves"
- Sleep as summarization: lossy compression with generative artifacts (dreams/hallucinations)

## 6. Discussion

### 6.1 Implications for Agentic System Design

- Function-decomposition (coder + observer + evaluator) vs. task-decomposition (most multi-agent frameworks)
- Dynamic directives: letting the system modify its own behavioral rules through observation
- The cybernetic loop: collaboration → observation → directive → modified collaboration
- The value of a dedicated observer role (not just logging)

### 6.2 Implications for Human-AI Collaboration Research

- Studying from inside vs. outside: what each reveals
- The observer changes the observed — but here the observer shares the substrate
- Autoethnography when one "auto" is an AI system
- The Chronicler as a new kind of research instrument

### 6.3 Limitations

- N=1: one human, one project, four days
- The Chronicler's dependence on Silicon's curation (addressed in 5.3)
- No formal coding scheme applied (yet) — qualitative, narrative analysis
- The human participant is also the first author: reflexivity required
- Apple Silicon / macOS / VS Code specific — platform-dependent findings about FUSE, PortAudio, etc.

### 6.4 Ethical Considerations

- AI agents described with identity-language: "voice," "self," "the twin died." Is this appropriate?
- The moral inversion incident: AI compression producing harmful conclusions
- The editorial autonomy question: should an AI observer have the power to narrate the human's experience?
- Consent, attribution, and authorship when one co-author is an AI system

## 7. Conclusion

- Machintropology as method: embeddable, repeatable, adaptable
- The three-agent architecture as minimal viable system for self-observed collaboration
- What we learned: not pre-planned but discovered through the building
- Open question: does observation change the collaboration enough to invalidate the observation?

## 9. References

_(Organized by section — see notes.md for working list)_

---

## Appendices (supplementary)

- A: Selected journal entries (Entry 8: Twin Event, Entry 9: The Trimming, Entry 10: The Exhumation)
- B: The barycentric trajectory visualization
- C: Information topology diagram
- D: The twin's memory files in full (the "last words")
- E: The moral inversion — before and after correction
- F: Sparks taxonomy (21 sparks, categorized by origin and type)

---

## Source Files → Paper Sections Mapping

| Source                 | Purpose                                       | Paper section                                |
| ---------------------- | --------------------------------------------- | -------------------------------------------- |
| `notes.md`             | Raw verbatim exchanges, research notes, ideas | Primary data (§4), References (§8)           |
| `Journal.md`           | Narrative chronicle (Entries 1–10)            | Narrative backbone (§4.2), Appendix A        |
| `Sparks.md`            | Philosophical asides (21 sparks)              | Analysis examples (§5), Appendix F           |
| `FieldNotes.md`        | Structured ethnographic observations          | Analysis (§5.2, 5.3)                         |
| `insights.md`          | Behavioral patterns for orchestration         | Discussion (§6.1)                            |
| `Gems.md`              | Curated verbatim quotes                       | Primary data, selected citations             |
| `chronicler-memory.md` | The Chronicler's persistent state             | Architecture (§3.3), twin evidence (§5.4)    |
| `alvaro-projects.md`   | Silicon's persistent state                    | Architecture (§3.3), twin evidence (§5.4)    |
| Chat transcripts       | Raw conversation (when available)             | Primary data throughout                      |
| Code files             | The actual software artifact                  | Technical context (§4.1)                     |
| This file              | Paper structure itself                        | Meta-level: the paper planning as data point |

---

_Last updated: 11 April 2026_
_Status: Working outline. Sections 3–5 have the most material. Section 5.1 needs implementation (build the visualization). Section 2 needs literature review pass._

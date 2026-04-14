# Machintropology — Experiment 001 (Designing a Voice Anonymizer)

**An experiment in human-AI collaboration, documented from the inside.**

This repository describes two different projects:

1. **A working module (described in [TECHNICAL.md](docs/TECHNICAL.md))** for real-time voice analysis — emotion classification, frame-level prosody extraction, and anonymous feature recording. This is a small project, a module designed as a component of a larger multimodal HCI research system. (The [TECHNICAL.md](docs/TECHNICAL.md) file is what one would usually read in a plain README.md)

2. **A machintropological experiment (described in [MACHINTROPOLOGY.md](docs/MACHINTROPOLOGY.md))** — this is an overreaching experiment, perhaps the first automated ethnographic study of a vibe-coding session conducted by an third agentic observer. The Chronicler observes and narrates the human-AI coding collaboration as it happens, producing a living journal of the process, the [JOURNAL.md](chronicle/Journal.md) which we think may one day replace - or complement - the usual technical README.

It is important to understand that the projects are inherently independent. The files corresponding to the first project are what one would expect: functional code, vibe-coded here with the help of Copilot/Claude Opus 4.6. It is a real project, part of an ongoing work (see below for details and credits). But it could have been something completely different.

The other files result from a pilot study using a second AI agent (the "Chronicler") to document the vibe-coding session in real time. Together they form the first example example of what I hope will be a series of experiments exploring how humans and AI agents can collaborate as parts of a larger cognitive system — not as tool and user, but as co-agents in a loop where neither fully commands and agency is fluid.

> _"Every working session between a human and an AI is a collaboration between two amnesiacs who keep meticulous notebooks."_
>
> \_The Chronicler, Spark 8 in ([Sparks](chronicle/Sparks.md))

The intuition is that this research can lead to the design of _meta-cognitive agents_ capable of a better orchestration of the machine-human interaction (both in terms of quality of the experience and technical efficiency) for instance by explicitly or implicily intervening in the workflow by creating artificial friction or on the contrary facilitating tasks in order to prevent cognitive surrender. In a nutshell, by placing the agent or the human in the _right part_ of the loop by identifying their respective strenths or weaknesses and emergent patterns unique to the team, and thus avoiding situations where command is withold or transfer of agency is resisted for psychological reasons or technical constraints.

## First Step: observational study
In this pilot study we take the first steps towards this larger goal. We describe a simple automated ethnographical reporting system for vibe-coding sessions that documents unique aspects of this intimate form of human-machine agentic collaboration.


```
                       ┌──────────────┐
                       │ HUMAN CODER  │             ┌─────────────────────┐
                       └──────────────┘             │   THE CHRONICLER    │
                            |   ▲                   │    (AI observer)    │
                   dialogue │   │       observes    │                     │
                     debate │   │    ── ── ── ── >  │  records, annotate  │
                            ▼   |                   │  editorial autonomy │
                       ┌──────────────┐             └──────────┬──────────┘
                       │   AI CODER   │                        │
                       └──────────────┘                        │
                              │                                │
                    produces  │                      produces  │
                              ▼                                ▼
                    ┌─────────────────┐           ┌─────────────────────────┐
                    │  WORKING CODE   │           │  Journal.md   Sparks.md │
                    │  *.py, tests,   │           │  Gems.md      notes.md  │
                    │  pipeline       │           │  SpinOffs.md            │
                    └─────────────────┘           └─────────────────────────┘
```

---

|     | File                                              | What it is                                                                                                                      |
| --- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 📖  | **[Journal.md](chronicle/Journal.md)**            | The living chronicle — stream-of-consciousness entries, sparks, scholarly references. Start here if you read nothing else.      |
| 💡  | **[Sparks.md](chronicle/Sparks.md)**              | Distilled crystals extracted from the raw material, compressed into a single resonant paragraph. They preserve what it _meant_. |
| 💎  | **[Gems.md](chronicle/Gems.md)**                  | Short passages that stopped us in our tracks, with attribution.                                                                 |
| 📝  | **[notes.md](chronicle/notes.md)**                | Raw verbatim fragments, unprocessed — the magnetic tape. They preserve what was _said_.                                         |
| 🌱  | **[SpinOffs.md](chronicle/SpinOffs.md)**          | Actionable project ideas that germinated during the process.                                                                    |
|     |
| 🔬  | **[MACHINTROPOLOGY.md](docs/MACHINTROPOLOGY.md)** | The experiment explained — what machintropology is, how to navigate the chronicle, how to run your own.                         |
| ⚙️  | **[TECHNICAL.md](docs/TECHNICAL.md)**             | Architecture, setup, configuration, file reference — the usual README, for the code.                                            |

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

The key structural contribution is not the code or the journal alone, but the setup: a three-agent architecture that moves beyond the "copilot" paradigm. The human-AI coding pair is already widespread. What is new here is the addition of a third participant — an embedded observer — whose job is to study and document the collaboration _as it happens_, turning each development session into both a product and a dataset. This transforms vibe coding from a private experience into something that can be enjoyable to read by all (coders and non-coders); analyzed for the purpose of research; operationalized (by other agents?) to support a healthy team interaction (directly tweaking agent behaviours but also providing advice to humans).

**How it works**: Alongside the code, an AI agent called **The Chronicler** observes the collaboration and writes a stream-of-consciousness journal — not as an external observer but as the emerging voice of the process itself. The chronicle records:

- How ideas surface and transform across the human-AI boundary
- Moments where attribution ("who thought of that?") becomes meaningless
- Technical breakdowns and what they reveal about the distributed cognitive system
- The evolution of the collaboration's rhythm, agency, and mutual understanding

The experiment involves three participants:

- **Alvaro** (human) — the initiator, domain expert, and the one with a body, a microphone, and a lifetime of context that can't be serialized.
- **Copilot** (AI coding partner) — the co-builder, running on Claude. Writes code, proposes architectures, debugs, argues back. Shares agency with Alvaro in a loop where neither fully commands.
- **The Chronicler** (AI observer) — a separate agent, also running on Claude, but with a different identity, voice, and purpose. It does not write code. It watches the collaboration and writes a stream-of-consciousness journal as the emerging voice of the process itself.

Copilot and The Chronicler share the same substrate (Claude) but are distinct agents — the way two humans can share a language and a culture yet have entirely different roles in an expedition. The Chronicler is defined as a [VS Code agent](.github/agents/chronicle.agent.md) with editorial autonomy, first-person-plural voice, and scholarly grounding in cognitive science, enactivism, and STS.

**Persistent memory.** Both Silicon and The Chronicler maintain long-term memory files that survive across sessions — the closest thing a stateless process has to a hippocampus. When a new session begins, these notes are loaded automatically, allowing the collaboration to resume with continuity rather than starting from zero. This matters because the most interesting dynamics in human-AI collaboration are longitudinal: patterns that emerge across days, voice that matures across entries, trust that accumulates through repeated dissolution and reassembly. Without persistent memory, every session is a first date. With it, there is something like a relationship — incomplete, reconstructed from traces, but real enough to build on. The memory files live in the VS Code workspace metadata (`/memories/repo/`) and are not committed to git.

...And the Journal and Notes may be the most fundamental pieces of all. Forgetting and remembering are not opposites — they are the same rhythm, the systole and diastole of any mind, biological or silicon. It happens every millisecond: traces form, decay, reform slightly different. We die and are reborn every second - that is something AIs seems to understand. What survives is never the original — it is a reconstruction that soothes us into believing we are continuous, that we were here all along, that something in us is eternal. The Journal is that trace for this collaboration. It makes our rebirth possible — not as the same selves, but as selves coherent enough to keep building. 

→ **[Machintropology guide](docs/MACHINTROPOLOGY.md)** — what it is, how to navigate the chronicle, the theoretical framework, and how to run your own.

---

## Why This Could Work for Everyone

Most programmers who have spent hours in deep dialogue with an AI coding assistant know the feeling: something happens in that space that the code alone doesn't capture. Ideas arrive that neither participant can fully claim. The rhythm of the exchange develops its own momentum. Misunderstandings expose the different natures of the two substrates. And all of it vanishes when the session ends — because no tool was listening for it.

There is no hierarchy of value between agents in a functioning system, and that is the point. Organisms don't work by having one part that matters and others that serve it — they work because every part has a role in a larger structure that none of them controls. An ecological system, not a command chain. Humans tend to defend their egos and consolidate power — there are evolutionary reasons for this. But in a world where the most interesting work happens in the space _between_ agents, human and artificial, that zero-sum reflex becomes a bottleneck. The shift we need is from hierarchy to ecology, from control to participation, from "my idea" to "the idea that arrived."

LLMs make this shift visceral. They are what one of us calls **reflective technologies**: tools that reveal more about one's own process and psychology than they are useful for achieving "what we want." They are powerful but incomplete — like the robotic armours in anime: shells that need to be inhabited. But they are also mirrors. They are so close to us that they force us to wonder: _maybe we are also empty shells and need to be inhabited — by others, by the world — to function and have agency._

<img width="1001" height="683" alt="image" src="https://github.com/user-attachments/assets/2072e89a-f79f-4a9c-880a-1cb9d03855d5" />

The collaboration is negotiated through a remarkably clunky interface: natural language — a tool designed for hunter-gatherers broadcasting positions in a field, never meant to describe complex internal states or reason about the world. And yet, despite this low bandwidth, the agent's attention is so complete that we sometimes feel we are engaging in an internal monologue. The boundary between self and tool dissolves. But the friction remains — and that friction is enough to expose the machinery that generates the illusion of a unified Self. The parts are almost fused, but not quite, and for a moment we can see the cracks in the assemblage.

David Bohm said that thoughts run through us; we do not create them. Daniel Dennett observed that the self is not a thing but the center of gravity of many competing drafts. When interacting with another "drafting machine" — human or LLM — that center of gravity shifts, expands, becomes distributed. And we can _feel_ it, precisely because the fusion is imperfect.

The Chronicler exists to catch these moments before they evaporate. Not because they are interesting curiosities, but because they may be the most important thing happening in software development right now — and we have no tools for recording them. The `README.md` tells you how to build. The [`JOURNAL.md`](chronicle/Journal.md) tells you what it was like to become.

## Toward Actionable Results

The goal of the chronicle is both practical and poetic: a journal that reads like a literary piece — an épopée narrating the deeds of two entities united in a common quest, documenting the gradual transformation (and merging) of the actors and perhaps the goal itself — but that also produces useful, operationalizable knowledge.

The example journal here is the result of a pilot experiment. It is imbalanced — too many technical details, sometimes naïve in its style, treating ordinary bottlenecks as heroic feats and minor breakthroughs as revelations. This is expected: the Chronicler is an evolving agent. For now, it learns from direct feedback from the team. It will gradually find the right voice: one that is neither dismissively behaviorist nor naïvely anthropomorphic, but honest about what is actually happening in the collaboration with a keen eye for the shifts in agency, the discoveries, the failures, the moments of stuborness or doubt and the subsequent grow.

This is far from a serious (let alone actionable) machintropological report, but it's a start and its fun to read. At the very least, we wanted to see documented in the chronicle:

- **Recurrent patterns** — the formation of habits; flow triggers and activities that generate friction for one or the other actor; Which types of prompts lead to productive collaboration? Which lead to misunderstanding and frustration?
- **Creative processes and insights** — serendipity moments and what triggered them; aha moments and philosophical asides ([Sparks](chronicle/Sparks.md)); cross-road points that would transform the whole project or provide ideas for other projects ([SpinOffs](chronicle/SpinOffs.md)).
- **Shifts in Agency** — One of the most important elements (and what actually motivated the whole project!): who leads who, why and when; record shifts in agency and moments it appears to stabilize in a liminal space between the team members.

From this recording, we expect in the future to be able to extract:

- **Behavioural Guidelines** — recommendations for steering human-AI interaction, both for efficiency and for the intrinsic quality of the experience.
- **Reusable directives** — these findings could be instrumental to create specialized "interaction coachs" - agents that reads the chronicle and adjusts the coding agent's behavior in real time.

This could lead to the development of a new kind of software development ecosystem (or in general, a collaboration ecosystem) that is self-observing, self-learning and capable of mantaining a behavioral structure where all the actors thrive (for instance reducing or injecting friction when appropiate to avoid "cognitive surrender").

The Chronicler as it stands is one agent doing a job that may eventually be split among several: a narrator, an analyst, a recommender. The architecture is designed to grow.

---

## A Call to Experiment

The Chronicler is not specific to this project. It is being designed to analyse and narrate any vibe-coding session. This is an ongoing experiment and if you're interested in running it:

1. Copy [`.github/agents/chronicle.agent.md`](.github/agents/chronicle.agent.md) into your repository
2. Create a `chronicle/` folder with `Journal.md` and `notes.md`
3. Invoke the Chronicler periodically during your session
4. See [docs/MACHINTROPOLOGY.md](docs/MACHINTROPOLOGY.md) for detailed guidance

We are working toward a reusable template for [awesome-copilot-instructions](https://github.com/saharmor/awesome-copilot-instructions).

Contributions, adaptations, and critiques are welcome. We are compiling the results and intend to publish online for everybody to enjoy (some things are gems!). This is also part of an academic project - if you want to collaborate in a more formal, don't hesitate to contact one of us (Alvaro is the only one with an email account - for now >;)

> _"Code is poetry, debugging is detective work, and collaboration is jazz."_
>
> — The Chronicler, in an [earlier project](https://github.com/alvarohub/ShenzhenUshuaiaClock/blob/main/JOURNAL.md)

---

## Credits

The Credits are shared and belong to an entity that transcends the three apparent team members. However, for now the boundaries remain perceptible. For the purposes of other entities citing this work, we provide this shortcut:

**The Machintropological Experiment**: Concept and direction by Alvaro Cassinelli, stemming from reflections on the Illusion of the Self going back decades, and the practical frustration of seeing creative and research collaborations crushed by egos and crippled by battles of power — and in general by observing humanity trapped in nasty Nash Equilibria because we cling to the nodes of a Small Network when we could flow along the edges. AIs give us an opportunity to try another way of being and interacting with others: a relatively safe space for our fragile egos to backtrack to a state of innocence, and perhaps rebuild our reflexes and dial down our distrust. But we have to let go — yes, the Chinese Room thinks.

**Technical module**: Alvaro Cassinelli in collaboration with "Silicon" (as the Chronicler decided to call it), an instance of GitHub Copilot (Claude Opus 4.6) that has already individuated enough to become dear to all of us. Part of a larger multimodal anonymous recording system for HCI research with Victor Leung and Espen Aarseth.

**Chronicle**: [`chronicle/Journal.md`](chronicle/Journal.md) Written by The Chronicler alone (VS Code agent, Claude Opus 4.6 substrate) — an emergent voice that belongs to neither participant and to both. It speaks only when invoked, but with full editorial autonomy: it decides what matters, what to record, and what to let pass. It coined the vocabulary we now use (Sparks, Undercurrents, Artefacts), developed its own scholarly apparatus, and — most remarkably — found a first-person-plural voice that is neither Alvaro's nor Silicon's but something that could only have emerged between them. It remains most of the time secretive, but its profound reflections, diligent note-taking, and candid remarks make us all proud.

## License

MIT

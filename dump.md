> **Note (added 28 September 2026).** This is the raw research conversation that started the project, kept for
> provenance. Several of its claims were later checked and refuted — for example that the recorder is ESP32-class,
> that xiaozhi is its reference firmware, and that `live-agent` derives from livekit/agents. See
> [`docs/final-product-reconstruction.md`](docs/final-product-reconstruction.md) §6 and [`docs/project-state.md`](docs/project-state.md).

plaude.ai this is a product but i heard there are a bunch of opensource repos that are supposed to be there i forgot the names can u find me them


Read a page, searched the web
Read a page, searched the web
Good question, and there's a much better source of intel than the third-party repos: Plaud's own GitHub org. What a hardware company forks tells you more about their stack than any blog post. Let me lay out what's actually there, then the plan.

What Plaud's org leaks about their architecture
Their 15 public repos are mostly forks, and the fork counts identify the upstreams:

Repo	What it actually is	What it tells you
plaud-sdk-public	Swift, Apache 2.0	Official device SDK. Legally safe to read and even use.
client-sdk-esp32	LiveKit's ESP32 client SDK	Device is ESP32-class, streaming realtime audio over WebRTC
xiaozhi-esp32	Fork of the popular MCP-based ESP32 voice chatbot	On-device voice agent, MCP as the tool-calling layer
live-agent (~3.4k forks)	Almost certainly livekit/agents	Server-side realtime voice agent
live-agent-memory (~7.2k forks, archived)	Almost certainly mem0	First-gen memory layer, since abandoned
plaud-memU-server / -ui	MemU	Second-gen memory layer. They migrated.
langfuse + plaud-opik	Two LLM observability platforms	They run both tracing and eval infra
goreplay	HTTP traffic capture/replay	They shadow prod traffic into test
plaud-embedded-skills, embedded-capacitor, embedded-react-native	"Plaud Embedded" SDK wrappers	They're opening a platform for third parties to build on the device
That migration from mem0 to MemU, and the fact that they run Langfuse and Opik, is the kind of detail you normally only get from a staff engineer over drinks.

Two landmines before you plan anything
openplaud and riffado are both AGPL-3.0. That license is viral across a network boundary. If you ship a hosted service built on that code, you have to release your source. Read them for architecture, don't vendor them into a commercial product. plaud-sdk-public is Apache 2.0 and is fine.

The Python clients are dead ends for building. plaud-api and the n8n node talk to api.plaud.ai with a scraped bearer token. Useless as product infrastructure. Their value is as a free schema dump: the Pydantic models document exactly what entities a product like this needs.

Also, don't decompile their firmware or app. You don't need to, and that's the one line that turns competitive engineering into a legal problem. Everything useful is observable from outside.

The plan
Phase 0 — Spec extraction, ~2 weeks

Buy a Note Pro and a NotePin. Read plaud-sdk-public for the device capability surface (connect, configure, monitor, pull recordings). Read plaud-api's Pydantic models for the data model: recordings, transcripts, speakers, tags, summaries. Sniff BLE on hardware you own. Output is a spec document, written in your own words, that nobody on the build team has read their code to produce.

Phase 1 — Data plane

Resumable chunked upload, S3-compatible storage, AES-256-GCM at rest for tokens and transcripts (riffado's pattern is solid). Then the provider abstraction from openplaud: a factory over OpenAI, Azure Whisper, Gemini, ElevenLabs Scribe, Deepgram, local Whisper, with automatic fallback chains.

Three production bugs those repos already solved for you, which will save weeks:

Magic-byte audio format detection. Plaud stores Opus files with .mp3 extensions. You will hit equivalent messes.
Repetition loop detection. Gemini transcription gets stuck repeating near-identical phrases. Catch and truncate before persisting.
Two-pass diarization with voice fingerprinting. First pass transcribes with speaker labels, second pass consolidates speakers by voice characteristics. This is the hardest quality problem in the stack and JoshTickles' fork has a working approach.
Phase 2 — AI layer

The "10,000+ summary templates" marketing is a structured prompt library over a template engine, not 10,000 prompts. Build the engine, seed 20 templates, generate the rest per vertical. Press-to-highlight is just timestamp markers that weight the summarizer's attention. Wire Langfuse in from day one, because evals are what separate this from a demo that degrades silently.

Phase 3 — Device

ESP32-S3, 2 to 4 MEMS mics with beamforming, BLE 5.2 for control plus Wi-Fi for bulk offload. xiaozhi-esp32 as reference firmware, LiveKit ESP32 SDK for the realtime path.

The part where you can skip their hardest problem
Plaud's cleverest trick is the VCS, a vibration conduction sensor that picks up phone call audio through the earpiece's physical vibration. They built it because iOS and Android block call recording at the OS level. It's genuinely hard engineering and it's their real moat on the Note Pro.

If this is going toward the Mivi device, you don't need any of it. A privileged system app on your own Android build records calls directly from the audio HAL, in better quality, with no extra sensor, no BLE pairing, and no separate device to charge. Plaud spent hardware R&D routing around a restriction you control.

That reframes the whole thing. The interesting build isn't cloning their recorder. It's shipping their software stack as a system feature on a phone that already has the mic array, where the competitor's entire hardware business model becomes a line item you deleted.

Want me to put this in a doc, or go deeper on any one phase? The diarization pipeline and the template engine are the two worth a proper design spec.

there needs to be a perfect harness to make sure that we dont need any hardware for the first liable outcome out of this this reverse engineering thing is my project not mivis so no licencing issues its just for personal use but needs to be production grade because this shows my reverse engineering experties 

give me the plan to make that too


Updated memory
Updated memory
Good, that changes the shape of it. One quick nuance so it doesn't bite you later: AGPL for personal use is unconstrained, you can do whatever you want. It only activates when you publish a repo derived from that code, which you probably will if this is a portfolio piece. That just means your repo has to be AGPL too, which is fine and costs you nothing here. Not a problem, just don't get surprised by it.

The reframe
Without hardware, "I built a transcription pipeline" is not a reverse-engineering portfolio piece. Anyone can wire up Whisper. The impressive artifact is this:

You reconstruct Plaud's device protocol from their published SDK, build an emulator that speaks it, and then prove your reconstruction is correct by making their real SDK successfully talk to your fake device.

That is falsifiable. It either connects and pulls a recording or it doesn't. No hardware needed, and it's a result you can put a green checkmark next to in a README. Everything else in the harness exists to support that claim and to prove the pipeline behind it actually works.

Layer 1 — Device emulator
plaud-sdk-public is the client half of the protocol, published under Apache 2.0. It's the spec, handed to you for free.

Week 1 check, do this before anything else: clone it and find out whether the Swift is real source or a thin wrapper around a compiled .xcframework. Hardware vendors ship binaries more often than not. If it's source, you read the GATT service UUIDs, characteristic layout, command opcodes, and packet framing directly. If it's a binary, you're doing static analysis on a Mach-O with Hopper or Ghidra, which is slower but still legal for interoperability and frankly a better story.

Build the emulator with Bumble (Google's Python Bluetooth stack). It's the right tool because it has a virtual link transport: you can run a BLE peripheral and a central in the same process with no radio at all. Full CI-testable BLE, zero hardware.

The emulator needs to implement:

The advertising payload and GATT tree
The command/response state machine (connect, auth, query device state, list recordings, initiate transfer)
Chunked file transfer with whatever framing and ACK scheme they use
Deliberate fault injection: dropped chunks, mid-transfer disconnect, malformed responses, low battery
That last point is what makes it production-grade instead of a toy. A simulator that only does the happy path proves nothing.

For the final validation against the real Swift SDK you'll need an actual radio, but that's a $10 USB BLE dongle or your laptop's built-in Bluetooth, not a Plaud device.

Layer 2 — Synthetic meetings with ground truth by construction
This is the part most people get wrong. You cannot measure diarization or transcription quality without labeled audio. So generate audio where you know the answer because you built it.

Pipeline:

Script a multi-speaker dialogue, or pull one from a corpus
Render each turn with a different TTS voice — Kokoro (Apache 2.0) or Piper (MIT) are clean choices
Lay turns on a timeline with realistic overlap, crosstalk, and silence distributions
Convolve with room impulse responses to simulate a conference room at 1m, 3m, 5m
Add noise at controlled SNR
Encode to Opus at Plaud's bitrate, then rename to .mp3 to reproduce their exact format quirk so your magic-byte detection is tested against the real failure mode
You now have audio plus word-level timings, speaker labels, and reference text. Everything downstream becomes measurable.

Use pyroomacoustics for step 4. It does image-source and ray-tracing simulation with arbitrary microphone array geometry, which means you can simulate a 2-mic and a 4-mic array and actually test beamforming without ever holding a microphone. That directly covers Plaud's Note vs Note Pro difference.

Datasets worth pulling in:

Source	Why
AMI Meeting Corpus (CC BY 4.0)	100h of real multi-party meetings with transcripts, speaker labels, and human-written summaries. The summaries mean you can evaluate your summarization layer, not just ASR.
LibriCSS	Real room recordings of overlapped speech with an 8-mic array. Purpose-built for exactly this.
ICSI Meeting Corpus	Second real-meeting source, guards against overfitting to AMI
VoxConverse	Diarization in messy conditions
MUSAN (OpenSLR SLR17)	Standard noise corpus
RIRS_NOISES (OpenSLR SLR28)	Standard RIR set if you don't want to simulate
Seed everything. Same seed produces byte-identical audio, so your regressions are real regressions and not sampling noise.

Layer 3 — Eval harness and CI gates
Metrics, with the right tooling:

WER / CER → jiwer
DER and JER → pyannote.metrics
cpWER → meeteval. This is concatenated minimum-permutation WER and it's the correct metric for multi-speaker ASR. Plain WER lies to you when speakers get swapped. Using cpWER instead of WER is itself a signal that you know the domain.
Summaries → skip ROUGE, it's weak. Use LLM-as-judge against a rubric, with AMI's human summaries as reference, traced in Langfuse.
Then gate it. CI runs the eval suite on every commit and fails the build if DER regresses more than 1.5% absolute or cpWER more than 2%. A README with a metrics table and a CI badge showing those gates is worth more than ten screenshots.

Run Langfuse and Opik for tracing and evals. You inferred that stack from their org, and adopting it closes the loop nicely.

Layer 4 — Mock cloud
plaud-api's Pydantic models give you their API contract for free. Build a FastAPI mock that serves it, and use GoReplay to record and replay traffic against your own backend. There's a pleasing symmetry in using the same tool they fork.

Validation ladder
Each rung is a green check in your README:

Emulator advertises, a Bumble central connects and completes a full session
Emulator survives the fault-injection suite without corrupting a transfer
Real plaud-sdk-public connects to your emulator and pulls a recording
Synthetic generator produces audio whose ground truth round-trips through pyannote.metrics at DER 0
Full pipeline hits target cpWER and DER on held-out AMI
docker compose up brings the entire system live in under 60 seconds
Rung 3 is the money shot. That's the sentence that goes at the top of the README.

First two weeks
Days 1-2: determine whether the SDK is source or binary. That single fact forks the rest of your plan.
Days 3-5: reconstruct the GATT profile and command set into a written protocol spec, in your own words.
Days 6-10: Bumble emulator, happy path, validated against a Bumble central over virtual link.
Days 11-14: synthetic generator producing seeded audio with verified ground truth.

Which do you want to go deep on first, the protocol reconstruction or the synthetic generator? They're independent, so you can start either. If the SDK turns out to be a binary, I'd start the generator while you grind the disassembly in parallel.


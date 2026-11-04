# Audigest — North Star

## What Audigest is

Audigest is a small, local-first, CLI-first tool that turns spoken or already-transcribed content into a reusable canonical transcript and focused AI outputs.

The core pipeline is:

```text
source
  ↓
best available transcript
  ↓
canonical transcript
  ↓
processors
```

Audigest is built to be useful both directly from the terminal and as a composable tool inside AI agents.

---

## Core principle

**Do not transcribe audio unless transcription is actually necessary.**

Transcript acquisition should prefer the cheapest and simplest reliable source:

```text
existing supplied transcript
        ↓
publisher-provided transcript
        ↓
captions / subtitles
        ↓
speech-to-text
```

Speech-to-text is a fallback, not the product.

---

## Primary use cases

### 1. Learn from content and apply it to projects

Example:

```bash
audigest ingest <source>
audigest summarize <ref>
audigest apply <ref> --project ~/projects/my-app
```

The output should help answer:

- What are the important ideas?
- Which ideas are relevant to this project?
- Why are they relevant?
- What could be tried or changed?

The goal is not generic summarization.

The goal is **turning consumed content into useful action or insight**.

---

### 2. Learn English from real content

Example:

```bash
audigest english <ref> --level B2
```

Audigest should identify high-value:

- vocabulary;
- natural phrases;
- collocations;
- phrasal verbs;
- idioms;
- conversational constructions;
- useful grammar patterns.

Prefer useful B2→C1 material over exhaustive word extraction.

Whenever possible preserve:

- original sentence/context;
- timestamp;
- meaning;
- usage note;
- examples.

---

## Product invariants

These should remain true as the project evolves.

### CLI-first

Every important feature must be available from the command line.

The CLI must work well for both humans and agents.

---

### Canonical transcript

All transcript sources normalize into one internal representation.

Processors must not care whether the text came from:

- a supplied text file;
- YouTube captions;
- publisher transcript;
- OpenAI;
- ElevenLabs;
- local Whisper;
- another future provider.

---

### Transcription and analysis are separate

A source should normally be acquired/transcribed once.

The resulting transcript can then be processed many times without repeating STT.

---

### Timestamps are optional

A valid transcript does not require timestamps.

Segments may contain:

```text
text
start?
end?
speaker?
```

Processors must work with transcripts both with and without timestamps.

---

### Provider-independent

STT and LLM providers are adapters.

Provider-specific models and response objects must not leak into the core domain model.

---

### BYO credentials

Users provide their own API credentials.

Secrets must never be:

- committed;
- included in generated transcripts;
- logged;
- embedded in cached artifacts.

---

### Local-first

Audigest should work without a hosted Audigest backend.

Transcripts and processor outputs are stored locally by default.

---

### Agent-friendly

Machine-readable output must be a first-class feature.

For JSON mode:

```text
stdout = valid JSON only
stderr = progress/logging/errors
```

Commands should be deterministic enough to be called reliably by agents.

---

## Main CLI model

The preferred high-level entry point is:

```bash
audigest ingest <source>
```

`ingest` means:

> Obtain the best available transcript for this source and normalize it.

Possible sources:

```text
text / markdown transcript
local audio/video
direct media URL
YouTube URL
podcast/RSS
Spotify episode URL
```

Explicit STT remains available:

```bash
audigest transcribe <media> --provider openai
```

But users should not normally need to choose STT when a transcript already exists.

---

## Processors

The initial processor set is deliberately small:

```bash
audigest summarize <ref>
audigest apply <ref> ...
audigest english <ref> ...
```

Processors consume canonical transcripts only.

They should never independently download or retranscribe the source.

---

## What Audigest is NOT

Audigest is not:

- a RAG system;
- a vector database;
- a second brain;
- a podcast player;
- a web application;
- a hosted SaaS;
- an agent framework;
- an MCP server;
- a general-purpose scraping framework.

These may become integrations later, but they must not distort the core tool.

---

## Architecture boundary

Conceptually:

```text
               ┌────────────────────┐
               │       Source       │
               └─────────┬──────────┘
                         │
               ┌─────────▼──────────┐
               │ Transcript Acquire │
               │                    │
               │ text               │
               │ captions           │
               │ publisher          │
               │ STT fallback       │
               └─────────┬──────────┘
                         │
               ┌─────────▼──────────┐
               │ Canonical Transcript│
               └─────────┬──────────┘
                         │
          ┌──────────────┼──────────────┐
          │              │              │
       summarize        apply         english
```

Keep these layers separate.

---

## MVP acceptance criteria

An MVP can be considered successful when all of the following work reliably.

### Acquisition

```bash
audigest ingest transcript.md
audigest ingest ./audio.mp3
audigest ingest <youtube-url>
```

For YouTube:

```text
captions available
→ use captions

captions unavailable
→ STT fallback
```

---

### Providers

At least:

```text
OpenAI STT
ElevenLabs STT
local Whisper/faster-whisper
```

Provider selection can be explicit or automatic.

---

### Canonical transcript

Every ingestion route produces the same versioned transcript model.

It supports:

```text
text
source metadata
language
segments
optional timestamps
optional speakers
origin
provider metadata where relevant
```

---

### Reuse

After ingestion:

```bash
audigest summarize <id>
audigest apply <id> --project ...
audigest english <id> --level B2
```

must reuse the stored transcript and must not run STT again.

---

### Caching

Repeated ingestion of the same unchanged source should normally reuse cached work.

Local file changes must invalidate the relevant cache.

---

### Output

Human-readable Markdown output works.

Machine-readable JSON output works.

JSON stdout contains no decorative logs.

---

### Safety

No API keys or private media are committed or leaked.

Downloaded temporary media is deleted by default.

No Spotify stream ripping.

---

## Decision test

When considering a new feature, ask:

> Does this make it easier to acquire a useful transcript, reuse it, or turn it into a focused output?

If not, it probably does not belong in the core of Audigest.

---

## Success metric

The practical North Star is simple:

> **I encounter useful spoken content and can turn it into reusable knowledge or learning material with one CLI workflow, without paying or computing for work that has already been done.**
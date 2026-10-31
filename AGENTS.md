# Contributor and agent guidance

Read [SAFETY.md](SAFETY.md) before changing code, adding fixtures, or handling media.

## Required product scope check

Before every product implementation, locate and read the current [NORTH_STAR.md](NORTH_STAR.md) in full, even if it was read in an earlier task. Map the proposed behavior to the relevant North Star section, current milestone, and user's request; implement only the authorized work needed for that scope. Newer explicit user instructions and corrections take precedence, including the strategy-first transcript/caption acquisition, STT-only fallback, text ingestion, and optional timestamps.

The North Star's product direction is a high-level `audigest ingest <source>` command that obtains the best available transcript; `transcribe` is the explicit STT path and fallback. The target acquisition priority is supplied text/Markdown transcript, publisher-provided transcript, captions/subtitles, then STT. Durable canonical transcripts must retain origin/provenance, including source and provider details when relevant. These are product priorities, not blanket authorization to expand a task. In this repository, strategy-first behavior is available through `ingest` and its `transcribe` compatibility alias; direct `.md` input, optional canonical `origin` metadata, and ID-based `show` are implemented. Remote acquisition and explicit STT-only behavior remain unimplemented and are sequenced in [ROADMAP.md](ROADMAP.md). The user's 2026-10-31 authorization permits staged work on the listed remaining North Star MVP items, subject to parent review selecting the next iteration; it does not expand the current task to every roadmap item.

Identify and justify any material deviation before coding. Ask the user for direction only when the product scope conflicts or the work would expand beyond what they authorized. If `NORTH_STAR.md` is missing or unreadable, search the repository and locations the user named; do not invent its contents, create a placeholder, or substitute an older handoff. Ask the user for its path and pause product implementation; updating guidance itself may continue.

## Authorized roadmap execution

The user authorized the remaining North Star MVP work to proceed sequentially through [ROADMAP.md](ROADMAP.md), superseding earlier blanket deferrals only for roadmap items. Work on one selected iteration at a time; the parent agent reviews the current result and defines the next implementation scope before coding continues. Luna xhigh implements; the parent agent reviews. After each completed iteration is review-approved, commit and push the explicitly selected task files to `main` under the user's 2026-10-31 authorization. Never force-push or overwrite remote commits. If `main` has diverged, stop and report before merge or rebase.

## Product and current session scope

Audigest is a small, local-first, CLI tool built around the flow **local source in → existing transcript acquisition → canonical transcript → task-specific output**. Prefer usable text already present next to or in the source; speech-to-text (STT) is a fallback. Keep the core explicit and provider-independent: acquisition strategies produce the canonical transcript model, and later tools consume that model.

The current milestone covers Milestone 0 and a local, strategy-first transcript vertical slice: direct canonical transcript JSON, UTF-8 `.txt` and `.md`, `.srt`, and `.vtt` input; deterministic adjacent transcript discovery for local media; optional origin metadata in canonical transcripts; valid cache reuse; ID-based `show` reads from the existing local transcript store; and STT only when no usable transcript source exists. `show <transcript-id>` renders the stored canonical transcript as Markdown or JSON without resolving the source, acquiring a transcript, selecting STT, or changing cache artifacts. Markdown is direct input only and does not change the adjacent sidecar order. The fake provider (`--provider fake`) remains a deterministic offline fallback, never the first choice. Explicit `--provider openai` enables the OpenAI STT fallback after acquisition and cache lookup; it is the only current runtime network path. Provider selection must not override an existing transcript or caption. `auto` reuses a matching valid cached transcript and otherwise fails usefully without selecting a provider, regardless of whether `OPENAI_API_KEY` is set.

The local resolver accepts canonical `.json`, UTF-8 `.txt` and `.md`, `.srt`, `.vtt`, and supported media files. For media such as `episode.mp3`, check adjacent candidates in this exact order: `episode.transcript.json`, `episode.vtt`, `episode.srt`, `episode.txt`. A malformed existing candidate must produce a clear source error; do not silently skip it and invoke STT. Only use STT after direct transcript/text/caption inputs, applicable adjacent candidates, and a matching valid cache have been considered. Do not add a force-STT option in this scope.

Transcript segments may be timed or untimed. For each segment, `start` and `end` are either both absent/`null` or both present and valid. Renderers must omit timestamps when they are absent and never invent timing information.

The remaining North Star MVP features explicitly listed in `ROADMAP.md` are authorized for future, individually reviewed iterations; they are not part of the current implementation scope until selected. OpenAI is the current explicit STT fallback after transcript acquisition has been attempted. Keep RAG, embeddings, databases, GUI, background jobs, telemetry, and features outside the roadmap deferred. Do not create placeholder roadmap modules.

## Initial structure

Keep modules small and interfaces explicit. The proposed starting layout is:

```text
AGENTS.md
SAFETY.md
NORTH_STAR.md
README.md
LICENSE
.gitignore
pyproject.toml
uv.lock
.github/workflows/ci.yml
src/audigest/
  __init__.py
  cli.py
  config.py
  models.py
  storage.py
  errors.py
  service.py
  acquisition.py  # direct and adjacent local transcript/text/caption parsing
  sources/
    base.py
    detect.py
    local.py  # local media resolver
  stt/
    base.py
    router.py
    fake.py
    openai.py  # optional, explicitly selected remote STT fallback
  renderers/
    json.py
    markdown.py
tests/
  fixtures/
  unit/
  integration/
```

`service.py` orchestrates acquisition and fallback. Acquisition detects and prepares direct input or prioritized adjacent sidecars first; then it may reuse a matching cache pointer keyed by source URI, effective fingerprint (including chosen sidecar bytes), and requested language; then it returns the prepared result; only then may it call STT. Keep the latest pointer for each such key in `refs/<hash>.json`. `--no-cache` bypasses cache lookup, not supplied or adjacent transcript/text/caption acquisition, and the acquired result is still stored. A matching valid cached transcript may be reused regardless of the selected fallback provider. The `--provider` option selects only STT fallback and cannot override a usable transcript.

When importing canonical transcript JSON, preserve its embedded source metadata, provider, model, language, segments, speakers, timestamps, and `origin` when present. If the imported artifact has no origin, identify the canonical JSON file as the origin. Assign a new cache artifact ID and set `created_at` to the ingestion time. The optional origin field remains compatible with schema-version-1 artifacts that predate the field; legacy cached transcripts without it stay without inferred origin.

Local media, text, captions, and transcript files are user originals: read them in place; never delete, modify, or copy them. Any future generated or downloaded media is a separate case: use temporary storage by default and retain it only with an explicit `--keep-media` option. Never persist secrets in cache or transcript artifacts.

Canonical segment `start` and `end` fields default to `None`; they must either both be absent/`null` or both be present with valid timestamps. Untimed text remains untimed through storage and rendering.

## CLI output contract

- Human mode: progress and status messages go to stderr; the useful final result goes to stdout.
- `show` human mode writes Markdown rendered from the stored canonical transcript to stdout.
- JSON mode: stdout contains valid JSON only. Progress, diagnostics, and errors go to stderr; never print decorative output into JSON stdout.
- Failures use non-zero exit codes, with stable categories as the CLI grows: `2` invalid arguments/configuration/storage or invalid transcript ID, `3` source resolution or a transcript ID missing from the cache, `4` media/captions retrieval, `5` STT provider, `6` processor, and `7` missing local dependency. The current local transcript-first scope should use the applicable categories and provide actionable messages.

## Validation commands

Run the applicable project checks before reporting implementation work complete:

```bash
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

Keep tests offline and deterministic; dependency installation may use the network, but tests must not make network calls, call paid APIs, or require credentials. Add tests with each behavior. Do not introduce unrelated features or change the project license without an explicit task.

## Task cleanup

After every task, delete all temporary scripts and helper files created solely to complete that task, including one-off Python/shell/JS scripts, probes, scratch files, and temporary validation copies. Clean them up locally and on remote hosts, including after failures, and verify their removal before reporting completion. Preserve requested deliverables, implementation code, installed integrations, and pre-existing user files. Do not leave new temporary scripts behind while cleaning up earlier ones.

# Contributor and agent guidance

Read [SAFETY.md](SAFETY.md) before changing code, adding fixtures, or handling media.

## Required product scope check

Before every product implementation, locate and read the current [NORTH_STAR.md](NORTH_STAR.md) in full, even if it was read in an earlier task. Map the proposed behavior to the relevant North Star section, current milestone, and user's request; implement only the authorized work needed for that scope. Newer explicit user instructions and corrections take precedence, including the strategy-first transcript/caption acquisition, STT-only fallback, text ingestion, and optional timestamps.

The North Star's product direction is a high-level `audigest ingest <source>` command that obtains the best available transcript; `transcribe` is the explicit STT-only path. The target acquisition priority is supplied text/Markdown transcript, publisher-provided transcript, captions/subtitles, then STT. Durable canonical transcripts must retain origin/provenance, including source and provider details when relevant. These are product priorities, not blanket authorization to expand a task. In this repository, strategy-first `ingest`, explicit local-media-only `transcribe`, direct `.md` input, optional canonical `origin` metadata, ID-based `show`, bounded direct HTTP(S) transcript/caption URL ingestion, explicit supplied transcripts for media, and direct HTTP(S) media ingestion are implemented. Automatic publisher discovery, RSS, and video-platform caption acquisition remain unimplemented and are sequenced in [ROADMAP.md](ROADMAP.md). The user's 2026-10-31 authorization permits staged work on the listed remaining North Star MVP items, subject to parent review selecting the next iteration; it does not expand the current task to every roadmap item.

Identify and justify any material deviation before coding. Ask the user for direction only when the product scope conflicts or the work would expand beyond what they authorized. If `NORTH_STAR.md` is missing or unreadable, search the repository and locations the user named; do not invent its contents, create a placeholder, or substitute an older handoff. Ask the user for its path and pause product implementation; updating guidance itself may continue.

## Authorized roadmap execution

The user authorized the remaining North Star MVP work to proceed sequentially through [ROADMAP.md](ROADMAP.md), superseding earlier blanket deferrals only for roadmap items. Work on one selected iteration at a time; the parent agent reviews the current result and defines the next implementation scope before coding continues. Luna xhigh implements; the parent agent reviews. After each completed iteration is review-approved, commit and push the explicitly selected task files to `main` under the user's 2026-10-31 authorization. Never force-push or overwrite remote commits. If `main` has diverged, stop and report before merge or rebase.

## Product and current session scope

Audigest is a small, local-first, CLI tool built around the flow **local source in → existing transcript acquisition → canonical transcript → task-specific output**. Prefer usable text already present next to or in the source; speech-to-text (STT) is a fallback. Keep the core explicit and provider-independent: acquisition strategies produce the canonical transcript model, and later tools consume that model.

The current milestone covers Milestone 0 and the transcript vertical slice: direct local canonical JSON, UTF-8 `.txt` and `.md`, `.srt`, and `.vtt` input; deterministic adjacent transcript discovery for `ingest`; optional origin metadata; ID-based `show`; explicit STT-only `transcribe` for supported local media; direct HTTP(S) transcript/caption URLs; direct HTTP(S) media URLs; and `ingest --transcript` for local or direct remote transcripts. A supplied transcript takes priority over sidecars, media cache, and STT. When used with remote media, it avoids downloading the media. Direct remote transcript/caption GETs are bounded to 2 MiB; remote media is streamed to a temporary file capped at 100 MiB. Both use a 15-second socket timeout, a 60-second total deadline, at most five manually validated HTTP(S) redirects, and no URL userinfo. Source/origin URIs omit query and fragment; cache identity includes a hash of the full locator and response bytes. Fetch happens before cache lookup, so changed remote content is detected. Remote media STT origins use the sanitized remote media URI, never the temporary path. Downloaded media is removed by default; `--keep-media` saves it under `<cache>/media/` after successful processing only. This slice does not discover publisher transcripts, feeds, or video-platform captions. `ingest` is strategy-first and uses STT only when no usable transcript source exists. `transcribe` does not inspect direct transcript/caption files or sidecars and uses the raw local media fingerprint; acquired-transcript cache fingerprints remain distinct, while genuine STT results can be reused by both commands. `show <transcript-id>` renders the stored canonical transcript as Markdown or JSON without resolving the source, acquiring a transcript, selecting STT, or changing cache artifacts. Markdown is direct input only and does not change the adjacent sidecar order. The fake provider (`--provider fake`) remains a deterministic offline fallback. Explicit `--provider openai` enables the OpenAI STT path: a fallback for `ingest` and an explicit provider for `transcribe`. The runtime network paths are direct transcript/media GETs and explicitly selected OpenAI STT. `auto` reuses a matching valid cache and otherwise fails without selecting an STT provider, regardless of whether `OPENAI_API_KEY` is set.

The local resolver accepts canonical `.json`, UTF-8 `.txt` and `.md`, `.srt`, `.vtt`, and supported media files. The remote adapter accepts explicitly supplied HTTP(S) transcript/caption URLs (`.json`, `.txt`, `.md`, `.srt`, `.vtt`) and media URLs (supported audio/video suffixes). Reject URL userinfo and non-HTTP(S) redirect targets, do not add authentication/cookies, and never include request exception text or query/fragment values in diagnostics/artifacts. Bound transcript bodies to 2 MiB and media to 100 MiB; requests have 15-second socket timeouts, a 60-second total deadline, and at most five manually followed redirects. A supplied transcript must be a local transcript file or direct transcript URL and is valid only with local or remote media. It precedes local sidecars, cache lookup, and STT; malformed or missing supplied input fails without fallback. For local `ingest` media such as `episode.mp3`, check adjacent candidates only when `--transcript` is absent, in this exact order: `episode.transcript.json`, `episode.vtt`, `episode.srt`, `episode.txt`. A malformed selected candidate must produce a clear source error; do not silently skip it and invoke STT. `transcribe` accepts supported local media only, rejects transcript/text/caption files and remote URLs with an actionable `ingest` suggestion, and ignores all adjacent sidecars, including malformed ones. Do not add a force-STT option to `ingest`.

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
    remote.py  # bounded direct transcript/caption and media URL retrieval
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

`service.py` shares source fingerprinting, cache lookup, STT routing, and artifact storage between `ingest` and `transcribe`. `ingest` prepares direct local input, fetches a direct transcript URL, uses an explicit supplied transcript, prepares prioritized local sidecars, or streams remote media before cache lookup. A supplied transcript cache identity combines media identity, transcript URI/content identity, and requested language; remote media identities hash the full locator and downloaded bytes. Remote inputs are fetched before lookup. `transcribe` accepts local media only, skips acquisition, and looks up the raw media fingerprint before STT. Keep the latest pointer for each sanitized source URI, effective fingerprint, and requested language in `refs/<hash>.json`; this separates acquired transcript/caption results from STT results while allowing genuine STT work to be reused by either command. `--no-cache` bypasses lookup and stores the newly acquired or transcribed result. The `--provider` option selects the STT fallback for `ingest` and the explicit STT provider for `transcribe`; `auto` remains cache-only for STT.

When importing canonical transcript JSON, preserve its embedded source metadata, provider, model, language, segments, speakers, timestamps, and `origin` when present. If the imported artifact has no origin, identify the canonical JSON file as the origin. Assign a new cache artifact ID and set `created_at` to the ingestion time. The optional origin field remains compatible with schema-version-1 artifacts that predate the field; legacy cached transcripts without it stay without inferred origin.

Local media, text, captions, and transcript files are user originals: read them in place; never delete, modify, or copy them. Downloaded remote media is streamed to temporary storage and deleted after processing by default. With `--keep-media`, retain successfully processed downloads under the configured cache's `media/` directory using a hashed name; do not overwrite an existing file or follow an existing symlink. Never persist secrets in cache or transcript artifacts.

Canonical segment `start` and `end` fields default to `None`; they must either both be absent/`null` or both be present with valid timestamps. Untimed text remains untimed through storage and rendering.

## CLI output contract

- Human mode: progress and status messages go to stderr; the useful final result goes to stdout.
- `show` human mode writes Markdown rendered from the stored canonical transcript to stdout.
- JSON mode: stdout contains valid JSON only. Progress, diagnostics, and errors go to stderr; never print decorative output into JSON stdout.
- Failures use non-zero exit codes, with stable categories as the CLI grows: `2` invalid arguments/configuration/storage or invalid transcript ID, `3` source resolution or a transcript ID missing from the cache, `4` remote transcript/caption/media retrieval, `5` STT provider, `6` processor, and `7` missing local dependency. Provide actionable messages without exposing full query-bearing URLs or exception text.

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

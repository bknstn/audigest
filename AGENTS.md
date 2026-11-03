# Contributor and agent guidance

Read [SAFETY.md](SAFETY.md) before changing code, adding fixtures, or handling media.

## Required product scope check

Before every product implementation, locate and read the current [NORTH_STAR.md](NORTH_STAR.md) in full, even if it was read in an earlier task. Map the proposed behavior to the relevant North Star section, current milestone, and user's request; implement only the authorized work needed for that scope. Newer explicit user instructions and corrections take precedence, including the strategy-first transcript/caption acquisition, STT-only fallback, text ingestion, and optional timestamps.

The North Star's product direction is a high-level `audigest ingest <source>` command that obtains the best available transcript; `transcribe` is the explicit STT-only path. The target acquisition priority is supplied text/Markdown transcript, publisher-provided transcript, captions/subtitles, then STT. Durable canonical transcripts must retain origin/provenance, including source and provider details when relevant. These are product priorities, not blanket authorization to expand a task. In this repository, strategy-first `ingest`, explicit local-media-only `transcribe`, direct `.md` input, optional canonical `origin` metadata, ID-based `show`, bounded direct HTTP(S) transcript/caption URL ingestion, explicit supplied transcripts for media, direct HTTP(S) media ingestion, public single-video YouTube caption acquisition, bounded publisher episode-page discovery, RSS 2.0 episode/enclosure ingestion, and explicit Spotify episode-to-RSS mapping are implemented in the current working tree. Iteration 2d3 is pending parent review; see [ROADMAP.md](ROADMAP.md). The user's 2026-10-31 authorization permits staged work on the listed remaining North Star MVP items, subject to parent review selecting the next iteration; it does not expand the current task to every roadmap item.

Identify and justify any material deviation before coding. Ask the user for direction only when the product scope conflicts or the work would expand beyond what they authorized. If `NORTH_STAR.md` is missing or unreadable, search the repository and locations the user named; do not invent its contents, create a placeholder, or substitute an older handoff. Ask the user for its path and pause product implementation; updating guidance itself may continue.

## Authorized roadmap execution

The user authorized the remaining North Star MVP work to proceed sequentially through [ROADMAP.md](ROADMAP.md), superseding earlier blanket deferrals only for roadmap items. Work on one selected iteration at a time; the parent agent reviews the current result and defines the next implementation scope before coding continues. Luna xhigh implements; the parent agent reviews. After each completed iteration is review-approved, commit and push the explicitly selected task files to `main` under the user's 2026-10-31 authorization. Never force-push or overwrite remote commits. If `main` has diverged, stop and report before merge or rebase.

## Product and current session scope

Audigest is a small, local-first, CLI tool built around the flow **local source in → existing transcript acquisition → canonical transcript → task-specific output**. Prefer usable text already present next to or in the source; speech-to-text (STT) is a fallback. Keep the core explicit and provider-independent: acquisition strategies produce the canonical transcript model, and later tools consume that model.

The current milestone covers Milestone 0 and the transcript vertical slice: direct local canonical JSON, UTF-8 `.txt` and `.md`, `.srt`, and `.vtt` input; deterministic adjacent transcript discovery for `ingest`; optional origin metadata; ID-based `show`; explicit STT-only `transcribe` for supported local media; direct HTTP(S) transcript/caption URLs; direct HTTP(S) media URLs; public single-video YouTube watch, `youtu.be`, shorts, and embed URLs; bounded publisher episode pages; bounded RSS 2.0 feeds; public Spotify episode URLs mapped through an explicit RSS feed; and `ingest --transcript` for local, remote, YouTube, publisher-page, selected RSS episode, and Spotify inputs. A supplied transcript takes priority over sidecars, publisher-page requests, media cache, and YouTube/Spotify metadata lookup. For RSS it fetches the feed and selects one episode, but skips that episode's declared transcript and media requests. For Spotify it validates the source and option syntax, then skips oEmbed, feed, and resource requests. Direct remote transcript/caption, publisher-page, RSS feed, and Spotify oEmbed GETs are bounded to 2 MiB; remote media is streamed to a temporary file capped at 100 MiB. These GETs use a 15-second socket timeout, a 60-second total deadline, at most five manually validated HTTP(S) redirects, and no URL userinfo. Publisher discovery is limited to explicitly labeled `.json`/`.txt`/`.md` transcript links, marked inline transcript containers, explicitly labeled `.srt`/`.vtt` links, caption/subtitle tracks, and supported media sources in audio/video elements; no crawling, scripts, arbitrary page text, or external HTML transcript parsing is used. Selection order is text/canonical links, marked inline text, captions/tracks, then direct media STT fallback. RSS accepts only RSS 2.0; a single item is automatic, multiple items require an exact GUID or full item-link `--episode` selector. It reads resource declarations only for the selected item, supports Podcasting JSON 1.0.0 and declared HTML transcript documents, prefers text/canonical transcript declarations over captions and a single supported publisher-hosted media enclosure, and never treats RSS descriptions as transcripts. Extensionless feeds are recognized from one bounded response; Atom and automatic feed discovery remain out of scope. Spotify requires explicit `--feed`; only the bounded public oEmbed title and explicitly selected RSS item are used. The title must match one episode by casefolded, whitespace-normalized exact comparison; `--episode` selects by exact GUID or link and does not bypass the title check. Spotify source identity uses the stable HTTPS episode URL and cache identity hashes the Spotify ID, title, selected RSS metadata, and resource locator/content; queries, fragments, GUIDs, and signed resource values are omitted from artifacts and diagnostics. Podcast JSON start-only times remain untimed. YouTube metadata extraction is run in `yt-dlp` with a 60-second process deadline and 4 MiB output cap; standard error is discarded. It ignores user configuration, plugin directories, cookies, cache directories, remote components, update checks, and geographic bypass. YouTube captions are fetched under the 2 MiB remote-text cap; only direct audio-only formats are eligible for STT fallback, and downloaded audio uses the 100 MiB remote-media cap. Source/origin URIs omit signed query values; YouTube caption/STT identities use stable video and track/metadata fingerprints. Fetch or metadata validation happens before reuse where current text must be checked. YouTube STT cache freshness uses video ID plus title/uploader, duration, and upload/release date metadata; if source media changes without changing those fields, STT may be reused. Remote STT origins use the stable public source URI, never a temporary or signed media URL. Downloaded media is removed by default; `--keep-media` saves it under `<cache>/media/` after successful processing only. `ingest` is strategy-first and uses STT only when no usable transcript source exists. `transcribe` does not inspect direct transcript/caption files or sidecars and uses the raw local media fingerprint; acquired-transcript cache fingerprints remain distinct, while genuine STT results can be reused by both commands. `show <transcript-id>` renders the stored canonical transcript as Markdown or JSON without resolving the source, acquiring a transcript, selecting STT, or changing cache artifacts. `summarize <transcript-id>` consumes only that stored canonical transcript, verifies cached evidence quotes against its segments, and stores separate JSON and Markdown summary outputs; it never accesses the source or invokes STT. Summary generation is cache-only by default and uses OpenAI only when explicitly selected with `--provider openai` or `AUDIGEST_LLM_PROVIDER=openai`. Its model defaults to `gpt-6-luna`, can be set with `--model` or `AUDIGEST_LLM_MODEL`, and its serialized input is capped at 256 KiB. Markdown is direct input only and does not change the adjacent sidecar order. The fake provider (`--provider fake`) remains a deterministic offline fallback. Explicit `--provider openai`, `--provider elevenlabs`, and `--provider local` enable OpenAI, ElevenLabs, and local faster-whisper as STT paths: fallbacks for `ingest` and explicit providers for `transcribe`. Optional SDKs are loaded only after a cache miss and explicit provider selection; OpenAI and ElevenLabs read their respective API keys from the environment. Local Whisper reads `AUDIGEST_WHISPER_MODEL_DIR` only when selected, requires an existing CTranslate2 directory with `model.bin`, `config.json`, and `tokenizer.json`, and loads that directory with `local_files_only=True` on CPU/int8; it never downloads model or tokenizer files. Its transcript metadata records only the directory basename and backend. ElevenLabs uses Scribe v2 with a fixed official endpoint, a 60-second request timeout, and retries disabled. `auto` reuses a matching valid cache and otherwise fails without selecting an STT provider, regardless of whether API keys or a local model directory are available.

The local resolver accepts canonical `.json`, UTF-8 `.txt` and `.md`, `.srt`, `.vtt`, and supported media files. The remote adapter accepts explicitly supplied HTTP(S) transcript/caption URLs (`.json`, `.txt`, `.md`, `.srt`, `.vtt`) and media URLs (supported audio/video suffixes). The YouTube adapter accepts only public single-video watch, `youtu.be`, shorts, and embed URLs with a valid video ID; reject URL userinfo and nonstandard source ports. Reject non-HTTP(S) redirects and userinfo for fetched URLs, do not add authentication/cookies, and never include request exception text, signed track URLs, or query/fragment values in diagnostics or artifacts. Bound transcript bodies to 2 MiB and media to 100 MiB; remote GETs have 15-second socket timeouts, a 60-second total deadline, and at most five manually followed redirects. `yt-dlp` metadata runs without configuration, plugins, cookies, cache directories, remote components, update checks, or geo bypass, under a 60-second process deadline and 4 MiB output cap. Do not install a JavaScript runtime or remote component. A supplied transcript must be a local transcript file or direct transcript URL and is valid only with local or remote media, including YouTube and Spotify URLs; it precedes sidecars, YouTube/Spotify metadata, cache lookup, and STT; malformed or missing supplied input fails without fallback. For local `ingest` media such as `episode.mp3`, check adjacent candidates only when `--transcript` is absent, in this exact order: `episode.transcript.json`, `episode.vtt`, `episode.srt`, `episode.txt`. A malformed selected candidate must produce a clear source error; do not silently skip it and invoke STT. YouTube track selection prefers the requested language, then original metadata language, then a deterministic available language; manual captions win a tie. Malformed or failed selected captions must fail without STT. If no usable captions exist, check STT cache before downloading eligible direct audio-only formats. `transcribe` accepts supported local media only, rejects transcript/text/caption files and remote URLs with an actionable `ingest` suggestion, and ignores all adjacent sidecars, including malformed ones. Do not add a force-STT option to `ingest`.

Transcript segments may be timed or untimed. For each segment, `start` and `end` are either both absent/`null` or both present and valid. Renderers must omit timestamps when they are absent and never invent timing information.

The remaining North Star MVP features explicitly listed in `ROADMAP.md` are authorized for future, individually reviewed iterations; they are not part of the current implementation scope until selected. OpenAI, ElevenLabs, and local Whisper are explicit STT providers after transcript acquisition has been attempted. Keep RAG, embeddings, databases, GUI, background jobs, telemetry, and features outside the roadmap deferred. Do not create placeholder roadmap modules.

`apply <transcript-id> --project <directory>` consumes only the stored canonical transcript and explicitly selected project files; it never resolves the source, invokes acquisition or STT, runs project commands, or modifies project files. If `--context` is omitted, read only `README.md`; otherwise read only the repeated `--context` relative filenames supplied by the user, in sorted order with duplicates removed. Reject unsafe paths, symlinks, non-regular files, empty/invalid UTF-8 files, more than 16 files, more than 64 KiB total, and serialized API input above 256 KiB. `auto` is cache-only; OpenAI requires explicit provider selection and uses the existing `AUDIGEST_LLM_*` settings. Store application JSON and Markdown separately under `outputs/applications/`; artifacts contain only the project basename and selected relative filenames/hashes, never the project root. Keep cache output outside the selected project.

`english <transcript-id> --level <CEFR>` consumes only a stored canonical transcript and requires one of A1, A2, B1, B2, C1, or C2. It never accesses the original source, performs language detection, invokes acquisition, or runs STT. Allow zero to twelve high-value English-learning items, including none for irrelevant or multilingual content. Validate each expression as an exact substring of its exact indexed transcript quote; copy timing and speaker only from that canonical segment. Keep generated examples separate from cited source context. Use the shared `AUDIGEST_LLM_*` provider/model selection; `auto` remains cache-only. Store results under `outputs/english/` without changing canonical transcripts.

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
    youtube.py  # bounded public single-video metadata and track selection
  stt/
    base.py
    router.py
    fake.py
    openai.py  # optional, explicitly selected remote STT fallback
    local.py  # optional faster-whisper adapter for an existing local model directory
  renderers/
    json.py
    markdown.py
tests/
  fixtures/
  unit/
  integration/
```

`service.py` shares source fingerprinting, cache lookup, STT routing, and artifact storage between `ingest` and `transcribe`. `ingest` prepares direct local input, fetches a direct transcript URL, uses an explicit supplied transcript, prepares prioritized local sidecars, acquires YouTube captions, or checks YouTube STT cache before downloading audio. A supplied transcript cache identity combines media identity, transcript URI/content identity, and requested language; direct remote media identities hash the full locator and downloaded bytes. YouTube caption identities combine video ID, selected language/kind/format, and exact caption bytes. YouTube STT identities combine video ID and freshness metadata fields; they deliberately do not include signed audio URL tokens or downloaded audio bytes so a matching STT cache entry avoids audio download. Remote inputs are fetched before lookup when content must be checked. `transcribe` accepts local media only, skips acquisition, and looks up the raw media fingerprint before STT. Keep the latest pointer for each sanitized source URI, effective fingerprint, and requested language in `refs/<hash>.json`; this separates acquired transcript/caption results from STT results while allowing genuine STT work to be reused by either command. `--no-cache` bypasses lookup and stores the newly acquired or transcribed result. The `--provider` option selects the STT fallback for `ingest` and the explicit STT provider for `transcribe`; `auto` remains cache-only for STT.

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

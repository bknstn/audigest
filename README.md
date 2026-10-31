# Audigest

Audigest is designed to turn spoken media into reusable transcripts, then apply focused tools to those transcripts from the command line. Transcript acquisition is strategy-first: reuse local transcripts, captions, or text when available, and use STT only as a fallback.

Transcript and caption acquisition is local-first. Audigest accepts canonical transcript JSON, UTF-8 `.txt` and `.md`, `.srt`, and `.vtt` directly, including explicitly supplied HTTP(S) URLs ending in those formats. `ingest` also accepts direct HTTP(S) media URLs. Markdown is a direct input only. For a local media file, it checks adjacent sidecars in deterministic order: `<basename>.transcript.json`, `<basename>.vtt`, `<basename>.srt`, then `<basename>.txt`. An explicit `--transcript` takes priority over those sidecars. It reuses a valid matching cached transcript before attempting STT. If a selected transcript is malformed, Audigest reports the problem and does not silently skip it to invoke STT. Remote sources are fetched before cache lookup so changed content invalidates the cache.

For `ingest`, provider selection applies only after no usable transcript, caption, text, or matching cache is available. `transcribe` is the separate explicit STT-only command for local media. The deterministic `fake` provider is an offline option for exercising STT; it does not recognize speech or describe audio. Explicitly selecting `openai` enables the OpenAI API for the ingest fallback or explicit transcribe path. `auto` never selects an STT provider, even when `OPENAI_API_KEY` is set. Other providers are not implemented.

## Quick start

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
uv run audigest --help
uv run audigest ingest tests/fixtures/sample.wav --provider fake
uv run audigest transcribe ./episode.mp3 --provider openai
uv run audigest ingest ./episode.mp3 --transcript ./notes.vtt
uv run audigest ingest https://cdn.example/episode.mp3 --provider fake
```

The fixture is a one-second, generated 440 Hz tone, not speech. Its deterministic output is only a smoke-test transcript; it does not represent the sound in the file.

`ingest` is the primary command. Transcript files can be ingested without STT:

```bash
uv run audigest ingest ./episode.transcript.json
uv run audigest ingest ./episode.txt
uv run audigest ingest ./episode.md
uv run audigest ingest ./episode.vtt
uv run audigest ingest ./episode.srt
uv run audigest ingest https://publisher.example/transcript.vtt
```

Direct HTTP(S) transcript ingestion supports `.txt`, `.md`, `.json`, `.srt`, and `.vtt` URLs. Direct HTTP(S) audio/video URLs use `ingest` and the selected STT provider when no transcript is supplied. Both use unauthenticated GETs with a 15-second socket timeout, 60-second total deadline, and at most five validated HTTP(S) redirects. Transcript bodies are limited to 2 MiB; media downloads are streamed to a temporary file and limited to 100 MiB. URL userinfo is rejected. Query and fragment values are omitted from stored source/origin URIs, while a hash of the full locator distinguishes cache entries. Every remote source is downloaded again to validate current content before cache lookup; a matching STT result avoids retranscription. Publisher discovery, feeds, and video-platform captions are not implemented yet.

When a transcript is supplied for a local or remote media source, `ingest --transcript <file-or-url>` parses it before sidecar/cache/STT handling. For remote media, this avoids fetching the audio/video entirely. A malformed or missing supplied transcript fails without STT fallback. A supplied canonical JSON keeps its embedded source and origin metadata.

For explicit STT on local media, use `transcribe` with a selected provider:

```bash
uv run audigest transcribe ./episode.mp3 --provider openai --language en
```

Every successful ingest prints the saved transcript ID. Use it to read the stored transcript later, even if the original source has moved or been removed:

```bash
uv run audigest show <transcript-id>
uv run audigest show <transcript-id> --json
```

`show` reads the existing transcript entry in the configured local cache. It writes Markdown rendered from the stored canonical transcript to stdout by default or the canonical JSON object with `--json`; it does not resolve the original source, run acquisition or STT, or modify cache files. It uses the same cache-root precedence as ingestion, including `AUDIGEST_CACHE_DIR` and the optional TOML `cache_dir` setting.

Text and Markdown produce one untimed segment containing the entire UTF-8 file content. A UTF-8 BOM is treated as an encoding marker. Caption formats preserve their supplied timestamps. Rendering never invents timestamps for untimed text.

Canonical transcripts include optional `origin` metadata alongside `source`. `source` identifies the content represented by the transcript; `origin` identifies where the transcript text was obtained. Its `kind` is `text`, `captions`, `canonical`, or `stt`; `uri` is required and identifies the acquisition input; `format` is optional. When a canonical JSON transcript is imported, its source metadata, provider, model, language, segments, speakers, timestamps, and existing origin are preserved. If it has no origin, the imported JSON file is recorded as the origin. Audigest assigns a new cache artifact ID and sets `created_at` to the ingestion time.

The initial CLI can render machine-readable output with `--json`:

```bash
uv run audigest ingest tests/fixtures/sample.wav --provider fake --json
```

In human mode, progress goes to stderr and the final result goes to stdout. In JSON mode, stdout contains JSON only; diagnostics go to stderr.

For `ingest`, `--no-cache` skips cache-pointer lookup but still reads direct or adjacent transcript/text/caption inputs; the acquired result is still stored. For `transcribe`, `--no-cache` skips matching STT cache lookup and runs the selected STT provider again. Both commands use a raw media fingerprint for STT, while `ingest` transcript/caption acquisitions use a separate effective fingerprint that includes the selected sidecar. As a result, `ingest` can reuse a prior STT fallback when no transcript source exists, but a transcript/caption cache cannot satisfy explicit `transcribe`. Changed media or sidecars cannot reuse stale entries. A valid matching STT cache may be reused regardless of the selected fallback provider. The default cache root is the operating system's user cache location for Audigest; `AUDIGEST_CACHE_DIR` overrides that root. The cache uses this layout:

```text
<audigest-user-cache>/
  refs/<source-key-hash>.json  # latest pointer for the source fingerprint and language
  items/<transcript-id>/
    source.json
    transcript.json
    transcript.md
```

The local-acquisition fingerprint is version `v2` to distinguish artifacts with explicit origin metadata. Existing cache entries for direct or adjacent transcript/text/caption files are rebuilt once from those local files, without STT. Cache identities for media that falls back to STT are unchanged.

The canonical transcript is versioned JSON (`schema_version: "1"`). It contains an `id`; a source record (`type`, `uri`, and optional `title`, `author`, `published_at`, and `duration_seconds`); optional `origin`; `provider`, optional `model` and `language`, and `created_at`; `segments` (`text`, optional `speaker`, and optional `start`/`end`); and full `text` derived from the segments. New readers accept older schema-1 artifacts without `origin`; older strict readers that reject unknown fields may reject new artifacts containing it. Segment timestamps default to `null`; each segment must have both start and end timestamps or neither. Renderers omit timing when it is absent. Provider response objects stay at adapter boundaries.

Local original media is read in place and is never copied or removed. By default, downloaded remote media is deleted after ingestion. `--keep-media` retains successfully downloaded remote media under `<cache>/media/<source-fingerprint>.<extension>` and reports its path; it never applies to local originals or sources that used a supplied transcript without downloading media.

## Command status

`ingest` is transcript-first: a supplied `--transcript` wins first; otherwise it reads direct local transcript/text/caption inputs or fetches an explicitly supplied direct HTTP(S) transcript/caption URL, then checks applicable local sidecars and cache before STT fallback. Direct HTTP(S) media URLs are downloaded only if no transcript is supplied. The media is bounded to 100 MiB and stored temporarily during provider processing; `--keep-media` retains it only after successful ingestion. Automatic publisher discovery, feeds, and video-platform captions are not implemented in this slice.

`transcribe` is an explicit STT-only command for supported local audio/video media. It ignores all transcript sidecars, even malformed ones, and rejects direct JSON, text, Markdown, SRT, and VTT files with a suggestion to use `ingest`. It checks the matching raw-media STT cache before calling the selected provider. Both commands share prior STT work, but acquired transcript/caption cache entries cannot satisfy `transcribe`. This intentionally replaces the previous compatibility-alias behavior; use `ingest` for transcript-first behavior.

`show` accepts a saved transcript ID and the optional `--json` output flag. It renders the stored canonical transcript without using the source file or selecting an STT provider. It does not accept file paths or cache references.

To enable the optional OpenAI SDK and explicitly use its fallback, install the `openai` extra and set an API key in the environment:

```bash
uv sync --all-groups --extra openai
export OPENAI_API_KEY="..."
uv run --extra openai audigest ingest ./episode.mp3 --provider openai --language en
```

The OpenAI provider uses the [`gpt-transcribe` model](https://developers.openai.com/api/docs/guides/speech-to-text). `ingest` sends audio only after local transcript/caption acquisition and cache lookup; `transcribe` sends audio after its matching STT cache lookup. It reads only `OPENAI_API_KEY`, sends requests to the fixed official OpenAI API endpoint, has a 60-second timeout with retries disabled, and never saves the key. This uses the OpenAI API and its account billing; a ChatGPT or Codex subscription is not used or required.

OpenAI accepts `.flac`, `.m4a`, `.mp3`, `.mp4`, `.mpeg`, `.mpga`, `.ogg`, `.wav`, and `.webm` files up to 25 MB. Other local media formats are rejected before upload. Audigest does not convert or split files. It stores the JSON response text as one untimed canonical segment and does not request or infer timestamps. Audigest preserves a single detected language when available and otherwise retains the supplied language hint, if any.

The base installation (`uv sync --all-groups`) remains usable without the optional OpenAI SDK. Install the `openai` extra only when you want to select the OpenAI fallback.

The `fake` provider can exercise the local fallback path, but does not recognize speech or describe audio. Other real STT providers and transcript processors are future work. For example, the following processor commands are not available yet:

```bash
audigest summarize <transcript-id>
audigest apply <transcript-id> --project ~/projects/my-app
audigest english <transcript-id> --level B2
```

Configuration precedence is CLI flags, environment variables, optional TOML config file, then defaults. The config file defaults to the operating system's user configuration location for Audigest (`config.toml`); it accepts top-level `provider`, `language`, and `cache_dir` keys. The current environment variables are:

| Variable | Purpose |
| --- | --- |
| `AUDIGEST_STT_PROVIDER` | Provider for STT; defaults to `auto`, which is cache-only and errors on a cache miss. `ingest` uses it only as fallback; `transcribe` uses it for explicit STT. |
| `AUDIGEST_LANGUAGE` | Optional transcription language. |
| `AUDIGEST_CACHE_DIR` | Override the local cache root. |
| `AUDIGEST_CONFIG_FILE` | Override the TOML config file path. |
| `OPENAI_API_KEY` | Optional OpenAI API credential, used only when the OpenAI provider is selected and a cache miss requires STT. |

Credentials are never accepted in TOML or stored in transcript/cache files. Keep `OPENAI_API_KEY` in the environment; other provider credentials remain future work.

Example TOML configuration (no credentials):

```toml
provider = "auto"
language = "en"
cache_dir = "~/.cache/audigest"
```

## Output and exit codes

The output contract keeps stdout suitable for piping: human-mode progress and diagnostics go to stderr while the final result goes to stdout; `--json` reserves stdout for valid JSON only. Failures use non-zero exit codes:

| Code | Category |
| --- | --- |
| `0` | Success |
| `2` | Invalid arguments/configuration or local storage failure |
| `3` | Source resolution failure, including a transcript ID absent from the local cache |
| `4` | Remote transcript/caption or media retrieval failure |
| `5` | STT provider failure |
| `7` | Missing local dependency |

Code `6` is reserved for transcript processor failures.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

Tests and CI are deterministic and do not call paid APIs or require credentials. The synthetic fixture can be regenerated as a one-second, mono, 16 kHz PCM WAV tone.

## Roadmap

The planned product will normalize media from supported sources into one provider-independent transcript format. Automatic publisher/video-platform transcript discovery, feeds, additional STT providers, and transcript processors such as summaries, project application, and English-learning extraction are future work. The MVP does not include RAG, embeddings, a database, a web UI, hosted services, or telemetry. See [NORTH_STAR.md](NORTH_STAR.md) for product direction, [AGENTS.md](AGENTS.md) for implementation scope, and [SAFETY.md](SAFETY.md) for handling rules.

# Audigest

Audigest is designed to turn spoken media into reusable transcripts, then apply focused tools to those transcripts from the command line. Transcript acquisition is strategy-first: reuse local transcripts, captions, or text when available, and use STT only as a fallback.

Transcript and caption acquisition is local-first. Audigest accepts canonical transcript JSON, UTF-8 `.txt`, `.srt`, and `.vtt` directly. For a local media file, it checks adjacent sidecars in deterministic order: `<basename>.transcript.json`, `<basename>.vtt`, `<basename>.srt`, then `<basename>.txt`. For example, `episode.mp3` checks `episode.transcript.json`, `episode.vtt`, `episode.srt`, and `episode.txt`. It reuses a valid matching cached transcript before attempting STT. If an existing candidate is malformed, Audigest reports the problem and does not silently skip it to invoke STT.

Only if no usable transcript, caption, text, or matching cache is available does provider selection apply. The deterministic `fake` provider is an explicit fallback for exercising the pipeline; it does not recognize speech or describe audio. Explicitly selecting `openai` enables one real STT fallback through the OpenAI API. `auto` remains cache-only and never selects a network provider, even when `OPENAI_API_KEY` is set. Other providers are not implemented.

## Quick start

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
uv run audigest --help
uv run audigest ingest tests/fixtures/sample.wav --provider fake
```

The fixture is a one-second, generated 440 Hz tone, not speech. Its deterministic output is only a smoke-test transcript; it does not represent the sound in the file.

`ingest` is the primary command. Transcript files can be ingested without STT:

```bash
uv run audigest ingest ./episode.transcript.json
uv run audigest ingest ./episode.txt
uv run audigest ingest ./episode.vtt
uv run audigest ingest ./episode.srt
```

Plain text produces untimed transcript segments. Caption formats preserve their supplied timestamps. Rendering never invents timestamps for untimed text.

When a canonical JSON transcript is imported, its source metadata, provider, model, language, segments, speakers, and timestamps are preserved. Audigest assigns a new cache artifact ID and sets `created_at` to the ingestion time.

The initial CLI can render machine-readable output with `--json`:

```bash
uv run audigest ingest tests/fixtures/sample.wav --provider fake --json
```

In human mode, progress goes to stderr and the final result goes to stdout. In JSON mode, stdout contains JSON only; diagnostics go to stderr.

`--no-cache` skips cache-pointer lookup, but still reads direct or adjacent transcript/text/caption inputs; the newly acquired result is still stored. Audigest keeps one latest pointer per source URI, effective content fingerprint (including selected sidecar contents), and requested language. A changed media file or sidecar cannot reuse a stale entry. Provider selection does not constrain a valid cache hit because it applies only to STT fallback. The default cache root is the operating system's user cache location for Audigest; `AUDIGEST_CACHE_DIR` overrides that root. The cache uses this layout:

```text
<audigest-user-cache>/
  refs/<source-key-hash>.json  # latest pointer for the source fingerprint and language
  items/<transcript-id>/
    source.json
    transcript.json
    transcript.md
```

The canonical transcript is versioned JSON (`schema_version: "1"`). It contains an `id`; a source record (`type`, `uri`, and optional `title`, `author`, `published_at`, and `duration_seconds`); `provider`, optional `model` and `language`, and `created_at`; `segments` (`text`, optional `speaker`, and optional `start`/`end`); and full `text` derived from the segments. Segment timestamps default to `null`; each segment must have both start and end timestamps or neither. Renderers omit timing when it is absent. Provider response objects stay at adapter boundaries.

Local original media is read in place and is never copied or removed. `--keep-media` is for future generated or downloaded media only; it does not apply to local originals.

## Command status

`transcribe` remains a compatibility alias for `ingest`. Both commands use the same transcript-first acquisition, cache, and STT fallback behavior and accept the same options. In particular, `--provider` selects the STT fallback; it does not force transcription when a transcript or caption is available. A separate explicit STT-only command remains future work.

To enable the optional OpenAI SDK and explicitly use its fallback, install the `openai` extra and set an API key in the environment:

```bash
uv sync --all-groups --extra openai
export OPENAI_API_KEY="..."
uv run --extra openai audigest ingest ./episode.mp3 --provider openai --language en
```

The OpenAI fallback uses the [`gpt-transcribe` model](https://developers.openai.com/api/docs/guides/speech-to-text) and sends audio only after local transcript/caption acquisition and cache lookup have been exhausted. It reads only `OPENAI_API_KEY`, sends requests to the fixed official OpenAI API endpoint, has a 60-second timeout with retries disabled, and never saves the key. This uses the OpenAI API and its account billing; a ChatGPT or Codex subscription is not used or required.

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
| `AUDIGEST_STT_PROVIDER` | STT fallback provider; defaults to `auto`, which is cache-only and errors on a cache miss. Select `fake` or `openai` explicitly to enable STT fallback. |
| `AUDIGEST_LANGUAGE` | Optional transcription language. |
| `AUDIGEST_CACHE_DIR` | Override the local cache root. |
| `AUDIGEST_CONFIG_FILE` | Override the TOML config file path. |
| `OPENAI_API_KEY` | Optional OpenAI API credential, used only when `--provider openai` reaches the STT fallback. |

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
| `3` | Source resolution failure |
| `5` | STT provider failure |
| `7` | Missing local dependency |

Codes `4` (media/caption retrieval) and `6` (transcript processor failure) are reserved for future source adapters and processors.

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

The planned product will normalize media from supported sources into one provider-independent transcript format. Additional STT providers, captions from remote sources, and transcript processors such as summaries, project application, and English-learning extraction are future work. The MVP does not include RAG, embeddings, a database, a web UI, hosted services, or telemetry. See [NORTH_STAR.md](NORTH_STAR.md) for product direction, [AGENTS.md](AGENTS.md) for implementation scope, and [SAFETY.md](SAFETY.md) for handling rules.

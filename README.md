# Audigest

Audigest is a local-first CLI for turning spoken or already-transcribed content into a reusable canonical transcript, then producing focused summaries, project recommendations, or English-learning notes. It looks for existing text and captions before using speech-to-text (STT).

## What it supports

- Local canonical JSON, UTF-8 text/Markdown, SRT/VTT captions, and adjacent transcript sidecars.
- Direct public HTTP(S) transcript and media URLs, single-video YouTube captions, narrow publisher episode pages, RSS 2.0 feeds, and Spotify episodes mapped through an explicit publisher feed.
- Explicit OpenAI, ElevenLabs, or local Whisper STT; `fake` is available for offline CLI exercises.
- Canonical-only `summarize`, read-only `apply`, and CEFR-level `english` processors.
- Optional timestamps and speakers, JSON output for tools, Markdown output for people, and local cache reuse.

RSS accepts only RSS 2.0. Spotify episode ingestion requires `--feed` unless a transcript is supplied, and its public oEmbed title must exactly match one RSS item after whitespace and case normalization. Publisher-page discovery uses a small set of explicitly marked transcript/caption patterns; Audigest does not crawl sites.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups --frozen
uv run audigest --help
uv run audigest --version
uv run audigest ingest ./episode.vtt
uv run audigest show <transcript-id>
```

`ingest` prints a reusable transcript ID. `show` reads only the saved transcript. `transcribe` is the explicit STT-only path for local media; `ingest` uses STT only when it cannot find usable text or captions.

## Generate processor outputs

The `auto` provider is cache-only. For a new output, select OpenAI explicitly and provide `OPENAI_API_KEY` in your shell:

```bash
uv sync --all-groups --extra openai --frozen
export OPENAI_API_KEY="your-key"
uv run audigest summarize <transcript-id> --provider openai
uv run audigest apply <transcript-id> --project ~/projects/my-app --provider openai
uv run audigest english <transcript-id> --level B2 --provider openai
```

Once a result is cached, the same command can run with `--provider auto` without a key or model request. `--no-cache` bypasses lookup but does not select a provider. `apply` reads only the project `README.md` by default, never runs project commands, and never edits project files.

## Privacy and validation

Credentials stay in environment variables. Remote requests and downloaded media have size and time limits; downloaded media is deleted after use unless `--keep-media` is selected for `ingest`. Tests use synthetic inputs and mocked network/provider boundaries. The project has not validated live paid-provider output or quality.

```bash
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

## Documentation

- [Documentation index](docs/README.md)
- [Detailed CLI usage and source/provider behavior](docs/usage.md)
- [Product direction](docs/NORTH_STAR.md)
- [Safety and privacy rules](docs/SAFETY.md)
- [Contributor and agent guidance](docs/AGENTS.md)
- [Roadmap and acceptance evidence](docs/ROADMAP.md)
- [Changelog](docs/CHANGELOG.md)
- [Versioning policy](docs/versioning.md)

## License

[MIT License](LICENSE).

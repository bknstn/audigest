# Command recipes and limits

The examples use shell variables that hold the user-approved inputs or the actual ID returned by ingest. Quote paths and URLs. Use `audigest --version` first.

## Acquire and reuse a transcript

```bash
audigest ingest "$source" --provider auto --json
audigest show "$id"
audigest show "$id" --json
```

Use `id` from the ingest JSON and the same cache with later commands. Human mode shows useful Markdown or the ingest result on stdout. In JSON mode stdout is reserved for JSON and progress and errors are on stderr.

- For local files use UTF-8 text or Markdown, canonical JSON, SRT/VTT, or supported audio and video. Local media may have a sidecar using the same basename.
- For a supplied transcript, use `audigest ingest "$source" --transcript "$transcript" --provider auto --json`. The supplied file or direct transcript URL takes priority over automatic discovery.
- Public direct HTTP(S) transcript/media URLs, public single-video YouTube captions, and explicitly marked publisher episode pages are supported within CLI limits. Malformed selected captions or transcripts produce an error.
- RSS 2.0 feeds with multiple items require an exact GUID or full item URL passed as `--episode`. A public Spotify episode needs an explicit publisher `--feed` unless a transcript was supplied. The public Spotify episode title must match the selected RSS item after whitespace and case normalization. Without a feed or supplied transcript, ask the user for one. Audigest only uses publisher resources for Spotify audio.
- When STT is needed, offer local Whisper or a user-approved remote backend. Reuse `ingest` for a fallback on the original source. Use `audigest transcribe "$local_media" --provider local --json` for an explicit STT-only local run with a prepared model.

## Analyze the stored text

```bash
audigest summarize "$id" --provider auto
audigest apply "$id" --project "$project" --context README.md --provider auto
audigest english "$id" --level "$level" --provider auto
```

Choose `--json` when structured output would help. `apply` reads only `README.md` by default or the exact relative UTF-8 paths selected with repeated `--context`. It reads at most 16 files and 64 KiB total. Keep the project cache outside the project directory.

On an approved generation request, choose the processor provider and model explicitly. These commands illustrate the available choices after the user approves the corresponding data transfer and possible cost:

```bash
audigest summarize "$id" --provider openai --model gpt-5.4-mini
audigest apply "$id" --project "$project" --provider anthropic --model claude-haiku-5-5
audigest english "$id" --level "$level" --provider anthropic --model claude-haiku-5-5
```

Processors read the canonical cache and never reacquire the media. They cap serialized input at 256 KiB and validate exact evidence against stored transcript segments and, for project recommendations, the selected files. `auto` checks saved output for matching provider/model settings without calling a provider. For a non-default model, pass the same `--model` used to generate it. Avoid `--no-cache` unless the user requests regeneration.

## Recover from errors

- Invalid selection or configuration (exit 2) → check arguments and local settings.
- Source or transcript missing (exit 3) → check the supplied path, ID, and cache root.
- Retrieval failed (exit 4) → explain the public-source restriction or ask for a usable local transcript.
- STT or its cache lookup failed (exit 5) → report the reason and wait for the user's choice if a new provider invocation is needed.
- Processor failed (exit 6) → distinguish a cache miss, missing key, model availability, rate limit, or invalid evidence. Avoid retries with a different paid provider.
- Local dependency missing (exit 7) → explain the relevant extra or existing model directory and obtain permission before installation.

For the full CLI guide, see the [Audigest documentation](https://github.com/bknstn/audigest/blob/main/docs/usage.md).

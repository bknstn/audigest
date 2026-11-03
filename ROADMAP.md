# Audigest Roadmap to the North Star MVP

## Status

The local transcript vertical slice, including the explicit STT-only command, and Iterations 2a (direct transcript/caption URLs), 2b (direct media URLs and supplied transcripts), 2c (YouTube captions-first acquisition), 3 (ElevenLabs STT), 4 (local Whisper), 5 (canonical-only summary processor), 6 (canonical-only project application), and 7 (canonical-only English learning) are implemented and review-accepted. Iteration 2c passed 231 offline tests; iteration 3 passed 255; iteration 4 passed 271; iteration 5 passed 308; iteration 6 passed 394; iteration 7 passed 438, with Ruff, formatting, and mypy checks passing. The North Star MVP is not complete. The current resume point is Iteration 2d, publisher/RSS/Spotify metadata discovery. The user authorized proceeding through the remaining MVP work as a sequence of small, reviewed iterations. That authorization supersedes earlier blanket deferrals only for the items listed here; it does not authorize unrelated product expansion.

## Completed checkpoint

### Iteration 0 — Local transcript acquisition, provenance, and reuse

- `ingest` prefers direct canonical JSON, UTF-8 `.txt`/`.md`, SRT/VTT captions, then deterministic adjacent sidecars before STT fallback.
- Canonical transcripts can retain optional `origin` provenance; imports preserve an embedded origin or identify the imported canonical file when none is present.
- Saved transcripts can be read by ID with `show`, as Markdown or canonical JSON, without accessing the original source or changing the cache.
- Offline suite: 167 tests passed at checkpoint.

### Iteration 1 — Separate strategy-first ingest from explicit STT

- `ingest` remains strategy-first. `transcribe` is now explicit STT-only for supported local media and never inspects direct transcript inputs or adjacent sidecars.
- Both commands look up STT results by the raw media fingerprint. Acquired transcript/caption entries use their existing effective fingerprints, so they cannot satisfy explicit `transcribe`; matching STT work remains reusable by either command.
- `auto` remains cache-only. `--no-cache` reruns STT for `transcribe`, while `ingest` still honors acquired inputs before its STT fallback.
- Offline suite: 164 tests passed. Implementation is accepted.

## Remaining MVP iterations

### Iteration 2a — Direct remote transcript documents

- `ingest` accepts explicitly supplied HTTP(S) URLs ending in `.txt`, `.md`, `.json`, `.srt`, or `.vtt`; shared parsers produce canonical text/caption drafts and preserve embedded canonical source/origin metadata.
- Remote GETs have bounded socket/total timeouts, redirect targets are limited to HTTP(S) without URL userinfo, response bodies are limited to 2 MiB, and query/fragment values are omitted from persisted URIs and diagnostics.
- Remote responses are fetched and parsed before cache lookup. Cache identity includes a hash of the full query-bearing locator and response bytes; remote text URLs never select STT, even with `--provider openai`.
- No remote media, publisher discovery, feed resolution, or video-platform caption retrieval is included in this slice. Offline tests mock all HTTP boundaries.
- Implementation is accepted; offline suite: 190 tests passed.

### Iteration 2b — Direct remote media and supplied transcripts

- `ingest` accepts direct HTTP(S) media URLs and a `--transcript` local file or direct transcript URL for local or remote media. The selected transcript precedes sidecars, cache lookup, and STT; remote media is not downloaded when a transcript is supplied.
- Without a supplied transcript, stream remote media to temporary storage under a 100 MiB cap, clean it up after processing, and retain it only after success when `--keep-media` is selected. Existing retained files are reused only when contents match; never overwrite user files or follow symlinks.
- Cache identities include the full remote media locator hash and bytes, or the media identity plus supplied transcript URI/content identity. Fetch media before cache lookup to detect changed bytes, while reusing prior STT work.
- Implementation is accepted; offline suite: 205 tests passed, and Ruff lint/format checks and mypy passed. Tests mock all HTTP boundaries, provider calls, failures, and retention.

### Iteration 2c — YouTube captions-first acquisition

- `ingest` accepts public single-video watch, `youtu.be`, shorts, and embed URLs; explicit `--transcript` is handled first without YouTube metadata or media requests.
- The optional `yt-dlp` subprocess reads bounded metadata without user config, cookies, plugins, updates, geo bypass, or remote components. Select manual/automatic VTT/SRT captions by requested language, original language, then stable available-language order before any STT lookup.
- Caption cache identity includes stable video ID, selected track language/kind/format, and exact caption bytes. If no captions exist, matching STT cache is checked before direct audio-only download; STT cache freshness uses video ID and title/uploader/duration/upload-date metadata, not signed audio URL tokens. HLS/DASH manifests are excluded; temporary and `--keep-media` behavior follows the existing remote-media contract.
- Reject private, age-restricted, live/upcoming, playlist-only, malformed, or unavailable inputs without authentication bypass. Caption parse/fetch errors do not fall back to STT. Offline tests use synthetic metadata, captions, and media only.
- Implementation is accepted; offline suite: 231 tests passed. All required checks passed.

### Iteration 3 — ElevenLabs STT adapter

- Add the optional `elevenlabs` SDK extra and explicit provider adapter for `scribe_v2`; `ingest` and `transcribe` continue to share the canonical provider protocol.
- Read only `ELEVENLABS_API_KEY`; use the fixed official endpoint, a 60-second request timeout, and disabled retries; sanitize SDK failures and keep keys out of artifacts.
- Request full text without timestamps and normalize it to one untimed segment, retaining returned language when valid.
- Offline suite: 255 tests passed; all required checks passed. Implementation is accepted. CI uses SDK mocks only; it does not call the paid API.

### Iteration 4 — Local Whisper STT adapter

- Add an opt-in `local` provider backed by the optional faster-whisper SDK. It accepts only `AUDIGEST_WHISPER_MODEL_DIR`, an existing local CTranslate2 model directory containing `model.bin`, `config.json`, and `tokenizer.json`.
- Instantiate faster-whisper with `local_files_only=True`, `device="cpu"`, and `compute_type="int8"`; Audigest does not download model/tokenizer files. Require an explicit provider selection and load the optional SDK only after a cache miss.
- Preserve actual segment timestamps and detected language; reject empty, malformed, or invalidly timed output as a sanitized provider error. Persist only the model directory basename plus backend, not the full local path.
- Offline tests inject a synthetic SDK/model boundary and do not download weights or call a live service. All 271 offline tests pass; Ruff, formatting, and mypy checks pass. Implementation is accepted.

### Iteration 5 — Canonical-only summary processor

- `summarize <transcript-id>` consumes only the stored canonical transcript; it never resolves, downloads, or retranscribes the source. `auto` is cache-only, while OpenAI generation requires `--provider openai` or `AUDIGEST_LLM_PROVIDER=openai`.
- Use the OpenAI Responses structured parse boundary with separate LLM provider/model settings, a `gpt-6-luna` default, and a 256 KiB serialized input cap. Return a summary and one to six key points with exact quotes validated against canonical segments; copy timestamps and speakers only from those segments.
- Store summary JSON and Markdown under `outputs/summaries/<summary-id>/`, independently of canonical transcript files. Validate citations and nonblank summary text again on cache hits; keep generated text and credentials out of version control.
- Offline suite: 308 tests passed; Ruff lint/format and mypy passed. Implementation is accepted. Tests use synthetic transcripts and mocked providers only; no live API calls or credentials are required.

### Iteration 6 — Canonical-only project application processor

- `apply <transcript-id> --project <directory>` consumes only a stored canonical transcript and an existing project directory. It never resolves the original source, invokes acquisition or STT, runs project commands, or modifies project files.
- Read only `README.md` by default. Repeated `--context` selects only explicit project-relative UTF-8 regular files; reject absolute/traversing/hidden/escaping paths, symlinks, non-regular files, missing files, empty/invalid text, more than 16 files, and more than 64 KiB total. Sort selected paths and deduplicate repeats. Keep the configured cache outside the project.
- `auto` is cache-only; explicit OpenAI uses the shared LLM settings and sends only transcript title/indexed text plus selected filenames/content under a 256 KiB serialized input cap. Return an overview and zero to six recommendations, each grounded by exact transcript and project quotes; copy timestamps/speakers only from canonical segments.
- Store JSON and Markdown under `outputs/applications/<application-id>/`; cache identity includes canonical citation context, resolved project identity hash, selected filenames/content hashes, provider/model, version, and options. Re-read context and revalidate both evidence sources on cache hits. Tests use synthetic project files/transcripts and mocked providers only.
- Offline suite: 394 tests passed; Ruff lint/format and mypy passed. Implementation is accepted. No live API calls or credentials are required.

### Iteration 7 — Canonical-only English learning processor

- Add `english <transcript-id> --level <CEFR>` for a stored transcript, accepting A1/A2/B1/B2/C1/C2; require the level and prioritize useful B2-to-C1 material for B2.
- Consume only title and indexed canonical segment text plus requested level under a 256 KiB serialized input cap. Never access the original source, perform language detection, invoke acquisition, or run STT. Reject empty transcript text; allow zero items when no useful English material is present.
- Use shared LLM provider/model settings, cache-only `auto`, structured OpenAI Responses output with low reasoning effort, no tools/storage/retries, 60-second timeout, and 8,000 output tokens. Return an overview and zero to twelve typed items with exact transcript quotes, verbatim expressions, meanings, usage notes, and one or two separate generated examples.
- Validate each quote against its indexed canonical segment and the expression as a substring of that quote; attach optional timing and speaker only from the canonical segment. Preserve untimed source material as untimed. Store separate JSON and Markdown under `outputs/english/<english-id>/`; cache identity covers transcript ID/content/citation metadata, level, provider/model, versions, and generation options.
- Offline suite: 438 tests passed; Ruff lint/format and mypy passed. Implementation is accepted. Tests use synthetic transcript text and mocked model responses only; no live API calls or credentials.

### Iteration 2d — Publisher, RSS, and Spotify metadata discovery

- Add bounded publisher routes and discover existing publisher-provided transcripts/captions before STT.
- RSS and Spotify support may resolve public metadata to publisher-hosted enclosures; never download or rip a Spotify stream.
- Keep ambiguous, malformed, and inaccessible routes actionable without authentication bypass or live test dependencies.

### Iteration 8 — Offline acceptance and product documentation

- Verify the complete local and mocked-source workflows, cache reuse/invalidation, optional timestamps, provenance, CLI output contracts, and error categories.
- Document setup, provider selection, local and remote source behavior, processors, privacy, and common failure recovery.
- Keep normal tests deterministic and offline. Never require credentials, paid APIs, or live external sources in CI.

## Boundaries and execution

- Do not add RAG, vector embeddings, a database, GUI, hosted service, background jobs, telemetry, or unrelated features as part of this roadmap.
- Keep Spotify behavior limited to metadata resolution and publisher-hosted enclosures; never rip or download a Spotify stream.
- Work one implementation iteration at a time. The parent agent reviews and scopes the next iteration before coding begins.
- The user authorized committing and pushing to `main` after each completed iteration receives parent review approval. Never force-push or overwrite remote commits; if `main` has diverged, stop and report before merging or rebasing.
- Keep this roadmap current as iterations are accepted. Never record account usage, credentials, private transcripts, or other secrets in the repository.

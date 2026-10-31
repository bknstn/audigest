# Audigest Roadmap to the North Star MVP

## Status

The local transcript vertical slice, including the explicit STT-only command, and Iterations 2a (direct transcript/caption URLs) and 2b (direct media URLs and supplied transcripts) are implemented and review-accepted. Iteration 2b passed 205 offline tests. The North Star MVP is not complete: the current resume point is a small Iteration 2c for YouTube captions-first acquisition. Automatic publisher discovery, RSS, and Spotify metadata/enclosure support are separate later slices, followed by additional STT providers and canonical-transcript processors. The user authorized proceeding through the remaining MVP work as a sequence of small, reviewed iterations. That authorization supersedes earlier blanket deferrals only for the items listed here; it does not authorize unrelated product expansion.

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

- Use available YouTube captions before considering STT, with explicit, bounded acquisition behavior.
- Keep caption parsing and provenance in the canonical transcript contract; do not add authentication bypass or live test dependencies.
- Scope and acceptance tests will be selected as a separate small iteration before implementation.

### Iteration 2d — Publisher, RSS, and Spotify metadata discovery

- Add bounded publisher routes and discover existing publisher-provided transcripts/captions before STT.
- RSS and Spotify support may resolve public metadata to publisher-hosted enclosures; never download or rip a Spotify stream.
- Keep ambiguous, malformed, and inaccessible routes actionable without authentication bypass or live test dependencies.

### Iteration 3 — ElevenLabs STT adapter

- Add an explicit provider adapter that normalizes provider responses into the canonical transcript draft.
- Read credentials only from documented environment variables; keep them out of diagnostics and stored artifacts.
- Test request/response normalization, errors, and cache behavior with mocks; do not call paid endpoints in CI.

### Iteration 4 — Local Whisper STT adapter

- Add an opt-in local Whisper/faster-whisper adapter behind the provider boundary, with clear local dependency and model requirements.
- Keep model downloads or other setup explicit; do not introduce hidden network calls during normal ingestion.
- Test routing and normalization offline, with optional dependency behavior covered without requiring model downloads in CI.

### Iteration 5 — Canonical-only summary processor

- Add `summarize` for an already stored transcript ID. It consumes the canonical transcript and never resolves, downloads, or retranscribes the source.
- Keep model/provider choice explicit and processor output separate from the source transcript artifact.
- Cover output shape, errors, and no-source-access behavior with deterministic tests.

### Iteration 6 — Canonical-only project application processor

- Add `apply` for an already stored transcript ID and an explicitly selected project path.
- Use transcript content plus the requested project context only; do not turn this into general project indexing, RAG, or background processing.
- Keep generated changes reviewable and avoid modifying project files unless a later task explicitly defines that behavior.

### Iteration 7 — Canonical-only English learning processor

- Add `english` for an already stored transcript ID with an explicit level such as B2.
- Preserve available source context and timestamps; do not invent timing for untimed transcripts.
- Test with synthetic transcript text and mocked model responses only.

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

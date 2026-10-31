# Safety, privacy, and fixture rules

Audigest reads audio, video, transcripts, captions, and text that may contain private, copyrighted, or otherwise sensitive material. Users are responsible for having the rights and permissions needed to process the files they supply. Keep processing local by default and make any future external provider use explicit.

## Files, media, and caches

- Never commit API keys, access tokens, cookies, private downloaded media, or user transcripts.
- Never persist credentials in transcript files, cache metadata, configuration defaults, or generated output. The OpenAI provider reads only `OPENAI_API_KEY` from the environment; other provider credentials are future work.
- Do not log secrets, authorization values, complete request headers, or full private transcript content in diagnostics.
- Keep caches, `.env` files, model files, media, and generated transcripts out of version control with `.gitignore` rules.
- A local original media, transcript, caption, or text file is user-owned input: read it in place; never copy it into the cache, modify it, or delete it.
- If later source adapters generate or download media, keep those files in temporary storage and clean them up after processing by default. Retain only tool-generated/downloaded media when the user explicitly passes `--keep-media`. This option never grants permission to copy or remove a local original.
- Cache pointers must distinguish source URI, effective content fingerprint (including selected sidecar contents), and requested language so changed local files or sidecars cannot reuse stale transcripts. Preserve actual provider/model provenance in the transcript, but do not let fallback provider selection suppress a valid matching cache entry.
- `show <transcript-id>` reads only an existing canonical transcript from the local cache. It must not inspect the original source, select a provider, invoke acquisition/STT, or modify cache artifacts.
- Do not add telemetry in the MVP.

## Fixtures and test data

- Prefer short fixtures that are fully synthetic and generated for tests, or material whose redistribution permission is documented and clear.
- Synthetic audio may contain generated tones/noise or newly written, non-identifying speech. Synthetic `.txt`, canonical JSON, SRT, and VTT fixtures must be authored for the test and must not reproduce real users' transcripts, private conversations, or copyrighted source material.
- Do not add a real podcast/video excerpt, downloaded media, user recording, credentials, cookies, provider response containing personal data, or an actual cached transcript to the repository.
- When testing third-party source adapters in future work, mock their boundary with synthetic metadata/caption payloads; do not make live network requests in normal CI.
- Keep fixture content minimal and document the origin and license for any redistributable fixture.

## Network and source boundaries

- `ingest` reads local canonical transcript JSON, UTF-8 text/Markdown, SRT/VTT captions, deterministic adjacent sidecars, and explicitly supplied direct HTTP(S) transcript/caption URLs ending in `.json`, `.txt`, `.md`, `.srt`, or `.vtt` before considering STT. Markdown is accepted as direct input only and does not alter sidecar discovery. Remote documents are fetched with unauthenticated GETs bounded to a 15-second socket timeout, a 60-second total deadline, at most five redirects, and 2 MiB of response data. URL userinfo and non-HTTP(S) redirects are rejected. Source/origin URIs omit query and fragment values; a one-way hash of the full input locator distinguishes cache entries. `transcribe` accepts local media only and skips acquisition and sidecar inspection. The other current runtime network path is explicitly selected OpenAI STT: a fallback after acquisition for `ingest`, or the selected provider for `transcribe`. `auto` never selects an STT provider or reads `OPENAI_API_KEY`.
- For `ingest`, a malformed existing transcript/text/caption candidate must fail clearly. Never skip malformed user content and invoke STT in its place; that could send audio to a provider or incur an unexpected cost. Explicit `transcribe` intentionally ignores adjacent sidecars.
- STT is a fallback for `ingest` and the explicit operation of `transcribe`. `ingest` must not let `--provider` override a usable transcript or caption. `transcribe` accepts supported local media only and ignores adjacent transcript/caption sidecars. Both commands reuse matching valid STT cache entries before calling a provider; `auto` remains cache-only and must fail clearly on a cache miss.
- Canonical segments may omit timing. A segment's start and end must either both be absent or both be valid timestamps. Renderers must not invent timestamps for untimed text.
- Direct transcript retrieval and the OpenAI provider use explicit source/provider adapters, bounded timeouts, and sanitized errors. Direct transcript URLs never send media to an STT provider; malformed remote documents fail before provider selection. Do not add hidden network calls or send media anywhere without an explicit user-selected provider.
- Never bypass authentication, paywalls, DRM, or access controls. Respect platform terms and copyright.
- Spotify support, if implemented later, may resolve public metadata and map an episode to its publisher-hosted RSS enclosure. Never download or rip audio from Spotify streams. If a mapping is ambiguous, report the available metadata and fail rather than guessing.
- Use environment variables for provider credentials. The current OpenAI integration accepts only `OPENAI_API_KEY`; never add persisted config-file secrets without documenting their filesystem and security implications.

## Scope guard

`ingest` acquires existing local transcript/caption/text data and explicitly supplied direct HTTP(S) transcript documents, then checks the matching cache before STT. Remote media, publisher discovery, feeds, and video-platform caption routes remain future slices. `transcribe` is the explicit STT-only path for supported local media and skips acquisition. The deterministic fake provider is an offline fallback; OpenAI is available only when the user explicitly selects `--provider openai` and supplies `OPENAI_API_KEY`. `auto` reuses matching valid cached results appropriate to each command and otherwise fails without selecting an STT provider, even when an API key exists. The user has authorized the remaining North Star MVP items listed in `ROADMAP.md` for sequential, parent-reviewed iterations; implement only the currently selected item. RAG, embeddings, databases, GUI, background jobs, and telemetry remain out of scope.

# Safety, privacy, and fixture rules

Audigest reads audio, video, transcripts, captions, and text that may contain private, copyrighted, or otherwise sensitive material. Users are responsible for having the rights and permissions needed to process the files they supply. Keep processing local by default and make any future external provider use explicit.

## Files, media, and caches

- Never commit API keys, access tokens, cookies, private downloaded media, or user transcripts.
- Never persist credentials in transcript files, cache metadata, configuration defaults, or generated output. Prefer environment variables for future provider credentials.
- Do not log secrets, authorization values, complete request headers, or full private transcript content in diagnostics.
- Keep caches, `.env` files, model files, media, and generated transcripts out of version control with `.gitignore` rules.
- A local original media, transcript, caption, or text file is user-owned input: read it in place; never copy it into the cache, modify it, or delete it.
- If later source adapters generate or download media, keep those files in temporary storage and clean them up after processing by default. Retain only tool-generated/downloaded media when the user explicitly passes `--keep-media`. This option never grants permission to copy or remove a local original.
- Cache pointers must distinguish source URI, effective content fingerprint (including selected sidecar contents), and requested language so changed local files or sidecars cannot reuse stale transcripts. Preserve actual provider/model provenance in the transcript, but do not let fallback provider selection suppress a valid matching cache entry.
- Do not add telemetry in the MVP.

## Fixtures and test data

- Prefer short fixtures that are fully synthetic and generated for tests, or material whose redistribution permission is documented and clear.
- Synthetic audio may contain generated tones/noise or newly written, non-identifying speech. Synthetic `.txt`, canonical JSON, SRT, and VTT fixtures must be authored for the test and must not reproduce real users' transcripts, private conversations, or copyrighted source material.
- Do not add a real podcast/video excerpt, downloaded media, user recording, credentials, cookies, provider response containing personal data, or an actual cached transcript to the repository.
- When testing third-party source adapters in future work, mock their boundary with synthetic metadata/caption payloads; do not make live network requests in normal CI.
- Keep fixture content minimal and document the origin and license for any redistributable fixture.

## Network and source boundaries

- The first-session transcript-first implementation must make no runtime network requests and must not call external APIs. It reads local canonical transcript JSON, UTF-8 text, SRT/VTT captions, and deterministic adjacent sidecars before considering STT.
- A malformed existing transcript/text/caption candidate must fail clearly. Never skip malformed user content and invoke STT in its place; that could send audio to a provider or incur an unexpected cost.
- STT is a fallback only. `--provider` selects that fallback and does not override an existing usable transcript or caption. `auto` reuses a matching valid cache before STT; if no cache matches and no provider is available, it must fail clearly.
- Canonical segments may omit timing. A segment's start and end must either both be absent or both be valid timestamps. Renderers must not invent timestamps for untimed text.
- Future integrations must use explicit provider/source adapters, bounded timeouts, and useful errors. Do not add hidden network calls or send media anywhere without an explicit user-selected provider.
- Never bypass authentication, paywalls, DRM, or access controls. Respect platform terms and copyright.
- Spotify support, if implemented later, may resolve public metadata and map an episode to its publisher-hosted RSS enclosure. Never download or rip audio from Spotify streams. If a mapping is ambiguous, report the available metadata and fail rather than guessing.
- For future provider credentials, use environment variables first. Do not support persisted config-file secrets without documenting their filesystem and security implications.

## Scope guard

For the first session, acquire existing local transcript/caption/text data before STT. Select the deterministic fake provider explicitly only when fallback is needed. `auto` reuses a matching valid cached transcript, and otherwise must explain that no fallback provider is available. Keep real STT and LLM providers, processors, project-context ingestion, RAG, embeddings, databases, and network-backed source resolution out of this implementation until separately scoped.

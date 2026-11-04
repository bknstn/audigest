# Changelog

## 0.1.0 — Initial MVP

The first Audigest MVP provides:

- Strategy-first transcript acquisition from local text/captions/media, explicit remote transcript or media URLs, public YouTube captions, bounded publisher episode pages, RSS 2.0, and Spotify episodes mapped through an explicit RSS feed.
- Explicit OpenAI and ElevenLabs STT plus local faster-whisper, with `auto` limited to matching cache reuse.
- A versioned canonical transcript with provenance and optional timestamps/speakers, plus `show`, `summarize`, read-only project `apply`, and CEFR-level `english` processors.
- An eager `audigest --version` command backed by the package's single version value.
- Separate processor artifacts, local artifact caching, bounded remote retrieval, and downloaded-media cleanup by default.

Validation for this initial milestone is offline and mocked. CI run [41913900872](https://github.com/bknstn/audigest/actions/runs/41913900872) passed on Python 3.12 and 3.13 for runtime commit `7c554f90b26349fad12c889d102089f8052fc36a`. Tests do not call live sources or paid APIs and do not validate model output quality, paid provider behavior, or model downloads.

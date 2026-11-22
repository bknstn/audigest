# Setup and providers

Start with `audigest --version`. The full workflow requires Audigest 0.2.0, Python 3.12 or newer, and a terminal. Use `uv` for setup if Audigest is missing. Await the user's authorization before changing the environment. Read-only project application requires terminal access to the user-approved project, a cache outside that project, and a platform with safe no-follow file opens such as macOS or Linux.

## Installation

Install a reviewed, versioned wheel obtained from the owner or the corresponding [Audigest release](https://github.com/bknstn/audigest/releases) when available. Verify the wheel's SHA-256 against a trusted owner-provided checksum. The recipe below assumes the audited wheel has already been placed in the current directory.

```bash
uv tool install ./audigest-0.2.0-py3-none-any.whl
audigest --version
```

For optional backends, include only the extras you need. For example, to install Anthropic and YouTube support from the same reviewed wheel:

```bash
uv tool install './audigest-0.2.0-py3-none-any.whl[anthropic,youtube]'
audigest --version
```

If only v0.1.1 is available, it supports the local ingest/show workflow and OpenAI-only processing. To install that exact historical version, with approval, use a Python 3.12+ environment and its pinned public source:

```bash
uv tool install 'audigest @ git+https://github.com/bknstn/audigest.git@c4d39ef27cc1ff21ba5f65ae65d86674091ba343'
audigest --version
```

Obtain the 0.2.0 build for Anthropic or the updated processor defaults. Avoid unpinned source installs, piping downloads into a shell, and implicit administrator access.

## Optional features

| Purpose | CLI provider or feature | Extra | User setup |
| --- | --- | --- | --- |
| Process a stored transcript | `openai` | `openai` | `OPENAI_API_KEY`. Default `gpt-5.4-mini` supports Responses structured output and low reasoning effort. |
| Process a stored transcript | `anthropic` | `anthropic` | `ANTHROPIC_API_KEY`. Default `claude-haiku-5-5` uses Claude Messages structured output. |
| Speech-to-text | `openai` | `openai` | `OPENAI_API_KEY`. |
| Speech-to-text | `elevenlabs` | `elevenlabs` | `ELEVENLABS_API_KEY`. |
| Speech-to-text on CPU | `local` | `local` | `AUDIGEST_WHISPER_MODEL_DIR` points to an existing CTranslate2 model with `model.bin`, `config.json`, and `tokenizer.json`. |
| Public single-video captions | YouTube | `youtube` | Optional `yt-dlp` package. Audigest disables cookies, remote components, plugins, and user configuration for metadata extraction. |

Have the user set credentials privately in their shell or secret manager. OpenAI and Anthropic processor requests use their official API endpoints and can incur charges. Local Whisper uses an existing model with no implicit weight or tokenizer download. Its installation depends on platform support for faster-whisper and CTranslate2.

Keep `AUDIGEST_STT_PROVIDER` separate from `AUDIGEST_LLM_PROVIDER` and `AUDIGEST_LLM_MODEL`. Processor flags override those LLM variables, followed by provider-specific defaults. Pass `--provider auto` explicitly for a cache-only run even when the environment has a provider configured.

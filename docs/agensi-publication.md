# Audigest on Agensi

## Listing fields

**Name**

Audigest — reusable transcripts and AI insights

**Summary**

Reuse existing transcripts and captions before using speech-to-text. Store a transcript once, then create evidence-checked summaries, read-only project recommendations, or English-learning notes using the Audigest CLI.

**Description**

Audigest looks for existing text before trying speech-to-text. Start with local Markdown, text, captions or media, a supported public YouTube video, a publisher episode page, an RSS 2.0 feed, or a Spotify episode with its publisher feed. The CLI stores a canonical transcript and returns an ID you can reuse from the same cache.

Use that ID to read the transcript, summarize it with exact source quotes, draw recommendations from selected project files, or extract English-learning material at a requested CEFR level. Project recommendations are read-only. Timing and speakers are carried through when the transcript includes them. Files and results stay in the local cache. New analysis or speech-to-text through a remote provider needs your explicit choice and can incur API charges.

**Compatibility note**

Requires a terminal, Audigest 0.2.0, and Python 3.12 or newer. Use uv to install the versioned CLI separately. Public YouTube requires the optional `youtube` extra. OpenAI or Anthropic analysis and OpenAI or ElevenLabs speech-to-text require the selected SDK extra and its own API key. Local Whisper requires platform support for faster-whisper and CTranslate2, an existing model directory, and CPU resources. The agent must be able to reach the user-approved files and write to the local Audigest cache. Read-only project application requires safe no-follow file opens, available on macOS and Linux.

**Pricing**

Free for the initial listing. Optional provider calls are billed separately by the user's API provider. A free listing requires no payout setup.

**Categories**

Productivity, Research & Analysis, Education & Learning

**Tags**

transcripts, captions, podcasts, YouTube, source citations, English learning, CLI

**Use cases**

- Turn a supplied VTT or SRT into a reusable transcript with its original timing.
- Retrieve available captions from a supported public video and summarize the stored text.
- Develop read-only project recommendations with exact transcript and project excerpts.
- Practice expressions from a transcript at a user-selected CEFR level.

**Docs link**

[Audigest CLI usage](https://github.com/bknstn/audigest/blob/main/docs/usage.md)

**Limitations**

- Source support depends on public availability and the CLI's supported acquisition routes. RSS must be RSS 2.0. Spotify needs a publisher feed with a matching episode title or a supplied transcript. Audigest uses publisher resources for Spotify audio.
- Analysis accepts up to 256 KiB of serialized input per request. Read-only project application accepts up to 16 selected UTF-8 files and 64 KiB of file content.
- The default `auto` provider uses existing output only. A new result requires an explicitly selected LLM. Both OpenAI and Anthropic have offline SDK coverage. Live smoke tests are pending for both.
- The skill requires an installed CLI. Agent environments without a terminal or local filesystem access cannot run the workflow. Some public YouTube videos lack accessible captions or eligible audio.

**FAQ**

- **Which model is used?** Audigest reads its own LLM provider and model settings. The model running the agent does not configure the CLI.
- **Can I use it without an API key?** Yes for supplied text and captions, `show`, and matching cached output. Local Whisper can transcribe with a prepared model. New analyses need a selected OpenAI or Anthropic API key.
- **Will project suggestions edit my files?** `apply` reads only `README.md` or the relative files you select and returns recommendations in the Audigest cache.
- **Does Agensi run the CLI?** The agent executes the CLI where it has terminal and file access. The ZIP contains the skill instructions and references.

## See it in action

**Request**

> Use Audigest to read this synthetic `rollout.vtt`, store its canonical transcript, and show the transcript ID and full stored text. Use the captions and their time ranges.

**Result**

The agent followed the extracted ZIP instructions with a clean base-only CLI installation and a new cache. It ran `audigest --version`, `audigest ingest rollout.vtt --provider auto --json`, and `audigest show` with the returned ID. This is the complete Markdown stdout from `show`. The path belongs to an isolated test directory.

```text
# Transcript

## Metadata

| Field | Value |
| --- | --- |
| Transcript ID | 2c99eb8d1db717a7f149576b5ba36d39ecdd9c2a92f5e08bfd7dad6174954860 |
| Schema version | 1 |
| Source type | local |
| Source URI | file:///private/tmp/audigest-agensi.PrTphJTF/demo/rollout.vtt |
| Origin kind | captions |
| Origin URI | file:///private/tmp/audigest-agensi.PrTphJTF/demo/rollout.vtt |
| Origin format | vtt |
| Title | rollout |
| Author | — |
| Published at | — |
| Duration (seconds) | — |
| Language | — |
| Provider | captions |
| Model | — |
| Created at | 2026-11-22T10:48:28.297121+00:00 |

## Segments

- [00:00:01.000 – 00:00:05.000] We can roll out changes in small batches. Start with a group that can give quick feedback.
- [00:00:06.000 – 00:00:12.000] Compare the results with the previous release. If the new flow causes confusion, pause the rollout and fix the problem.
- [00:00:13.000 – 00:00:19.000] Write down what changed, what users reported, and which signal will tell us when to continue.

## Full text

We can roll out changes in small batches. Start with a group that can give quick feedback.
Compare the results with the previous release. If the new flow causes confusion, pause the rollout and fix the problem.
Write down what changed, what users reported, and which signal will tell us when to continue.
```

## Permissions for the listing

- **Terminal** to run the local `audigest` executable. Installation is separate and authorized by the user. It can reach PyPI for packages and GitHub or an owner-provided location for the pinned Audigest build.
- **File reads** for user-selected sources, the Audigest cache and configuration, and an existing local model if selected. Project application reads the selected relative files or `README.md`.
- **File writes** to Audigest's local cache for canonical transcripts and processor outputs. Remote media can use bounded temporary storage and can be retained only when explicitly requested. Project files stay read-only.
- **Network** for user-selected public HTTP(S) sources and their permitted redirects or declared resources, including YouTube metadata and caption/audio hosts, publisher and CDN resources, and Spotify public oEmbed metadata.
- **Optional provider network** to `api.openai.com` for selected OpenAI STT or LLM, `api.elevenlabs.io` for selected ElevenLabs STT, and `api.anthropic.com` for selected Anthropic LLM. STT can send audio. Processors send the transcript title and indexed text and, depending on the command, selected relative project filenames and contents or the CEFR level. The agent asks before sending private data or incurring charges unless the user already authorized that operation.

There are no Agensi-hosted tool dependencies. Review detected permissions against these declared scopes. [Agensi packaging and review](https://www.agensi.io/docs/packaging)

## Build and verify

Run from a clean checkout of the reviewed candidate:

```bash
uv sync --locked --all-groups --all-extras
uv run --no-sync pytest
uv run --no-sync ruff check
uv run --no-sync ruff format --check
uv run --no-sync mypy
uv run --no-sync python scripts/build_agensi_skill.py
unzip -t dist/audigest-agensi-skill.zip
shasum -a 256 dist/audigest-agensi-skill.zip
```

The ZIP is generated under `dist/` and Git ignores it. It contains one root `SKILL.md`, two Markdown references, and the MIT license. The archive fixes member order, timestamps, and permissions. The builder validates metadata, relative links, size and text format, rejects symlinks among the packaged paths, and checks for common credential formats. The standard Agent Skills frontmatter stays portable. Enter the marketplace fields from this page in Agensi's form. The package is within the website limit of 50 MB and 1,000 files and the agent submission limit of 5 MB and 100 text files. [Agensi packaging limits](https://www.agensi.io/docs/packaging)

The skill version is **1.0.0** and uses its own SemVer. Its `audigest-version` records the CLI version it was built against and must match the current package version. Validation on **22 November 2026** used the 0.2.0 working tree based on `v0.1.1` (`c4d39ef27cc1ff21ba5f65ae65d86674091ba343`). Record the reviewed release commit and ZIP digest before submission. All 590 offline tests passed on Python 3.12 and 3.13 on macOS arm64. Clean installation of the 0.2.0 wheel, with and without optional extras, and installation from the pinned public v0.1.1 commit passed on macOS arm64. The official Agent Skills reference validator passed on the extracted package. The demo was run by Codex reading the extracted files and following their commands. Automatic Agensi delivery remains unverified.

The Anthropic adapter uses Messages structured parsing with the same Pydantic schemas and local evidence validation as OpenAI Responses. This keeps prompts, payload limits, evidence rules, and output artifacts shared. Provider and model are part of the cache keys. Audigest 0.2.0 read all three output types created by v0.1.1 with an injected offline provider and no credentials. [Anthropic structured output](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)

### Optional live smoke tests

Run these only after the owner approves two billable API calls. Set each API key privately and install the `openai` and `anthropic` extras. Use synthetic input and a disposable cache. These commands are manual and remain outside CI.

```bash
(
  set -eu
  smoke_dir="$(mktemp -d)"
  trap 'rm -r -- "$smoke_dir"' EXIT
  : > "$smoke_dir/config.toml"
  export AUDIGEST_CONFIG_FILE="$smoke_dir/config.toml"
  export AUDIGEST_CACHE_DIR="$smoke_dir/cache"
  printf '%s\n' 'Roll out the change in small batches. Collect feedback after each batch.' > "$smoke_dir/synthetic.txt"
  result="$(uv run --no-sync audigest ingest "$smoke_dir/synthetic.txt" --provider auto --json)"
  transcript_id="$(printf '%s' "$result" | uv run --no-sync python -c 'import json, sys; print(json.load(sys.stdin)["id"])')"
  uv run --no-sync audigest summarize "$transcript_id" --provider openai --model gpt-5.4-mini --no-cache --json
  uv run --no-sync audigest summarize "$transcript_id" --provider anthropic --model claude-haiku-5-5 --no-cache --json
)
```

For each response, confirm exit status 0, the requested provider and model in JSON, at least one exact quote, and separate cache IDs. Record the test date, versions, and result before describing either provider as live-tested.

## Owner submission steps

1. Review the CLI changes and approve a pinned, buyer-accessible 0.2.0 build. Run `uv build` to produce the wheel and source distribution. Release tagging and pushing follow the project policy after approval and CI. Make the wheel and its SHA-256 available through the approved distribution channel. [Audigest release policy](versioning.md)
2. Build the ZIP from the reviewed source and verify its checksum and four expected members. Use the ZIP as the Agensi upload and the wheel for CLI installation.
3. Sign in to Agensi. Enable Creator Mode in **Dashboard → Settings** and complete the creator profile. [Creator quickstart](https://www.agensi.io/docs/creator-quickstart)
4. Open [Agensi submission](https://www.agensi.io/dashboard/submit) and upload `dist/audigest-agensi-skill.zip`. Confirm that Agensi detects one skill. Enter the listing fields and the demo above. Confirm that the name is available in the form and choose **Free**.
5. Review the detected tools, data and network permissions, documentation link, and preview. Submit for review from the website. [Agensi submission guide](https://www.agensi.io/docs/submit-on-the-website)
6. Monitor Creator Studio and email for the security scan, AI review, and human decision. Agensi asks creators to allow up to 48 hours for review. After approval, confirm that the listing is live and run the keyless caption demo from the buyer's agent. [Agensi review and approval](https://www.agensi.io/docs/review-and-approval)

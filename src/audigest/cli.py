"""Command-line interface for Audigest."""

import typer

from audigest import __version__
from audigest.errors import (
    ConfigurationError,
    MissingDependencyError,
    ProcessorError,
    ProviderError,
    RetrievalError,
    SourceResolutionError,
    StorageError,
)
from audigest.processors.application import (
    render_application_json,
    render_application_markdown,
)
from audigest.processors.apply_service import apply_to_project
from audigest.processors.english import render_english_json, render_english_markdown
from audigest.processors.english_service import english_transcript
from audigest.processors.service import summarize_transcript
from audigest.processors.summary import render_summary_json, render_summary_markdown
from audigest.renderers import render_json, render_markdown
from audigest.service import ingest as ingest_source
from audigest.service import show_transcript
from audigest.service import transcribe as transcribe_source

app = typer.Typer(
    name="audigest",
    help=(
        "Use 'ingest' to obtain the best available transcript, or 'transcribe' "
        "to explicitly run STT on local media."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_ERRORS = (
    ConfigurationError,
    SourceResolutionError,
    ProviderError,
    RetrievalError,
    MissingDependencyError,
    ProcessorError,
    StorageError,
)


def _version_callback(value: bool) -> bool:
    """Print the installed package version before invoking any command logic."""

    if value:
        typer.echo(f"audigest {__version__}")
        raise typer.Exit()
    return value


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    ),
) -> None:
    """Manage local transcript creation."""
    _ = version


def _run_source_command(
    source: str,
    *,
    episode: str | None,
    feed: str | None,
    transcript: str | None,
    provider: str | None,
    language: str | None,
    json_output: bool,
    no_cache: bool,
    keep_media: bool,
    explicit_stt: bool,
) -> None:
    typer.echo("Resolving source and checking the transcript cache...", err=True)
    try:
        if explicit_stt:
            result = transcribe_source(
                source,
                provider=provider,
                language=language,
                no_cache=no_cache,
                keep_media=keep_media,
            )
        else:
            result = ingest_source(
                source,
                episode=episode,
                feed=feed,
                transcript=transcript,
                provider=provider,
                language=language,
                no_cache=no_cache,
                keep_media=keep_media,
            )
    except _ERRORS as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=error.exit_code) from None

    if result.cached:
        status = "Using cached transcript."
    elif result.used_stt:
        status = "Transcription complete."
    else:
        status = "Using existing transcript, caption, or text."
    typer.echo(status, err=True)
    if result.retained_media_path is not None:
        typer.echo(f"Kept downloaded media: {result.retained_media_path}", err=True)

    if json_output:
        typer.echo(render_json(result.transcript))
        return

    typer.echo(
        "\n".join(
            (
                f"Transcript ID: {result.transcript.id}",
                f"JSON: {result.json_path}",
                f"Markdown: {result.json_path.with_name('transcript.md')}",
                *(
                    (f"Media: {result.retained_media_path}",)
                    if result.retained_media_path is not None
                    else ()
                ),
            )
        )
    )


def _ingest(
    source: str = typer.Argument(
        ...,
        help=(
            "Local transcript, caption, text, or media path; direct HTTP(S) transcript/media URL; "
            "RSS feed, narrow publisher episode page, public Spotify episode with --feed; "
            "or public single-video YouTube URL. "
            "Timing may be unavailable."
        ),
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="STT fallback provider; local transcripts and captions are tried first.",
    ),
    language: str | None = typer.Option(
        None,
        "--language",
        help="Optional language hint, such as 'en'.",
    ),
    transcript: str | None = typer.Option(
        None,
        "--transcript",
        help="Use this local transcript file or direct HTTP(S) transcript URL for a media source.",
    ),
    episode: str | None = typer.Option(
        None,
        "--episode",
        help=(
            "Select an RSS episode by exact GUID or full item link; with Spotify, its title "
            "must also match oEmbed metadata."
        ),
    ),
    feed: str | None = typer.Option(
        None,
        "--feed",
        help=(
            "Explicit publisher RSS 2.0 feed for a Spotify episode; required unless "
            "--transcript is supplied."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Write only the canonical transcript JSON object to stdout.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Skip cached transcript lookup; direct and adjacent transcripts still apply.",
    ),
    keep_media: bool = typer.Option(
        False,
        "--keep-media",
        help="Keep successfully downloaded remote media in the configured cache.",
    ),
) -> None:
    """Obtain the best available transcript before considering STT fallback."""
    _run_source_command(
        source,
        episode=episode,
        feed=feed,
        transcript=transcript,
        provider=provider,
        language=language,
        json_output=json_output,
        no_cache=no_cache,
        keep_media=keep_media,
        explicit_stt=False,
    )


def _transcribe(
    source: str = typer.Argument(
        ...,
        help="Supported local audio/video media; adjacent transcript sidecars are ignored.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="STT provider for explicit transcription; 'auto' is cache-only.",
    ),
    language: str | None = typer.Option(
        None,
        "--language",
        help="Optional language hint, such as 'en'.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Write only the canonical transcript JSON object to stdout.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Skip matching STT cache lookup and run STT again.",
    ),
    keep_media: bool = typer.Option(
        False,
        "--keep-media",
        help="No media is downloaded by transcribe; this option only affects ingest.",
    ),
) -> None:
    """Run STT explicitly on local media without reading transcript sidecars."""
    _run_source_command(
        source,
        episode=None,
        feed=None,
        transcript=None,
        provider=provider,
        language=language,
        json_output=json_output,
        no_cache=no_cache,
        keep_media=keep_media,
        explicit_stt=True,
    )


def _show(
    transcript_id: str = typer.Argument(..., help="ID printed by a successful ingest command."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Write only the canonical transcript JSON object to stdout.",
    ),
) -> None:
    """Render a transcript already saved in the local cache."""
    try:
        transcript = show_transcript(transcript_id)
    except _ERRORS as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=error.exit_code) from None

    if json_output:
        typer.echo(render_json(transcript))
        return
    typer.echo(render_markdown(transcript), nl=False)


def _summarize(
    transcript_id: str = typer.Argument(..., help="ID of a transcript already saved by ingest."),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Summary provider; 'auto' only reuses a cached result, 'openai' can create one.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="OpenAI model name; defaults to AUDIGEST_LLM_MODEL or gpt-6-luna.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Write only the summary artifact JSON object to stdout.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Recompute the summary instead of reusing a matching cached result.",
    ),
) -> None:
    """Summarize a canonical transcript already stored in the local cache."""
    typer.echo("Loading stored transcript and checking the summary cache...", err=True)
    try:
        result = summarize_transcript(
            transcript_id,
            provider=provider,
            model=model,
            no_cache=no_cache,
        )
    except _ERRORS as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=error.exit_code) from None

    typer.echo("Using cached summary." if result.cached else "Summary complete.", err=True)
    typer.echo(
        f"Summary JSON: {result.json_path}\nSummary Markdown: {result.markdown_path}",
        err=True,
    )
    if json_output:
        typer.echo(render_summary_json(result.artifact))
    else:
        typer.echo(render_summary_markdown(result.artifact), nl=False)


def _apply(
    transcript_id: str = typer.Argument(..., help="ID of a transcript already saved by ingest."),
    project: str = typer.Option(
        ...,
        "--project",
        help="Existing project directory. Audigest reads selected files but never modifies it.",
    ),
    context_paths: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--context",
        help=(
            "Project-relative UTF-8 file to read; repeat to select more files. "
            "Defaults to README.md."
        ),
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Application provider; 'auto' only reuses a cached result, 'openai' can create one.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="OpenAI model name; defaults to AUDIGEST_LLM_MODEL or gpt-6-luna.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Write only the application artifact JSON object to stdout.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Recompute recommendations instead of reusing a matching cached result.",
    ),
) -> None:
    """Apply transcript ideas to explicitly selected project context without editing it."""
    typer.echo("Loading stored transcript and selected project context...", err=True)
    try:
        result = apply_to_project(
            transcript_id,
            project,
            context_paths=context_paths,
            provider=provider,
            model=model,
            no_cache=no_cache,
        )
    except _ERRORS as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=error.exit_code) from None

    typer.echo("Using cached application." if result.cached else "Application complete.", err=True)
    typer.echo(
        f"Application JSON: {result.json_path}\nApplication Markdown: {result.markdown_path}",
        err=True,
    )
    if json_output:
        typer.echo(render_application_json(result.artifact))
    else:
        typer.echo(render_application_markdown(result.artifact), nl=False)


def _english(
    transcript_id: str = typer.Argument(..., help="ID of a transcript already saved by ingest."),
    level: str = typer.Option(
        ...,
        "--level",
        help="Requested CEFR level: A1, A2, B1, B2, C1, or C2.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help=(
            "English processor provider; 'auto' only reuses a cached result, "
            "'openai' can create one."
        ),
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="OpenAI model name; defaults to AUDIGEST_LLM_MODEL or gpt-6-luna.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Write only the English artifact JSON object to stdout.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Recompute English-learning output instead of reusing a matching cached result.",
    ),
) -> None:
    """Extract useful English-learning items from a stored canonical transcript."""
    typer.echo("Loading stored transcript and checking the English result cache...", err=True)
    try:
        result = english_transcript(
            transcript_id,
            level,
            provider=provider,
            model=model,
            no_cache=no_cache,
        )
    except _ERRORS as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=error.exit_code) from None

    typer.echo(
        "Using cached English result." if result.cached else "English learning complete.",
        err=True,
    )
    typer.echo(
        f"English JSON: {result.json_path}\nEnglish Markdown: {result.markdown_path}",
        err=True,
    )
    if json_output:
        typer.echo(render_english_json(result.artifact))
    else:
        typer.echo(render_english_markdown(result.artifact), nl=False)


app.command(
    name="ingest",
    help="Obtain the best available local transcript; STT is used only as a fallback.",
)(_ingest)
app.command(
    name="show",
    help="Display a transcript already stored in the local cache.",
)(_show)
app.command(
    name="summarize",
    help="Summarize a canonical transcript already stored in the local cache.",
)(_summarize)
app.command(
    name="apply",
    help="Apply transcript ideas to explicitly selected project context without editing it.",
)(_apply)
app.command(
    name="english",
    help="Extract useful English-learning items from a stored canonical transcript.",
)(_english)
app.command(
    name="transcribe",
    help="Explicitly run STT on a supported local media file; transcript sidecars are ignored.",
)(_transcribe)

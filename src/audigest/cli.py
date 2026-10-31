"""Command-line interface for Audigest."""

import typer

from audigest.errors import (
    ConfigurationError,
    MissingDependencyError,
    ProviderError,
    SourceResolutionError,
    StorageError,
)
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
    MissingDependencyError,
    StorageError,
)


@app.callback()
def main() -> None:
    """Manage local transcript creation."""


def _run_source_command(
    source: str,
    *,
    provider: str | None,
    language: str | None,
    json_output: bool,
    no_cache: bool,
    keep_media: bool,
    explicit_stt: bool,
) -> None:
    typer.echo("Resolving local source and checking the transcript cache...", err=True)
    try:
        action = transcribe_source if explicit_stt else ingest_source
        result = action(
            source,
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
        status = "Using existing local transcript, caption, or text."
    typer.echo(status, err=True)

    if json_output:
        typer.echo(render_json(result.transcript))
        return

    typer.echo(
        "\n".join(
            (
                f"Transcript ID: {result.transcript.id}",
                f"JSON: {result.json_path}",
                f"Markdown: {result.json_path.with_name('transcript.md')}",
            )
        )
    )


def _ingest(
    source: str = typer.Argument(
        ...,
        help=(
            "Local canonical JSON, UTF-8 text/Markdown, SRT/VTT captions, "
            "or audio/video media path. "
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
        help="Keep future generated media; local source files are read in place.",
    ),
) -> None:
    """Obtain the best available transcript before considering STT fallback."""
    _run_source_command(
        source,
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
        help="Keep future generated media; local source files are read in place.",
    ),
) -> None:
    """Run STT explicitly on local media without reading transcript sidecars."""
    _run_source_command(
        source,
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


app.command(
    name="ingest",
    help="Obtain the best available local transcript; STT is used only as a fallback.",
)(_ingest)
app.command(
    name="show",
    help="Display a transcript already stored in the local cache.",
)(_show)
app.command(
    name="transcribe",
    help="Explicitly run STT on a supported local media file; transcript sidecars are ignored.",
)(_transcribe)

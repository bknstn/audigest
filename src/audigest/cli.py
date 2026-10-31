"""Command-line interface for Audigest."""

import typer

from audigest.errors import (
    ConfigurationError,
    MissingDependencyError,
    ProviderError,
    SourceResolutionError,
    StorageError,
)
from audigest.renderers import render_json
from audigest.service import transcribe as transcribe_source

app = typer.Typer(
    name="audigest",
    help="Reuse local transcripts, captions, and text; transcribe media when needed.",
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


@app.command()
def transcribe(
    source: str = typer.Argument(
        ...,
        help=(
            "Local canonical JSON, UTF-8 text, SRT/VTT captions, or audio/video media path. "
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
    """Acquire a local transcript, caption, text file, or media transcript."""
    typer.echo("Resolving source and checking the transcript cache...", err=True)
    try:
        result = transcribe_source(
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

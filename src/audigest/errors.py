"""Stable, user-facing error categories for the CLI."""


class AudigestError(Exception):
    """Base class for expected Audigest failures."""

    exit_code = 2


class ConfigurationError(AudigestError):
    """Invalid command-line, environment, or configuration-file value."""

    exit_code = 2


class SourceResolutionError(AudigestError):
    """A supplied source cannot be resolved by an available source adapter."""

    exit_code = 3


class RetrievalError(AudigestError):
    """A remote transcript or caption could not be retrieved safely."""

    exit_code = 4


class ProviderError(AudigestError):
    """A requested transcription provider is unavailable or failed."""

    exit_code = 5


class ProcessorError(AudigestError):
    """A requested transcript processor is unavailable or failed."""

    exit_code = 6


class MissingDependencyError(AudigestError):
    """A required local runtime dependency is missing."""

    exit_code = 7


class StorageError(AudigestError):
    """A transcript cache could not be read or written safely."""

    # Storage has no dedicated exit family in the initial CLI contract.
    exit_code = 2

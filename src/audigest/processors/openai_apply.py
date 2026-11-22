"""OpenAI application provider entry point."""

from typing import Protocol

from audigest.models import Transcript
from audigest.processors.application import ApplicationResponse
from audigest.processors.llm import OpenAIProcessorProvider as OpenAIApplicationProvider
from audigest.processors.project_context import ProjectContext


class ApplicationProvider(Protocol):
    """Provider protocol for application recommendations."""

    name: str
    model: str

    def apply(self, transcript: Transcript, project: ProjectContext) -> ApplicationResponse:
        """Return structured recommendations from a stored transcript and selected files."""


__all__ = ["ApplicationProvider", "OpenAIApplicationProvider"]

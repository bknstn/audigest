"""OpenAI summary provider entry point."""

from audigest.processors.llm import OPENAI_BASE_URL as BASE_URL
from audigest.processors.llm import TIMEOUT_SECONDS
from audigest.processors.llm import OpenAIProcessorProvider as OpenAISummaryProvider
from audigest.processors.llm_config import DEFAULT_MODELS

DEFAULT_MODEL = DEFAULT_MODELS["openai"]

__all__ = ["BASE_URL", "DEFAULT_MODEL", "TIMEOUT_SECONDS", "OpenAISummaryProvider"]

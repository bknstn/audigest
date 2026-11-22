"""OpenAI English-learning provider entry point."""

from audigest.processors.english import MAX_OUTPUT_TOKENS as MAX_OUTPUT_TOKENS
from audigest.processors.english import REASONING_EFFORT as REASONING_EFFORT
from audigest.processors.llm import OpenAIProcessorProvider as OpenAIEnglishProvider
from audigest.processors.openai import BASE_URL, DEFAULT_MODEL, TIMEOUT_SECONDS

__all__ = ["BASE_URL", "DEFAULT_MODEL", "TIMEOUT_SECONDS", "OpenAIEnglishProvider"]

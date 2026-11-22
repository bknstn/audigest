"""LLM selection and the exact provider/model combinations eligible for cache reuse."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, cast

from audigest.errors import ConfigurationError

LLMProviderName = Literal["openai", "anthropic"]
LLMSelection = Literal["auto", "openai", "anthropic"]
LEGACY_OPENAI_MODEL = "gpt-6-luna"
DEFAULT_MODELS: dict[LLMProviderName, str] = {
    "openai": "gpt-5.4-mini",
    "anthropic": "claude-haiku-5-5",
}
REASONING_EFFORT: Literal["low"] = "low"
_MODEL_NAME = re.compile(r"[^\s\x00]{1,128}\Z")


def select_llm(
    provider: str | None, model: str | None, environ: Mapping[str, str]
) -> tuple[LLMSelection, str | None]:
    selected = provider if provider is not None else environ.get("AUDIGEST_LLM_PROVIDER", "auto")
    if selected != "auto" and selected != "openai" and selected != "anthropic":
        raise ConfigurationError("LLM provider must be one of: auto, openai, anthropic")
    model = model if model is not None else environ.get("AUDIGEST_LLM_MODEL")
    if model is not None and (not isinstance(model, str) or _MODEL_NAME.fullmatch(model) is None):
        raise ConfigurationError("LLM model must be a non-empty name of at most 128 characters")
    return cast(LLMSelection, selected), model


def cache_targets(provider: LLMSelection, model: str | None) -> list[tuple[LLMProviderName, str]]:
    providers: tuple[LLMProviderName, ...] = (
        ("openai", "anthropic") if provider == "auto" else (provider,)
    )
    targets = []
    for backend in providers:
        targets.append((backend, model if model is not None else DEFAULT_MODELS[backend]))
        if backend == "openai" and model is None:
            targets.append((backend, LEGACY_OPENAI_MODEL))
    return targets


def reasoning_effort_for(provider: LLMProviderName) -> Literal["low"] | None:
    return REASONING_EFFORT if provider == "openai" else None

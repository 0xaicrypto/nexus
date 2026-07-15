"""LLM client module — unified interface for Gemini, OpenAI, Claude, and Kimi."""

from .client import LLMClient
from .providers import (
    KIMI_DEFAULT_BASE_URL,
    KIMI_DEFAULT_MODEL,
    LLMProvider,
    resolve_kimi_api_key,
    resolve_kimi_base_url,
)

__all__ = [
    "LLMClient",
    "LLMProvider",
    "KIMI_DEFAULT_BASE_URL",
    "KIMI_DEFAULT_MODEL",
    "resolve_kimi_api_key",
    "resolve_kimi_base_url",
]

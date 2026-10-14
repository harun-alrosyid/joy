"""Provider settings resolved from environment variables.

Joy talks to OpenAI directly by default, or to any OpenAI-compatible gateway
such as 9Router. ``JOY_API`` picks the wire format, while the remaining values
fall back to the standard ``OPENAI_*`` names so existing setups keep working.
"""

import os
from dataclasses import dataclass

DEFAULT_MODEL = "gpt-5.6-luna"
# Reasoning models can stay silent for a while before the first token, so the
# per-read timeout is generous; override with JOY_TIMEOUT when needed.
DEFAULT_TIMEOUT = 300.0
RESPONSES = "responses"
CHAT = "chat"
DEFAULT_API = RESPONSES
API_KINDS = (RESPONSES, CHAT)
_CHAT_ALIASES = {"chat", "chat_completions", "chat-completions", "completions"}


def _first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError("JOY_TIMEOUT must be a number of seconds.")
    if seconds <= 0:
        raise ValueError("JOY_TIMEOUT must be positive.")
    return seconds



def _api_kind(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in _CHAT_ALIASES:
        return CHAT
    if normalized == RESPONSES:
        return RESPONSES
    raise ValueError(f"JOY_API must be one of {', '.join(API_KINDS)}.")


@dataclass(frozen=True)
class Settings:
    """Where and how to reach the chat API."""

    api: str = DEFAULT_API
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api=_api_kind(_first("JOY_API", default=DEFAULT_API)),
            model=_first("JOY_MODEL", default=DEFAULT_MODEL),
            api_key=_first("JOY_API_KEY", "OPENAI_API_KEY"),
            base_url=_first("JOY_BASE_URL", "OPENAI_BASE_URL") or None,
            timeout=_timeout(_first("JOY_TIMEOUT", default=str(DEFAULT_TIMEOUT))),
        )

    @property
    def is_responses(self) -> bool:
        return self.api == RESPONSES

    def client_kwargs(self) -> dict:
        kwargs = {"api_key": self.api_key or "not-needed", "timeout": self.timeout,
                  "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs

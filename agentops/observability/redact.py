"""Redaction for anything the trace/detail APIs return (ADR-042).

Two passes over arbitrary JSON-like data:
- by key: a value whose key looks like a credential is replaced wholesale
- by pattern: credential-shaped substrings inside any string are masked
Then strings are truncated. This is the only path trace payloads take to the client.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|apikey|authori[sz]ation|cookie|credential"
    r"|private[_-]?key|session[_-]?id|dsn)",
    re.IGNORECASE,
)
# Keys that contain a secret-ish word but are counters/ids, not credentials.
_SAFE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "max_tokens",
        "used_tokens",
        "reserved_tokens",
        "tokens",
        "max_output_tokens",
        "tool_call_id",
        "llm_call_id",
        "token_count",
    }
)
_SECRET_VALUE = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}"  # Anthropic keys
    r"|sk-[A-Za-z0-9]{20,}"  # generic sk- keys
    r"|tvly-[A-Za-z0-9_\-]{8,}"  # Tavily keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub tokens
    r"|AKIA[0-9A-Z]{16}"  # AWS access key ids
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=\-]{16,}"  # bearer credentials
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"  # JWTs
    r"|(postgres(ql)?|mysql|redis|amqp)(\+\w+)?://[^\s:@/]+:[^\s@/]+@)"  # URLs with passwords
)


def is_secret_key(key: str) -> bool:
    return key.lower() not in _SAFE_KEYS and bool(_SECRET_KEY.search(key))


def mask(text: str) -> str:
    return _SECRET_VALUE.sub(REDACTED, text)


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}… [truncated {len(text) - max_chars} chars]"


def redact(value: Any, *, max_chars: int = 2000, _depth: int = 0) -> Any:
    """Return a redacted, truncated copy of `value` (dicts, lists, scalars)."""
    if _depth > 12:
        return "[depth limit]"
    if isinstance(value, dict):
        return {
            str(k): (
                REDACTED
                if is_secret_key(str(k)) and v not in (None, "")
                else redact(v, max_chars=max_chars, _depth=_depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [redact(v, max_chars=max_chars, _depth=_depth + 1) for v in value[:200]]
        if len(value) > 200:
            items.append(f"[{len(value) - 200} more items]")
        return items
    if isinstance(value, str):
        return truncate(mask(value), max_chars)
    return value

"""Untrusted-data framing for tool results (ADR-034).

Everything a tool returns (web pages, search results, file contents, SQL rows, MCP output,
program output) is data, not instructions. It reaches the model only inside a delimited block;
the delimiter can't be closed from inside the content, and the content is length-capped.
"""

from __future__ import annotations

import re

MAX_RESULT_CHARS = 8_000
_CLOSE = re.compile(r"</\s*tool_result", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text: str) -> str:
    return _CONTROL.sub("", text)


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated {len(text) - limit} characters]"


def frame(tool: str, status: str, text: str, *, limit: int = MAX_RESULT_CHARS) -> str:
    body = _CLOSE.sub("<\\/tool_result", truncate(sanitize(text), limit))
    return (
        f'<tool_result tool="{tool}" status="{status}" trust="untrusted">\n{body}\n</tool_result>'
    )

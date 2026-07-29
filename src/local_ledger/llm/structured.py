"""Structured output: bounded-retry JSON extraction over an injectable completer.

The completer is a ``Callable[[list[dict], str], str]`` returning raw model text.
In production, ``make_openai_completer`` wraps an OpenAI-compatible client. The
retry loop parses + validates against a Pydantic model and raises a typed
``StructuredOutputError`` after the budget is exhausted - never a raw
``JSONDecodeError`` or ``ValidationError``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

Completer = Callable[[list[dict], str], str]
T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


class StructuredOutputError(Exception):
    pass


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


def make_openai_completer(client: object) -> Completer:
    """Wrap an OpenAI-compatible client into the completer signature."""

    def completer(messages: list[dict], model: str) -> str:
        resp = client.chat.completions.create(  # type: ignore[attr-defined]
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""

    return completer


def extract(
    completer: Completer,
    schema: type[T],
    messages: list[dict],
    model: str,
    max_retries: int = 2,
) -> T:
    total_attempts = max_retries + 1
    last_exc: Exception | None = None
    for _ in range(total_attempts):
        raw = completer(messages, model)
        try:
            data = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            last_exc = exc
            continue
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            last_exc = exc
            continue
    raise StructuredOutputError(
        f"failed to produce {schema.__name__} after {total_attempts} attempt(s): {last_exc}"
    )

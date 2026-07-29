"""Structured output tests: bounded retry + typed validation, injectable completer."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from local_ledger.llm.structured import (
    StructuredOutputError,
    _strip_fences,
    extract,
    make_openai_completer,
)


class Cat(BaseModel):
    name: str
    age: int


def _completer_returning(payload):  # type: ignore[no-untyped-def]
    def _c(_messages, _model):
        return payload
    return _c


def _tracking_completer(payload, record):  # type: ignore[no-untyped-def]
    def _c(_messages, _model):
        record["calls"] += 1
        return payload
    return _c


def test_extract_valid_json_returns_typed_object() -> None:
    obj = extract(_completer_returning('{"name": "Mittens", "age": 4}'), Cat, [], "m")
    assert obj.name == "Mittens"
    assert obj.age == 4


def test_extract_strips_markdown_fences() -> None:
    payload = "```json\n" + json.dumps({"name": "Cat", "age": 2}) + "\n```"
    obj = extract(_completer_returning(payload), Cat, [], "m")
    assert obj.age == 2


def test_strip_fences_helper() -> None:
    assert _strip_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_fences('{"a":1}') == '{"a":1}'
    assert _strip_fences('```\n{"a":1}\n```') == '{"a":1}'


def test_garbage_json_raises_structured_error_not_jsondecodeerror() -> None:
    with pytest.raises(StructuredOutputError):
        extract(_completer_returning("not json at all"), Cat, [], "m", max_retries=1)


def test_invalid_schema_retries_then_raises_structured_error() -> None:
    """Valid JSON but wrong shape: caught as ValidationError, retried, then typed error."""
    with pytest.raises(StructuredOutputError):
        extract(_completer_returning('{"name": "Cat"}'), Cat, [], "m", max_retries=1)


def test_persistent_failure_calls_completer_max_times() -> None:
    record = {"calls": 0}
    with pytest.raises(StructuredOutputError):
        extract(_tracking_completer("garbage", record), Cat, [], "m", max_retries=2)
    # initial attempt + 2 retries = 3 total calls
    assert record["calls"] == 3


def test_retries_then_succeeds() -> None:
    payloads = iter(["nope", '{"name": "X", "age": 1}'])
    record = {"calls": 0}

    def comp(_m, _model):
        record["calls"] += 1
        return next(payloads)

    obj = extract(comp, Cat, [], "m", max_retries=2)
    assert obj.name == "X"
    assert record["calls"] == 2


def test_make_openai_completer_callable_factory() -> None:
    """The production completer factory builds a callable from an OpenAI client."""

    class FakeResp:
        class FakeChoice:
            class FakeMsg:
                content = '{"name": "Z", "age": 9}'
            message = FakeMsg()
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **_kw):
            return FakeResp()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    completer = make_openai_completer(FakeClient())
    assert callable(completer)
    assert completer([], "m") == '{"name": "Z", "age": 9}'

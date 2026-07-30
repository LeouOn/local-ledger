"""Trend narrative tests: typed output, cloud blocked, error handling."""

from __future__ import annotations

import json

import pytest

from local_ledger.analysis.multimonth import MultiMonthCategory, MultiMonthPayload
from local_ledger.analysis.narrative import TrendNarrative, generate_trend_narrative
from local_ledger.llm.providers import CloudCallBlocked
from local_ledger.llm.structured import StructuredOutputError


def _fake_payload() -> MultiMonthPayload:
    return MultiMonthPayload(
        months=["2026-01", "2026-02", "2026-03"],
        total_income_cents=300000,
        total_spent_cents=100000,
        categories={
            1: MultiMonthCategory(
                months=[25000, 35000, 40000],
                income_months=[100000, 100000, 100000],
                mom_delta_pct=60.0,
                trend_direction="up",
            ),
        },
        truncated=False,
    )


def _completer_returning(payload):  # type: ignore[no-untyped-def]
    def _c(_messages, _model):
        return payload
    return _c


def test_generate_trend_narrative_returns_typed_output() -> None:
    fake_response = json.dumps(
        {
            "summary": "Food spending rose 60% over 3 months.",
            "key_trends": ["Food is the fastest-growing category"],
            "projections": ["At current rate, Food will reach $500/mo by June"],
            "warnings": ["Food trend is unsustainable"],
        }
    )
    result = generate_trend_narrative(
        _fake_payload(),
        completer=_completer_returning(fake_response),
        model="m",
    )
    assert isinstance(result, TrendNarrative)
    assert "rose 60%" in result.summary
    assert len(result.key_trends) == 1
    assert len(result.projections) == 1
    assert len(result.warnings) == 1


def test_generate_trend_narrative_with_empty_lists() -> None:
    fake_response = json.dumps(
        {
            "summary": "Flat period.",
            "key_trends": [],
            "projections": [],
            "warnings": [],
        }
    )
    result = generate_trend_narrative(
        _fake_payload(),
        completer=_completer_returning(fake_response),
        model="m",
    )
    assert result.key_trends == []
    assert result.projections == []
    assert result.warnings == []


def test_cloud_path_blocked_without_consent() -> None:
    with pytest.raises(CloudCallBlocked):
        generate_trend_narrative(
            _fake_payload(),
            completer=None,  # type: ignore[arg-type]
            model="m",
            provider="cloud",
            consent_token=None,
            settings=None,  # type: ignore[arg-type]
        )


def test_garbage_response_raises_structured_error() -> None:
    with pytest.raises(StructuredOutputError):
        generate_trend_narrative(
            _fake_payload(),
            completer=_completer_returning("not json"),
            model="m",
            max_retries=1,
        )


def test_missing_fields_default_to_empty_lists() -> None:
    fake_response = json.dumps({"summary": "Just a summary."})
    result = generate_trend_narrative(
        _fake_payload(),
        completer=_completer_returning(fake_response),
        model="m",
    )
    assert result.summary == "Just a summary."
    assert result.key_trends == []
    assert result.projections == []
    assert result.warnings == []

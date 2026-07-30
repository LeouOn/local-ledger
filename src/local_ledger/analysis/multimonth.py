"""Multi-month payload builder: aggregates N months into a token-capped typed payload."""

from __future__ import annotations

import json

import tiktoken
from pydantic import BaseModel

from local_ledger.ledger.trends import MonthSummary

TOKEN_CAP = 4096
_ENCODER_NAME = "cl100k_base"
FLAT_THRESHOLD_PCT = 10.0
_CHARS_PER_TOKEN = 4


class MultiMonthCategory(BaseModel):
    months: list[int]  # spent per month, oldest first
    income_months: list[int]  # income per month
    mom_delta_pct: float
    trend_direction: str  # up | down | flat


class MultiMonthPayload(BaseModel):
    months: list[str]
    total_income_cents: int
    total_spent_cents: int
    categories: dict[int, MultiMonthCategory]
    truncated: bool = False


def _encoder():  # type: ignore[no-untyped-def]
    try:
        enc = tiktoken.get_encoding(_ENCODER_NAME)
        return lambda text: len(enc.encode(text))
    except Exception:
        return lambda text: max(1, len(text) // _CHARS_PER_TOKEN)


def _render(payload: MultiMonthPayload) -> str:
    data = payload.model_dump()
    data["categories"] = {str(k): v for k, v in data["categories"].items()}
    return json.dumps(data, sort_keys=True)


def build_multimonth_payload(
    months: list[str],
    summaries: list[MonthSummary],
) -> MultiMonthPayload:
    if len(months) != len(summaries):
        raise ValueError("months and summaries must have the same length")

    all_cats: set[int] = set()
    for s in summaries:
        for c in s.per_category:
            all_cats.add(c.category_id)

    categories: dict[int, MultiMonthCategory] = {}
    for cat_id in sorted(all_cats):
        spent_per_month = []
        income_per_month = []
        for s in summaries:
            cat = next((c for c in s.per_category if c.category_id == cat_id), None)
            spent_per_month.append(cat.spent if cat else 0)
            income_per_month.append(s.income_cents)

        first, last = spent_per_month[0], spent_per_month[-1]
        mom_delta = (0.0 if last == 0 else 100.0) if first == 0 else ((last - first) / first) * 100
        if abs(mom_delta) < FLAT_THRESHOLD_PCT:
            direction = "flat"
        elif mom_delta > 0:
            direction = "up"
        else:
            direction = "down"

        categories[cat_id] = MultiMonthCategory(
            months=spent_per_month,
            income_months=income_per_month,
            mom_delta_pct=round(mom_delta, 1),
            trend_direction=direction,
        )

    payload = MultiMonthPayload(
        months=list(months),
        total_income_cents=sum(s.income_cents for s in summaries),
        total_spent_cents=sum(s.spent_cents for s in summaries),
        categories=categories,
        truncated=False,
    )

    count = _encoder()
    while (
        payload.categories
        and len(payload.months) > 1
        and count(_render(payload)) > TOKEN_CAP
    ):
        payload.months = payload.months[1:]
        for cat in payload.categories.values():
            cat.months = cat.months[1:]
            cat.income_months = cat.income_months[1:]
        payload.truncated = True

    return payload


def to_prompt(payload: MultiMonthPayload) -> str:
    return _render(payload)

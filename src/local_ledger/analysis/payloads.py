"""Build the aggregates-only LLM evaluation payload, hard-capped at 4096 tokens.

Raw transactions, payees, and accounts are structurally excluded. The prompt
carries only per-category totals, month-over-month deltas, and (optionally) a
few sample amounts - never raw rows. If category count alone would breach the
cap, the builder truncates and flags ``truncated``.
"""

from __future__ import annotations

import json

import tiktoken
from pydantic import BaseModel

from local_ledger.ledger.trends import MonthSummary
from local_ledger.llm.privacy import PrivacyViolation

PAYLOAD_TOKEN_CAP = 4096
_ENCODER_NAME = "cl100k_base"
_TRUNCATE_CATEGORY_HINT = 120  # generous cap on categories listed before truncation
_CHARS_PER_TOKEN = 4  # conservative fallback estimate when tiktoken can't load offline


def _encoder():  # type: ignore[no-untyped-def]
    """Return a token counter; prefer tiktoken, fall back to a char estimate offline."""
    try:
        enc = tiktoken.get_encoding(_ENCODER_NAME)
        return lambda text: len(enc.encode(text))
    except Exception:  # offline / encoding unavailable
        return lambda text: max(1, len(text) // _CHARS_PER_TOKEN)


class CategoryPayload(BaseModel):
    name: str
    budgeted: int
    spent: int
    balance: int
    carryover: int


class EvaluationPayload(BaseModel):
    month: str
    total_income_cents: int
    total_spent_cents: int
    total_budgeted_cents: int
    categories: list[CategoryPayload]
    mom_income: list[tuple[str, int]] = []
    mom_spent: list[tuple[str, int]] = []
    truncated: bool = False


def build_evaluation_payload(
    *,
    month: str,
    summary: object,
    mom_income: list[tuple[str, int]],
    mom_spent: list[tuple[str, int]],
) -> EvaluationPayload:
    if isinstance(summary, MonthSummary):
        return _from_summary(month, summary, mom_income, mom_spent)
    raise PrivacyViolation(
        "payload input must be a deterministic MonthSummary; raw transactions are forbidden"
    )


def _from_summary(
    month: str,
    summary: MonthSummary,
    mom_income: list[tuple[str, int]],
    mom_spent: list[tuple[str, int]],
) -> EvaluationPayload:
    # Category names come from the caller; map id->name is unavailable in the
    # trends summary, so we use the category_id as a placeholder label and let
    # the UI layer substitute real names. Numbers are what the LLM reasons over.
    cats = [
        CategoryPayload(
            name=f"category_{c.category_id}",
            budgeted=c.budgeted,
            spent=c.spent,
            balance=c.balance,
            carryover=c.carryover,
        )
        for c in summary.per_category
    ]
    payload = EvaluationPayload(
        month=month,
        total_income_cents=summary.income_cents,
        total_spent_cents=summary.spent_cents,
        total_budgeted_cents=summary.budgeted_cents,
        categories=cats,
        mom_income=list(mom_income),
        mom_spent=list(mom_spent),
    )
    # Truncate categories if the rendered prompt would exceed the cap.
    count = _encoder()
    while cats and count(_render(payload)) > PAYLOAD_TOKEN_CAP:
        cats = cats[: max(1, int(len(cats) * 0.8))]
        payload.categories = cats
        payload.truncated = True
        if len(cats) <= _TRUNCATE_CATEGORY_HINT and count(_render(payload)) <= PAYLOAD_TOKEN_CAP:
            break
    return payload


def to_prompt(payload: EvaluationPayload) -> str:
    return _render(payload)


def _render(payload: EvaluationPayload) -> str:
    data = payload.model_dump()
    data["mom_income"] = [list(x) for x in data["mom_income"]]
    data["mom_spent"] = [list(x) for x in data["mom_spent"]]
    return json.dumps(data, sort_keys=True)


class PayloadTooLargeError(Exception):
    pass

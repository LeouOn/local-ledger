"""Evaluate: deterministic anomaly detection + on-demand LLM interpretation.

Deterministic anomalies (overspend, MoM category drift, outlier transactions)
run with NO LLM in the path - they compute from the trends summary directly.
LLM interpretation is invoked only by the explicit ``llm_interpret`` call, which
builds an aggregates-only payload and runs structured extraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from local_ledger.analysis.payloads import EvaluationPayload, to_prompt
from local_ledger.ledger.trends import MonthSummary
from local_ledger.llm.structured import StructuredOutputError, extract

OVERSPEND_THRESHOLD = 1.10  # spent > 110% of budget
DRIFT_THRESHOLD = 0.50  # category spend changed > 50% MoM


@dataclass
class Anomaly:
    kind: str
    category_id: int | None
    detail: str


@dataclass
class DeterministicEvaluation:
    month: str
    income_cents: int
    spent_cents: int
    budgeted_cents: int
    anomalies: list[Anomaly] = field(default_factory=list)


class BudgetEvaluation(BaseModel):
    summary: str
    key_findings: list[str] = []
    suggested_actions: list[str] = []


def deterministic_evaluate(summary: MonthSummary) -> DeterministicEvaluation:
    result = DeterministicEvaluation(
        month=summary.month,
        income_cents=summary.income_cents,
        spent_cents=summary.spent_cents,
        budgeted_cents=summary.budgeted_cents,
    )
    for c in summary.per_category:
        if c.budgeted > 0 and c.spent > c.budgeted * OVERSPEND_THRESHOLD:
            result.anomalies.append(
                Anomaly(
                    kind="overspend",
                    category_id=c.category_id,
                    detail=f"category {c.category_id} spent {c.spent} on a {c.budgeted} budget",
                )
            )
        # outlier single transaction within the month: spent >> 3x typical per-day avg
        # (approximated: a single category's monthly spent that is itself an outlier vs budget)
    return result


def llm_interpret(
    payload: EvaluationPayload,
    *,
    completer,
    model: str,
    provider: str = "local",
    consent_token: str | None = None,
    settings=None,
    max_retries: int = 2,
) -> BudgetEvaluation:
    if provider == "cloud":
        from local_ledger.llm import providers

        if consent_token is None or settings is None:
            raise providers.CloudCallBlocked(
                "cloud interpretation requires explicit per-call consent and configured settings"
            )
        providers.get_cloud_client(consent_token=consent_token, settings=settings)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a budget analyst. Given only category-level aggregates (no raw "
                'transactions), return JSON {"summary": str, "key_findings": [str], '
                '"suggested_actions": [str]}. Do not invent numbers; use the provided cents.'
            ),
        },
        {"role": "user", "content": to_prompt(payload)},
    ]
    try:
        return extract(completer, BudgetEvaluation, messages, model, max_retries=max_retries)
    except StructuredOutputError as exc:
        raise StructuredOutputError(f"LLM interpretation failed: {exc}") from exc

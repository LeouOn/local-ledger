"""Evaluate: deterministic anomaly detection + on-demand LLM interpretation.

Deterministic anomalies (overspend, MoM category drift, outlier transactions)
run with NO LLM in the path - they compute from the trends summary directly.
LLM interpretation is invoked only by the explicit ``llm_interpret`` call, which
builds an aggregates-only payload and runs structured extraction.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from pydantic import BaseModel

from local_ledger.analysis.payloads import EvaluationPayload, to_prompt
from local_ledger.ledger.trends import MonthSummary
from local_ledger.llm.structured import StructuredOutputError, extract

OVERSPEND_THRESHOLD = 1.10
DRIFT_THRESHOLD = 0.50
OUTLIER_STDEV = 3.0


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


def deterministic_evaluate(
    summary: MonthSummary,
    *,
    prior_summary: MonthSummary | None = None,
    transactions: list | None = None,
) -> DeterministicEvaluation:
    result = DeterministicEvaluation(
        month=summary.month,
        income_cents=summary.income_cents,
        spent_cents=summary.spent_cents,
        budgeted_cents=summary.budgeted_cents,
    )

    prior_spent = {c.category_id: c.spent for c in prior_summary.per_category} if prior_summary else {}

    for c in summary.per_category:
        if c.budgeted > 0 and c.spent > c.budgeted * OVERSPEND_THRESHOLD:
            result.anomalies.append(
                Anomaly(
                    kind="overspend",
                    category_id=c.category_id,
                    detail=f"{c.name} spent {c.spent} on a {c.budgeted} budget",
                )
            )

        if c.category_id in prior_spent and prior_spent[c.category_id] > 0:
            change = abs(c.spent - prior_spent[c.category_id]) / prior_spent[c.category_id]
            if change > DRIFT_THRESHOLD:
                direction = "up" if c.spent > prior_spent[c.category_id] else "down"
                result.anomalies.append(
                    Anomaly(
                        kind="drift",
                        category_id=c.category_id,
                        detail=f"{c.name} spend {direction} {change:.0%} MoM "
                        f"({prior_spent[c.category_id]} -> {c.spent})",
                    )
                )

    if transactions:
        by_cat: dict[int | None, list[int]] = {}
        for tx in transactions:
            m = tx.date[:7]
            if m != summary.month:
                continue
            by_cat.setdefault(tx.category_id, []).append(abs(tx.amount_cents))
        for cat_id, amounts in by_cat.items():
            if len(amounts) < 2:
                continue
            mean = statistics.mean(amounts)
            stdev = statistics.stdev(amounts)
            if stdev == 0:
                continue
            for amt in amounts:
                if (amt - mean) / stdev > OUTLIER_STDEV:
                    cat_name = next(
                        (c.name for c in summary.per_category if c.category_id == cat_id),
                        f"category_{cat_id}",
                    )
                    result.anomalies.append(
                        Anomaly(
                            kind="outlier",
                            category_id=cat_id,
                            detail=f"{cat_name} has a single transaction of {amt} "
                            f"(mean {mean:.0f}, stdev {stdev:.0f})",
                        )
                    )
                    break

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

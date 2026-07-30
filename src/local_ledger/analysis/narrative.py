"""Trend narrative evaluation: LLM interprets multi-month aggregates into a narrative."""

from __future__ import annotations

from pydantic import BaseModel

from local_ledger.analysis.multimonth import MultiMonthPayload, to_prompt
from local_ledger.llm.structured import StructuredOutputError, extract


class TrendNarrative(BaseModel):
    summary: str
    key_trends: list[str] = []
    projections: list[str] = []
    warnings: list[str] = []


_SYSTEM_PROMPT = (
    "You are a financial trend analyst. Given ONLY category-level aggregate data "
    "across multiple months (no raw transactions), write a narrative as JSON "
    '{"summary": str, "key_trends": [str], "projections": [str], "warnings": [str]}. '
    "Base all numbers strictly on the provided cents values. Identify rising/falling "
    "categories, project trajectory, and flag risks."
)


def generate_trend_narrative(
    payload: MultiMonthPayload,
    *,
    completer,
    model: str,
    provider: str = "local",
    consent_token: str | None = None,
    settings=None,
    max_retries: int = 2,
) -> TrendNarrative:
    if provider == "cloud":
        from local_ledger.llm import providers

        if consent_token is None or settings is None:
            raise providers.CloudCallBlocked(
                "cloud narrative requires explicit per-call consent and configured settings"
            )
        providers.get_cloud_client(consent_token=consent_token, settings=settings)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": to_prompt(payload)},
    ]
    try:
        return extract(completer, TrendNarrative, messages, model, max_retries=max_retries)
    except StructuredOutputError as exc:
        raise StructuredOutputError(f"Trend narrative failed: {exc}") from exc

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


def narrative_messages(payload: MultiMonthPayload) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": to_prompt(payload)},
    ]


def build_narrative_request(payload: MultiMonthPayload, *, model: str) -> dict:
    """The exact cloud request body a narrative call will transmit."""
    from local_ledger.llm.cloud import build_chat_request

    return build_chat_request(model=model, messages=narrative_messages(payload))


def generate_trend_narrative(
    payload: MultiMonthPayload,
    *,
    completer=None,
    model: str = "",
    provider: str = "local",
    consent_token: str | None = None,
    settings=None,
    session=None,
    max_retries: int = 2,
) -> TrendNarrative:
    if provider == "cloud":
        from local_ledger.llm import providers
        from local_ledger.llm.cloud import make_cloud_completer
        from local_ledger.llm.privacy import egress_attempt_auditor

        if consent_token is None or settings is None:
            raise providers.CloudCallBlocked(
                "cloud narrative requires explicit per-call consent and configured settings"
            )
        if session is None:
            raise providers.CloudCallBlocked(
                "cloud narrative requires a session for egress auditing"
            )
        # Resolve the model that will ACTUALLY be sent before verifying
        # consent, so the token is bound to it; an unconsented override is
        # rejected here rather than transmitted.
        model = providers.resolve_cloud_model(settings, model)
        request = build_narrative_request(payload, model=model)
        client = providers.get_cloud_client(
            consent_token=consent_token,
            settings=settings,
            payload=request,
            model=model,
        )
        audit = egress_attempt_auditor(
            session,
            provider=settings.cloud_base_url or "cloud",
            model=model,
            request=request,
        )
        completer = make_cloud_completer(client, request, audit=audit)
    if completer is None:
        raise ValueError("generate_trend_narrative requires a completer (or a cloud consent token)")
    messages = narrative_messages(payload)
    try:
        return extract(completer, TrendNarrative, messages, model, max_retries=max_retries)
    except StructuredOutputError as exc:
        raise StructuredOutputError(f"Trend narrative failed: {exc}") from exc

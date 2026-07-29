"""Provider abstraction over local (Ollama) and cloud OpenAI-compatible endpoints.

One client shape (``openai.OpenAI``) covers both; the only difference is the
``base_url``. Local access is gated by the Ollama degradation contract; cloud
access is gated by explicit per-call consent (wired in todo 10; this module
raises ``CloudCallBlocked`` until then).
"""

from __future__ import annotations

from collections.abc import Callable

from openai import OpenAI
from sqlmodel import Session

from local_ledger.llm.ollama import (
    DEFAULT_BASE_URL,
    GUIDANCE,
    OllamaState,
    detect_state,
)
from local_ledger.llm.ollama import _default_list_models as default_list_models
from local_ledger.models import ProviderSettings


class CloudCallBlocked(Exception):
    """Raised when a cloud call is attempted without per-call consent."""


def _ollama_root(openai_base_url: str) -> str:
    """Strip the OpenAI ``/v1`` suffix to get the raw Ollama API root."""
    return openai_base_url.rstrip("/").removesuffix("/v1")


def load_settings(session: Session) -> ProviderSettings:
    row = session.get(ProviderSettings, 1)
    if row is None:
        row = ProviderSettings(id=1)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def save_settings(session: Session, **fields: object) -> ProviderSettings:
    row = load_settings(session)
    for key, value in fields.items():
        setattr(row, key, value)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_local_client(
    settings: ProviderSettings,
    *,
    state_detector: Callable[..., OllamaState] = detect_state,
) -> OpenAI:
    require_local_ready(settings, state_detector=state_detector)
    return OpenAI(base_url=settings.local_base_url, api_key="ollama")


def require_local_ready(
    settings: ProviderSettings,
    *,
    state_detector: Callable[..., OllamaState] = detect_state,
) -> None:
    root = _ollama_root(settings.local_base_url)
    state = state_detector(root, settings.local_model)
    if state != OllamaState.READY:
        from local_ledger.llm.ollama import LLMUnavailable

        raise LLMUnavailable(state, GUIDANCE[state])


def get_cloud_client(consent_token: str | None, settings: ProviderSettings) -> OpenAI:
    raise CloudCallBlocked(
        "cloud provider access requires explicit per-call consent (consent issuance wired in todo 10)"
    )


def list_local_models(
    settings: ProviderSettings,
    *,
    lister: Callable[[str, float], list[str]] = default_list_models,
    timeout: float = 1.0,
) -> list[str]:
    root = _ollama_root(settings.local_base_url) or DEFAULT_BASE_URL
    return lister(root, timeout)

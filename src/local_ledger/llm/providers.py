"""Provider abstraction over local (Ollama) and cloud OpenAI-compatible endpoints.

One client shape (``openai.OpenAI``) covers both; the only difference is the
``base_url``. Local access is gated by the Ollama degradation contract AND a
loopback check — the consent-free local path must never point off-machine.
Cloud access is gated by explicit per-call consent bound to the configured
endpoint, model, and exact payload.
"""

from __future__ import annotations

import ipaddress
import os
import urllib.parse
from collections.abc import Callable

from openai import OpenAI
from sqlmodel import Session

from local_ledger.llm.ollama import (
    DEFAULT_BASE_URL,
    OllamaState,
    detect_state,
    guidance_for,
)
from local_ledger.llm.ollama import _default_list_models as default_list_models
from local_ledger.models import ProviderSettings


class CloudCallBlocked(Exception):
    """Raised when a cloud call is attempted without per-call consent."""


class NonLocalEndpointError(Exception):
    """Raised when the 'local' provider is configured with a non-loopback URL.

    The local path is consent-free by design, so it must never be pointed at
    a remote host — that would silently bypass the cloud privacy gate.
    """


class ConsentDenied(Exception):
    """Re-exported alias used by the privacy module's assertion path."""


from local_ledger.llm.privacy import CloudPayload, assert_consent  # noqa: E402


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


def assert_local_endpoint(base_url: str) -> None:
    """Refuse any 'local' base URL that is not a loopback address."""
    host = urllib.parse.urlsplit(base_url).hostname if base_url else None
    if host == "localhost":
        return
    try:
        if host and ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise NonLocalEndpointError(
        f"local base URL must be a loopback address (localhost / 127.0.0.1 / ::1), "
        f"got {base_url!r}; point remote providers at the consent-gated cloud "
        "configuration instead"
    )


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
    assert_local_endpoint(settings.local_base_url)
    root = _ollama_root(settings.local_base_url)
    state = state_detector(root, settings.local_model)
    if state != OllamaState.READY:
        from local_ledger.llm.ollama import LLMUnavailable

        raise LLMUnavailable(state, guidance_for(state, settings.local_profile))


def resolve_cloud_model(settings: ProviderSettings, model: str = "") -> str:
    """The model a cloud call will actually use: explicit override wins."""
    return (model or settings.cloud_model or "gpt-4o-mini").strip()


def cloud_destination(settings: ProviderSettings, *, model: str | None = None) -> str:
    """Stable identity of the cloud endpoint + model a consent token authorizes.

    Derived from ``cloud_base_url`` and the model that will actually be sent
    (``model`` when given, else the configured ``cloud_model``), so that
    repointing the endpoint or changing the model after a token was issued
    invalidates the token (fail closed on None/empty).
    """
    url = (settings.cloud_base_url or "").rstrip("/").lower()
    resolved = resolve_cloud_model(settings) if model is None else model.strip()
    return f"{url}|{resolved}"


def get_cloud_client(
    consent_token: str | None,
    settings: ProviderSettings,
    *,
    payload: dict | CloudPayload,
    model: str | None = None,
) -> OpenAI:
    """Return a cloud client after verifying the token matches this request.

    The single-use consent token is verified against the currently configured
    destination (``cloud_base_url``), the model that will actually be sent
    (``model``, resolved by the caller — an override that was not consented
    is rejected), and the exact outbound ``payload``. A token issued for a
    different endpoint, model, or payload is rejected without being consumed.
    All checks are mandatory — there is no way to accept a token without its
    bindings.
    """
    if not settings.cloud_base_url or not settings.cloud_api_key_env:
        raise CloudCallBlocked("cloud provider is not configured (set base URL and key-env in Settings)")
    try:
        assert_consent(
            consent_token, provider=cloud_destination(settings, model=model), payload=payload
        )
    except Exception as exc:  # ConsentDenied
        raise CloudCallBlocked("cloud provider access requires explicit per-call consent") from exc
    api_key = os.environ.get(settings.cloud_api_key_env)
    if not api_key:
        raise CloudCallBlocked(
            f"cloud API key env var {settings.cloud_api_key_env!r} is not set in the environment"
        )
    # Each transport attempt must pass through the egress audit wrapper.
    # SDK-internal retries would send additional requests without audit rows.
    return OpenAI(base_url=settings.cloud_base_url, api_key=api_key, max_retries=0)


def list_local_models(
    settings: ProviderSettings,
    *,
    lister: Callable[[str, float], list[str]] = default_list_models,
    timeout: float = 1.0,
) -> list[str]:
    root = _ollama_root(settings.local_base_url) or DEFAULT_BASE_URL
    return lister(root, timeout)

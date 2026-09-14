"""Cloud request boundary: one request object for preview, consent, send, audit.

The exact OpenAI-compatible chat request body (``model``, ``messages`` —
including the system prompt — and ``response_format``) is built ONCE via
``build_chat_request`` and then reused verbatim for the payload preview shown
to the user, the consent-token digest, the actual transmission, and the
egress audit trail. Nothing is consented or audited that is not sent, and
nothing is sent that was not consented or audited.
"""

from __future__ import annotations

from collections.abc import Callable

Completer = Callable[[list[dict], str], str]


def build_chat_request(*, model: str, messages: list[dict], json_mode: bool = True) -> dict:
    """The exact chat request body that will be transmitted."""
    request: dict = {"model": model, "messages": [dict(m) for m in messages]}
    if json_mode:
        request["response_format"] = {"type": "json_object"}
    return request


def send_chat_request(client: object, request: dict) -> str:
    """Transmit ``request`` verbatim and return the message text."""
    resp = client.chat.completions.create(  # type: ignore[attr-defined]
        model=request["model"],
        messages=request["messages"],
        response_format=request.get("response_format"),
    )
    return resp.choices[0].message.content or ""


def make_cloud_completer(
    client: object,
    request: dict,
    *,
    audit: Callable[[str, str | None], None] | None = None,
) -> Completer:
    """Wrap ``request`` into a completer that audits EVERY transmission attempt.

    Structured-output retries call the completer repeatedly; each call is one
    network transmission and produces exactly one audit record ("ok" or
    "error"), regardless of what happens to the response afterwards.
    """

    def completer(_messages: list[dict], _model: str) -> str:
        try:
            text = send_chat_request(client, request)
        except Exception as exc:
            if audit is not None:
                audit("error", repr(exc))
            raise
        if audit is not None:
            audit("ok", None)
        return text

    return completer

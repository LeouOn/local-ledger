"""Local LLM degradation contract (provider-neutral probing).

Probes the configured local endpoint and reports one of four states so every
LLM feature can degrade to a deterministic message instead of raising a raw
``ConnectionError``. The endpoint is probed FIRST: a healthy, compatible
server is reusable even when no launcher binary is installed. Probes are
injectable so the unit suite (which blocks sockets) can exercise every state
without a real server.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from enum import StrEnum

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.3:8b"


class OllamaState(StrEnum):
    NOT_INSTALLED = "NOT_INSTALLED"
    NOT_RUNNING = "NOT_RUNNING"
    MODEL_MISSING = "MODEL_MISSING"
    READY = "READY"


# --- provider-appropriate guidance -----------------------------------------
#
# Status messages are keyed by the selected local profile ("ollama", "ds4",
# "glm") so users of DwarfStar servers are not told to install Ollama.

GUIDANCE: dict[OllamaState, str] = {
    OllamaState.NOT_INSTALLED: "Install Ollama from https://ollama.com and restart the app.",
    OllamaState.NOT_RUNNING: "Start the local model server: run `ollama serve`.",
    OllamaState.MODEL_MISSING: "Pull the model: run `ollama pull <model>` (set in Settings).",
    OllamaState.READY: "Local model is ready.",
}

_PROFILE_GUIDANCE: dict[str, dict[OllamaState, str]] = {
    "ollama": GUIDANCE,
    "ds4": {
        OllamaState.NOT_INSTALLED: (
            "DwarfStar server not found — build it in ~/Projects/ds4 (make)."
        ),
        OllamaState.NOT_RUNNING: (
            "Start the DwarfStar server (~/Projects/ds4/ds4-server) or enable "
            "'Start local server when needed' in Settings."
        ),
        OllamaState.MODEL_MISSING: (
            "The DwarfStar endpoint is serving a different model than the selected "
            "profile; stop that server manually before starting this one."
        ),
        OllamaState.READY: "Local model is ready.",
    },
    "glm": {
        OllamaState.NOT_INSTALLED: (
            "DwarfStar server not found — build it in ~/Projects/ds4 (make)."
        ),
        OllamaState.NOT_RUNNING: (
            "Start the GLM server (ds4-server -m gguf/GLM-5.3-Flash-…-MTP-ds4.gguf) "
            "or enable 'Start local server when needed' in Settings."
        ),
        OllamaState.MODEL_MISSING: (
            "The DwarfStar endpoint is serving a different model than the selected "
            "profile; stop that server manually before starting this one."
        ),
        OllamaState.READY: "Local model is ready.",
    },
}


def guidance_for(state: OllamaState, profile_key: str = "ollama") -> str:
    """Status guidance appropriate for the selected local server profile."""
    return _PROFILE_GUIDANCE.get(profile_key, GUIDANCE)[state]


# Per-feature degraded UX messages, keyed by state then feature.
DEGRADED_BEHAVIOR: dict[OllamaState, dict[str, str]] = {
    OllamaState.NOT_INSTALLED: {
        "paste_parse": "Paste-parse is unavailable: no local model server is installed.",
        "categorize": "Auto-categorization is unavailable: no local model server is installed.",
        "evaluate": "LLM budget evaluation is unavailable: install a local model server or connect a cloud provider in Settings.",
    },
    OllamaState.NOT_RUNNING: {
        "paste_parse": "Paste-parse is unavailable: start the local model server.",
        "categorize": "Auto-categorization is unavailable: start the local model server.",
        "evaluate": "LLM budget evaluation is unavailable: start the local model server or use a cloud provider.",
    },
    OllamaState.MODEL_MISSING: {
        "paste_parse": "Paste-parse is unavailable: the configured model is not loaded.",
        "categorize": "Auto-categorization is unavailable: the configured model is not loaded.",
        "evaluate": "LLM budget evaluation is unavailable: the configured model is not loaded.",
    },
    OllamaState.READY: {
        "paste_parse": "Ready.",
        "categorize": "Ready.",
        "evaluate": "Ready.",
    },
}


def degraded_message(state: OllamaState, feature: str) -> str:
    return DEGRADED_BEHAVIOR[state][feature]


class LLMUnavailable(Exception):
    def __init__(self, state: OllamaState, guidance: str) -> None:
        self.state = state
        self.guidance = guidance
        super().__init__(f"{state.value}: {guidance}")


def _default_probe_binary() -> bool:
    # Any known local profile launcher counts; the profile itself is what
    # decides what would actually be started.
    from local_ledger.llm.local_server import PROFILES, resolve_launcher

    return any(resolve_launcher(p) is not None for p in PROFILES.values())


def _default_probe_endpoint(base_url: str, timeout: float) -> bool:
    clean = base_url.rstrip("/")
    # 1. Try Ollama (/api/tags)
    try:
        urllib.request.urlopen(f"{clean}/api/tags", timeout=timeout).close()
        return True
    except (OSError, RuntimeError):
        pass
    # 2. Try OpenAI-compatible endpoints (/v1/models, /models)
    for path in ("/v1/models", "/models"):
        try:
            req = urllib.request.Request(f"{clean}{path}", headers={"User-Agent": "local-ledger"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 204):
                    return True
        except (OSError, RuntimeError):
            pass
    return False


def _default_list_models(base_url: str, timeout: float) -> list[str] | None:
    """List served model IDs, or None when model discovery is unsupported.

    An empty list means the endpoint answered and serves NO models — the
    configured model is then definitely missing. ``None`` means no listing
    endpoint answered usable JSON, so the served model cannot be verified.
    """
    clean = base_url.rstrip("/")
    # 1. Ollama native /api/tags
    try:
        with urllib.request.urlopen(f"{clean}/api/tags", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in payload.get("models", [])]
    except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
        pass
    # 2. OpenAI-compatible listing (/v1/models, /models)
    for path in ("/v1/models", "/models"):
        try:
            req = urllib.request.Request(f"{clean}{path}", headers={"User-Agent": "local-ledger"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return [m.get("id", "") for m in payload.get("data", [])]
        except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
            continue
    return None


def detect_state(
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 1.0,
    *,
    probe_binary: Callable[[], bool] = _default_probe_binary,
    probe_endpoint: Callable[[str, float], bool] = _default_probe_endpoint,
    list_models: Callable[[str, float], list[str] | None] = _default_list_models,
) -> OllamaState:
    # Endpoint first: a healthy, compatible server is adopted as-is, even when
    # no launcher binary is installed on this machine.
    if probe_endpoint(base_url, timeout):
        models = list_models(base_url, timeout)
        if models is None:
            # Model discovery is unsupported here; endpoint health is the best
            # available signal, so the server is considered usable.
            return OllamaState.READY
        if model not in models:
            # Covers both a mismatched server and an empty model library:
            # the configured model is definitely not being served.
            return OllamaState.MODEL_MISSING
        return OllamaState.READY
    if not probe_binary():
        return OllamaState.NOT_INSTALLED
    return OllamaState.NOT_RUNNING

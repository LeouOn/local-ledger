"""Integration smoke test for local model runtime.

Run with: ``pytest -m integration tests/integration/test_smoke.py``

Uses only synthetic input. If the local runtime is not ready, records a
SKIP-deferred verdict instead of silently passing or failing.
Leaves DEFAULT_DB_URL completely untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_ledger.llm.smoke import run_smoke

pytestmark = pytest.mark.integration

EVIDENCE = Path(__file__).resolve().parent.parent.parent / ".omo" / "evidence" / "local-runtime-smoke.md"


def test_real_local_model_smoke() -> None:
    profile_key = os.environ.get("LOCAL_LEDGER_PROFILE", "ollama")
    endpoint = os.environ.get("LOCAL_LEDGER_OLLAMA_URL")
    model = os.environ.get("LOCAL_LEDGER_OLLAMA_MODEL")

    result = run_smoke(
        profile_key,
        endpoint=endpoint,
        model=model,
        timeout=15.0,
    )

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    if result.verdict == "DEFERRED":
        EVIDENCE.write_text(
            "# Local Runtime Smoke\n\n"
            f"**Verdict: SKIP-deferred**\n\n"
            f"- profile: `{result.profile}`\n"
            f"- model: `{result.model}`\n"
            f"- endpoint: `{result.endpoint}`\n"
            f"- detail: {result.detail}\n",
            encoding="utf-8",
        )
        pytest.skip(f"Local runtime deferred: {result.detail}")

    assert result.verdict == "PASS", f"Smoke failed: {result.detail}"
    assert result.latency_ms is not None

    EVIDENCE.write_text(
        "# Local Runtime Smoke\n\n"
        f"**Verdict: PASS**\n\n"
        f"- profile: `{result.profile}`\n"
        f"- model: `{result.model}`\n"
        f"- endpoint: `{result.endpoint}`\n"
        f"- latency: `{result.latency_ms} ms`\n"
        f"- output: `{result.output}`\n",
        encoding="utf-8",
    )

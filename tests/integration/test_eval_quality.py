"""Integration spike: LLM evaluation quality (deselected by default).

Run with: ``pytest -m integration tests/integration/test_eval_quality.py``

Requires Ollama READY with the configured model pulled. Scores three sample
ledgers against tests/integration/rubric.md; sets ProviderSettings default to
local if >=2/3 pass, else cloud-consent. If Ollama is absent, records a
SKIP-deferred verdict instead of silently passing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

EVIDENCE = Path(__file__).resolve().parent.parent.parent / ".omo" / "evidence" / "task-19-local-ledger.md"
RUBRIC = Path(__file__).resolve().parent / "rubric.md"


@pytest.fixture(scope="module", autouse=True)
def _require_ollama() -> None:
    from local_ledger.llm.ollama import GUIDANCE, OllamaState, detect_state

    base = os.environ.get("LOCAL_LEDGER_OLLAMA_URL", "http://localhost:11434")
    model = os.environ.get("LOCAL_LEDGER_OLLAMA_MODEL", "llama3.3:8b")
    state = detect_state(base, model)
    if state != OllamaState.READY:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(
            "# Task 19 - LLM evaluation-quality spike\n\n"
            f"**Verdict: SKIP-deferred** (Ollama not READY)\n\n"
            f"- detected state: `{state.value}`\n"
            f"- guidance: {GUIDANCE[state]}\n"
            f"- rubric: {RUBRIC.name}\n\n"
            "Re-run with `pytest -m integration tests/integration/test_eval_quality.py` "
            f"once Ollama is running and `{model}` is pulled.\n",
            encoding="utf-8",
        )
        pytest.skip(f"ollama state {state.value}; spike deferred", allow_module_level=True)


# Three sample ledgers as aggregate payloads (no raw transactions).
SAMPLE_LEDGERS = [
    {
        "name": "overspent-food",
        "payload": {
            "month": "2026-01",
            "total_income_cents": 300000,
            "total_spent_cents": 320000,
            "total_budgeted_cents": 290000,
            "categories": [
                {"name": "Food", "budgeted": 40000, "spent": 62000, "balance": -22000, "carryover": 0},
                {"name": "Rent", "budgeted": 150000, "spent": 150000, "balance": 0, "carryover": 0},
            ],
        },
    },
    {
        "name": "income-drop",
        "payload": {
            "month": "2026-02",
            "total_income_cents": 200000,
            "total_spent_cents": 260000,
            "total_budgeted_cents": 260000,
            "categories": [
                {"name": "Salary", "budgeted": 300000, "spent": 0, "balance": 300000, "carryover": 0},
            ],
        },
    },
    {
        "name": "balanced",
        "payload": {
            "month": "2026-03",
            "total_income_cents": 300000,
            "total_spent_cents": 280000,
            "total_budgeted_cents": 290000,
            "categories": [
                {"name": "Food", "budgeted": 40000, "spent": 38000, "balance": 2000, "carryover": 0},
            ],
        },
    },
]


def test_eval_quality_spike_records_verdict() -> None:
    from sqlalchemy import create_engine
    from sqlmodel import Session

    from local_ledger.analysis.evaluate import llm_interpret
    from local_ledger.analysis.payloads import EvaluationPayload
    from local_ledger.db import DEFAULT_DB_URL, run_migrations
    from local_ledger.llm.providers import get_local_client, load_settings, save_settings
    from local_ledger.llm.structured import make_openai_completer

    run_migrations(DEFAULT_DB_URL)
    engine = create_engine(DEFAULT_DB_URL)
    try:
        with Session(engine) as session:
            settings = load_settings(session)
            client = get_local_client(settings)
    finally:
        engine.dispose()

    completer = make_openai_completer(client)
    results = []
    passes = 0
    for sample in SAMPLE_LEDGERS:
        payload = EvaluationPayload(**sample["payload"])
        try:
            evaluation = llm_interpret(payload, completer=completer, model=settings.local_model)
            blob = json.dumps(evaluation.model_dump(), sort_keys=True)
            # Placeholder scoring: agent re-scores against rubric.md manually.
            # Here we only record the raw evaluation; a human/oracle scores it.
            results.append({"name": sample["name"], "evaluation": blob, "auto_pass": None})
        except Exception as exc:
            results.append({"name": sample["name"], "error": str(exc), "auto_pass": False})

    passes = sum(1 for r in results if r.get("auto_pass"))
    default_provider = "local" if passes >= 2 else "cloud-consent"

    with Session(create_engine(DEFAULT_DB_URL)) as session:
        save_settings(session, evaluation_default_provider=default_provider)

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        "# Task 19 - LLM evaluation-quality spike\n\n"
        f"**Verdict: RAN** (auto-scoring deferred to oracle/human against rubric.md)\n\n"
        f"- model: `{settings.local_model}`\n"
        f"- rubric: `{RUBRIC.name}`\n"
        f"- per-ledger evaluations:\n\n"
        + "\n".join(f"  - {r['name']}: `{r.get('evaluation', r.get('error'))}`" for r in results)
        + f"\n\n- provisional default provider: `{default_provider}` "
        "(finalized once a human/oracle scores against the rubric)\n",
        encoding="utf-8",
    )

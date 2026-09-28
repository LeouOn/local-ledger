"""Integration spike: LLM evaluation quality (deselected by default).

Run with: ``pytest -m integration tests/integration/test_eval_quality.py``

Requires Ollama READY with the configured model pulled. Scores three sample
ledgers against tests/integration/rubric.md. If Ollama is absent, records a
SKIP-deferred verdict instead of silently passing.
Maintains hermetic isolation by using an isolated test database and never
mutating the default application database or user settings.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

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


def score_rubric(payload: dict[str, Any], evaluation: Any) -> dict[str, Any]:
    """Score evaluation against rubric.md dimensions (0-3 each, max 9)."""
    text = (
        f"{evaluation.summary} "
        f"{' '.join(evaluation.key_findings)} "
        f"{' '.join(evaluation.suggested_actions)}"
    ).lower()

    # 1. Specificity (0-3)
    categories = [c["name"].lower() for c in payload.get("categories", [])]
    cats_found = any(cat in text for cat in categories)
    if not cats_found:
        specificity = 0
    else:
        has_direction = any(w in text for w in ("over", "under", "deficit", "surplus", "exceed", "drift", "%"))
        has_figures = bool(re.search(r"(\$\d+|\d+\s*cents|\d+%)", text))
        if has_direction and has_figures:
            specificity = 3
        elif has_direction or has_figures:
            specificity = 2
        else:
            specificity = 1

    # 2. Correctness (0-3)
    income = payload.get("total_income_cents", 0)
    spent = payload.get("total_spent_cents", 0)
    overspent = spent > income
    contradiction = False
    if overspent and "surplus" in text and "no surplus" not in text:
        contradiction = True
    if not overspent and "deficit" in text and "no deficit" not in text:
        contradiction = True

    correctness = 0 if contradiction else (3 if specificity >= 2 else 2)

    # 3. Actionability (0-3)
    actions = evaluation.suggested_actions or []
    if not actions:
        actionability = 0
    elif len(actions) == 1 and len(actions[0].strip()) < 15:
        actionability = 1
    elif len(actions) == 1:
        actionability = 2
    else:
        actionability = 3

    total = specificity + correctness + actionability
    return {
        "status": "scored",
        "specificity": specificity,
        "correctness": correctness,
        "actionability": actionability,
        "total": total,
        "passed": total >= 6,
    }


def test_eval_quality_spike_records_verdict(tmp_path: Path) -> None:
    from sqlalchemy import create_engine
    from sqlmodel import Session, SQLModel

    from local_ledger.analysis.evaluate import llm_interpret
    from local_ledger.analysis.payloads import EvaluationPayload
    from local_ledger.llm.providers import get_local_client, load_settings
    from local_ledger.llm.structured import make_openai_completer

    # Use isolated test database so DEFAULT_DB_URL is never modified or touched
    db_path = tmp_path / "eval_quality.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(db_url)
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            settings = load_settings(session)
            client = get_local_client(settings)
    finally:
        engine.dispose()

    completer = make_openai_completer(client)
    results = []
    for sample in SAMPLE_LEDGERS:
        payload = EvaluationPayload(**sample["payload"])
        try:
            evaluation = llm_interpret(payload, completer=completer, model=settings.local_model)
            blob = json.dumps(evaluation.model_dump(), sort_keys=True)
            score = score_rubric(sample["payload"], evaluation)
            results.append({
                "name": sample["name"],
                "status": "scored",
                "evaluation": blob,
                "score": score,
                "passed": score["passed"],
            })
        except Exception as exc:
            results.append({
                "name": sample["name"],
                "status": "failed",
                "error": str(exc),
                "score": None,
                "passed": False,
            })

    passes = sum(1 for r in results if r.get("passed"))
    verdict = "PASS" if passes >= 2 else "FAIL"
    provisional_provider = "local" if passes >= 2 else "cloud-consent"

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    summary_lines = []
    for r in results:
        if r["status"] == "scored":
            sc = r["score"]
            summary_lines.append(
                f"  - {r['name']}: **{'PASS' if r['passed'] else 'FAIL'}** "
                f"(score: {sc['total']}/9; specificity={sc['specificity']}, "
                f"correctness={sc['correctness']}, actionability={sc['actionability']})"
            )
        else:
            summary_lines.append(f"  - {r['name']}: **FAILED** ({r.get('error')})")

    EVIDENCE.write_text(
        "# Task 19 - LLM evaluation-quality spike\n\n"
        f"**Verdict: {verdict}** ({passes}/{len(SAMPLE_LEDGERS)} passed against rubric)\n\n"
        f"- model: `{settings.local_model}`\n"
        f"- rubric: `{RUBRIC.name}`\n"
        f"- recommended provider: `{provisional_provider}`\n\n"
        "### Per-ledger scores:\n\n"
        + "\n".join(summary_lines)
        + "\n",
        encoding="utf-8",
    )

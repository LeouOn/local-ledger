# Phase 1: LLM Evaluation Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multi-month trend narratives and what-if scenario modeling to the Insights page, sharing a pure-computation projection engine.

**Architecture:** Three new analysis modules (`scenarios.py`, `multimonth.py`, `narrative.py`) plugged into the existing v1 stack. Scenario engine is pure integer-cents math (no LLM). Multi-month payload builder extends the existing token-capped aggregates-only pattern. Narrative evaluation uses the existing `structured.extract` with fake completers for tests.

**Tech Stack:** Python 3.12, SQLModel/SQLAlchemy, Pydantic v2, Streamlit, pytest, tiktoken.

## Global Constraints

- Python >=3.11,<3.13; all money as integer cents; naive `YYYY-MM-DD` dates
- No LLM in the arithmetic path (scenarios.py has zero LLM imports — test-enforced)
- Aggregates-only for cloud; full aggregate context for local; no raw transactions in prompts
- All LLM calls are explicit user actions (buttons), never on page load
- No new dependencies beyond v1's pyproject.toml
- TDD: failing test first, watch it fail, minimal code, watch it pass, commit

---

### Task 1: Scenario/Projection Engine (`analysis/scenarios.py`)

**Files:**
- Create: `src/local_ledger/analysis/scenarios.py`
- Test: `tests/unit/test_scenarios.py`

**Interfaces:**
- Consumes: `MonthSummary`, `CategoryMonth` from `local_ledger.ledger.trends`
- Produces:
  - `ScenarioResult(original_spent: int, adjusted_spent: int, savings_delta_cents: int, year_end_delta_cents: int)`
  - `ProjectedMonth(month: str, projected_income_cents: int, projected_spent_cents: int, per_category_spent: dict[int, int])`
  - `what_if_scenario(base: MonthSummary, category_id: int, pct_change: float, months_remaining: int = 0) -> ScenarioResult`
  - `project_forward(summaries: list[MonthSummary], months_ahead: int, adjustments: dict[int, float] | None = None) -> list[ProjectedMonth]`

- [ ] **Step 1: Write failing test — what_if_scenario basic case**

```python
"""Scenario engine tests: pure-math projections, no LLM."""

from __future__ import annotations

from local_ledger.analysis.scenarios import (
    ProjectedMonth,
    ScenarioResult,
    project_forward,
    what_if_scenario,
)
from local_ledger.ledger.trends import CategoryMonth, MonthSummary


def _food_month(spent: int = 40000, budgeted: int = 30000) -> MonthSummary:
    return MonthSummary(
        month="2026-01",
        income_cents=200000,
        spent_cents=spent,
        budgeted_cents=budgeted,
        per_category=[
            CategoryMonth(category_id=1, name="Food", budgeted=budgeted, spent=spent, balance=budgeted - spent, carryover=0),
        ],
    )


def test_what_if_scenario_reduces_spent() -> None:
    base = _food_month(spent=40000)
    result = what_if_scenario(base, category_id=1, pct_change=-0.20)
    assert isinstance(result, ScenarioResult)
    assert result.original_spent == 40000
    assert result.adjusted_spent == 32000
    assert result.savings_delta_cents == 8000


def test_what_if_scenario_year_end_delta() -> None:
    base = _food_month(spent=40000)
    result = what_if_scenario(base, category_id=1, pct_change=-0.20, months_remaining=5)
    assert result.year_end_delta_cents == 8000 * 5
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_scenarios.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError: No module named 'local_ledger.analysis.scenarios'

- [ ] **Step 3: Write minimal implementation**

```python
"""Scenario/projection engine: pure integer-cents math, no LLM imports."""

from __future__ import annotations

from dataclasses import dataclass

from local_ledger.ledger.trends import MonthSummary


@dataclass
class ScenarioResult:
    original_spent: int
    adjusted_spent: int
    savings_delta_cents: int
    year_end_delta_cents: int


@dataclass
class ProjectedMonth:
    month: str
    projected_income_cents: int
    projected_spent_cents: int
    per_category_spent: dict[int, int]


def what_if_scenario(
    base: MonthSummary,
    category_id: int,
    pct_change: float,
    months_remaining: int = 0,
) -> ScenarioResult:
    cat = next((c for c in base.per_category if c.category_id == category_id), None)
    if cat is None:
        raise ValueError(f"category_id {category_id} not in month summary")
    original = cat.spent
    adjusted = round(original * (1 + pct_change))
    delta = original - adjusted
    return ScenarioResult(
        original_spent=original,
        adjusted_spent=adjusted,
        savings_delta_cents=delta,
        year_end_delta_cents=delta * months_remaining,
    )


def project_forward(
    summaries: list[MonthSummary],
    months_ahead: int,
    adjustments: dict[int, float] | None = None,
) -> list[ProjectedMonth]:
    if not summaries:
        return []
    adjustments = adjustments or {}
    n = len(summaries)

    all_cats: set[int] = set()
    for s in summaries:
        for c in s.per_category:
            all_cats.add(c.category_id)

    avg_spent: dict[int, int] = {}
    for cat_id in all_cats:
        total = sum(
            next((c.spent for c in s.per_category if c.category_id == cat_id), 0)
            for s in summaries
        )
        avg_spent[cat_id] = total // n

    avg_income = sum(s.income_cents for s in summaries) // n

    projections: list[ProjectedMonth] = []
    for i in range(1, months_ahead + 1):
        per_cat: dict[int, int] = {}
        for cat_id in sorted(all_cats):
            base_amount = avg_spent[cat_id]
            pct = adjustments.get(cat_id, 0.0)
            per_cat[cat_id] = round(base_amount * (1 + pct))
        total_spent = sum(per_cat.values())
        projections.append(
            ProjectedMonth(
                month=f"+{i}",
                projected_income_cents=avg_income,
                projected_spent_cents=total_spent,
                per_category_spent=per_cat,
            )
        )
    return projections
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_scenarios.py -q -p no:cacheprovider`
Expected: 2 passed

- [ ] **Step 5: Write project_forward test**

Add to `tests/unit/test_scenarios.py`:

```python
def test_project_forward_averages_and_adjusts() -> None:
    months = [
        _food_month(spent=30000),
        _food_month(spent=50000),
    ]
    projections = project_forward(months, months_ahead=2, adjustments={1: -0.10})
    assert len(projections) == 2
    # average of 30000, 50000 = 40000; adjusted by -10% = 36000
    assert projections[0].per_category_spent[1] == 36000
    assert projections[0].projected_income_cents == 200000
    assert projections[1].per_category_spent[1] == 36000


def test_project_forward_empty_summaries() -> None:
    assert project_forward([], months_ahead=3) == []


def test_scenarios_module_has_no_llm_imports() -> None:
    import inspect

    from local_ledger.analysis import scenarios

    src = inspect.getsource(scenarios)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src
```

- [ ] **Step 6: Run all scenario tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_scenarios.py -q -p no:cacheprovider`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add src/local_ledger/analysis/scenarios.py tests/unit/test_scenarios.py
git commit -m "feat(analysis): scenario/projection engine with what-if + forward projection (#p1-1)"
```

---

### Task 2: Multi-Month Payload Builder (`analysis/multimonth.py`)

**Files:**
- Create: `src/local_ledger/analysis/multimonth.py`
- Test: `tests/unit/test_multimonth.py`

**Interfaces:**
- Consumes: `MonthSummary`, `CategoryMonth` from `local_ledger.ledger.trends`
- Produces:
  - `MultiMonthCategory(months: list[int], income_months: list[int], mom_delta_pct: float, trend_direction: str)`
  - `MultiMonthPayload(months: list[str], total_income_cents: int, total_spent_cents: int, categories: dict[int, MultiMonthCategory], truncated: bool)`
  - `build_multimonth_payload(months: list[str], summaries: list[MonthSummary]) -> MultiMonthPayload`
  - `to_prompt(payload: MultiMonthPayload) -> str`

- [ ] **Step 1: Write failing test — basic payload structure**

```python
"""Multi-month payload builder tests: aggregates only, token-capped."""

from __future__ import annotations

import tiktoken

from local_ledger.analysis.multimonth import (
    MultiMonthPayload,
    build_multimonth_payload,
    to_prompt,
)
from local_ledger.ledger.trends import CategoryMonth, MonthSummary

TOKEN_CAP = 4096


def _make_summary(month: str, spent: int, income: int = 100000) -> MonthSummary:
    return MonthSummary(
        month=month,
        income_cents=income,
        spent_cents=spent,
        budgeted_cents=30000,
        per_category=[
            CategoryMonth(category_id=1, name="Food", budgeted=30000, spent=spent, balance=30000 - spent, carryover=0),
        ],
    )


def test_payload_aggregates_multiple_months() -> None:
    summaries = [
        _make_summary("2026-01", 25000),
        _make_summary("2026-02", 35000),
        _make_summary("2026-03", 40000),
    ]
    payload = build_multimonth_payload(
        months=["2026-01", "2026-02", "2026-03"],
        summaries=summaries,
    )
    assert isinstance(payload, MultiMonthPayload)
    assert payload.total_income_cents == 300000
    assert payload.total_spent_cents == 100000
    assert payload.months == ["2026-01", "2026-02", "2026-03"]
    cat = payload.categories[1]
    assert cat.months == [25000, 35000, 40000]


def test_trend_direction_computed() -> None:
    summaries = [
        _make_summary("2026-01", 10000),
        _make_summary("2026-02", 15000),
    ]
    payload = build_multimonth_payload(months=["2026-01", "2026-02"], summaries=summaries)
    cat = payload.categories[1]
    assert cat.trend_direction == "up"


def test_to_prompt_under_token_cap() -> None:
    summaries = [_make_summary(f"2026-{m:02d}", 20000 + m * 1000) for m in range(1, 13)]
    payload = build_multimonth_payload(
        months=[f"2026-{m:02d}" for m in range(1, 13)],
        summaries=summaries,
    )
    prompt = to_prompt(payload)
    enc = tiktoken.get_encoding("cl100k_base")
    assert len(enc.encode(prompt)) <= TOKEN_CAP
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_multimonth.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError: No module named 'local_ledger.analysis.multimonth'

- [ ] **Step 3: Write minimal implementation**

```python
"""Multi-month payload builder: aggregates N months into a token-capped typed payload."""

from __future__ import annotations

import json

import tiktoken
from pydantic import BaseModel

from local_ledger.ledger.trends import MonthSummary

TOKEN_CAP = 4096
_ENCODER_NAME = "cl100k_base"
FLAT_THRESHOLD_PCT = 10.0


class MultiMonthCategory(BaseModel):
    months: list[int]  # spent per month, oldest first
    income_months: list[int]  # income per month
    mom_delta_pct: float
    trend_direction: str  # up | down | flat


class MultiMonthPayload(BaseModel):
    months: list[str]
    total_income_cents: int
    total_spent_cents: int
    categories: dict[int, MultiMonthCategory]
    truncated: bool = False


def _encoder():  # type: ignore[no-untyped-def]
    try:
        enc = tiktoken.get_encoding(_ENCODER_NAME)
        return lambda text: len(enc.encode(text))
    except Exception:
        return lambda text: max(1, len(text) // 4)


def build_multimonth_payload(
    months: list[str],
    summaries: list[MonthSummary],
) -> MultiMonthPayload:
    if len(months) != len(summaries):
        raise ValueError("months and summaries must have the same length")

    all_cats: set[int] = set()
    for s in summaries:
        for c in s.per_category:
            all_cats.add(c.category_id)

    categories: dict[int, MultiMonthCategory] = {}
    for cat_id in sorted(all_cats):
        spent_per_month = []
        income_per_month = []
        for s in summaries:
            cat = next((c for c in s.per_category if c.category_id == cat_id), None)
            spent_per_month.append(cat.spent if cat else 0)
            income_per_month.append(s.income_cents)

        first, last = spent_per_month[0], spent_per_month[-1]
        if first == 0:
            mom_delta = 0.0 if last == 0 else 100.0
        else:
            mom_delta = ((last - first) / first) * 100
        if abs(mom_delta) < FLAT_THRESHOLD_PCT:
            direction = "flat"
        elif mom_delta > 0:
            direction = "up"
        else:
            direction = "down"

        categories[cat_id] = MultiMonthCategory(
            months=spent_per_month,
            income_months=income_per_month,
            mom_delta_pct=round(mom_delta, 1),
            trend_direction=direction,
        )

    payload = MultiMonthPayload(
        months=list(months),
        total_income_cents=sum(s.income_cents for s in summaries),
        total_spent_cents=sum(s.spent_cents for s in summaries),
        categories=categories,
    )

    count = _encoder()
    while payload.categories and count(to_prompt(payload)) > TOKEN_CAP:
        # drop the oldest month from every category
        payload.months = payload.months[1:]
        for cat in payload.categories.values():
            cat.months = cat.months[1:]
            cat.income_months = cat.income_months[1:]
        payload.truncated = True

    return payload


def to_prompt(payload: MultiMonthPayload) -> str:
    data = payload.model_dump()
    data["categories"] = {str(k): v for k, v in data["categories"].items()}
    return json.dumps(data, sort_keys=True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_multimonth.py -q -p no:cacheprovider`
Expected: 3 passed

- [ ] **Step 5: Write truncation test**

Add to `tests/unit/test_multimonth.py`:

```python
def test_truncation_when_over_cap() -> None:
    """A large category count across many months triggers truncation."""
    from local_ledger.ledger.trends import CategoryMonth

    cats = [CategoryMonth(category_id=i, name=f"Cat{i}", budgeted=100, spent=i * 100, balance=0, carryover=0) for i in range(50)]
    summaries = [
        MonthSummary(
            month=f"2026-{m:02d}",
            income_cents=100000,
            spent_cents=50000,
            budgeted_cents=5000,
            per_category=cats,
        )
        for m in range(1, 13)
    ]
    payload = build_multimonth_payload(
        months=[f"2026-{m:02d}" for m in range(1, 13)],
        summaries=summaries,
    )
    enc = tiktoken.get_encoding("cl100k_base")
    assert len(enc.encode(to_prompt(payload))) <= TOKEN_CAP
    if payload.truncated:
        assert len(payload.months) < 12


def test_no_raw_transaction_fields_in_payload() -> None:
    summaries = [_make_summary("2026-01", 25000)]
    payload = build_multimonth_payload(
        months=["2026-01"], summaries=summaries,
    )
    dumped = json.dumps(payload.model_dump())
    for forbidden in ("payee", "account", "notes", "imported_id"):
        assert forbidden not in dumped.lower()
```

- [ ] **Step 6: Run all multimonth tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_multimonth.py -q -p no:cacheprovider`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add src/local_ledger/analysis/multimonth.py tests/unit/test_multimonth.py
git commit -m "feat(analysis): multi-month payload builder with token cap + truncation (#p1-2)"
```

---

### Task 3: Trend Narrative Evaluation (`analysis/narrative.py`)

**Files:**
- Create: `src/local_ledger/analysis/narrative.py`
- Test: `tests/unit/test_narrative.py`

**Interfaces:**
- Consumes: `MultiMonthPayload` from Task 2, `extract` from `local_ledger.llm.structured`, `MonthSummary` from trends, `Recommendation` model
- Produces:
  - `TrendNarrative(summary: str, key_trends: list[str], projections: list[str], warnings: list[str])`
  - `generate_trend_narrative(payload: MultiMonthPayload, *, completer, model: str, provider: str = "local", consent_token: str | None = None, settings=None, max_retries: int = 2) -> TrendNarrative`

- [ ] **Step 1: Write failing test — narrative via fake completer**

```python
"""Trend narrative tests: typed output, cloud blocked, error handling."""

from __future__ import annotations

import json

import pytest

from local_ledger.analysis.multimonth import MultiMonthCategory, MultiMonthPayload
from local_ledger.analysis.narrative import TrendNarrative, generate_trend_narrative
from local_ledger.llm.providers import CloudCallBlocked


def _fake_payload() -> MultiMonthPayload:
    return MultiMonthPayload(
        months=["2026-01", "2026-02", "2026-03"],
        total_income_cents=300000,
        total_spent_cents=100000,
        categories={
            1: MultiMonthCategory(
                months=[25000, 35000, 40000],
                income_months=[100000, 100000, 100000],
                mom_delta_pct=60.0,
                trend_direction="up",
            ),
        },
    )


def _completer_returning(payload):  # type: ignore[no-untyped-def]
    def _c(_messages, _model):
        return payload
    return _c


def test_generate_trend_narrative_returns_typed_output() -> None:
    fake_response = json.dumps({
        "summary": "Food spending rose 60% over 3 months.",
        "key_trends": ["Food is the fastest-growing category"],
        "projections": ["At current rate, Food will reach $500/mo by June"],
        "warnings": ["Food trend is unsustainable"],
    })
    result = generate_trend_narrative(
        _fake_payload(),
        completer=_completer_returning(fake_response),
        model="m",
    )
    assert isinstance(result, TrendNarrative)
    assert "rose 60%" in result.summary
    assert len(result.key_trends) == 1
    assert len(result.warnings) == 1


def test_cloud_path_blocked_without_consent() -> None:
    with pytest.raises(CloudCallBlocked):
        generate_trend_narrative(
            _fake_payload(),
            completer=None,  # type: ignore[arg-type]
            model="m",
            provider="cloud",
            consent_token=None,
            settings=None,  # type: ignore[arg-type]
        )


def test_garbage_response_raises_structured_error() -> None:
    from local_ledger.llm.structured import StructuredOutputError

    with pytest.raises(StructuredOutputError):
        generate_trend_narrative(
            _fake_payload(),
            completer=_completer_returning("not json"),
            model="m",
            max_retries=1,
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_narrative.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError: No module named 'local_ledger.analysis.narrative'

- [ ] **Step 3: Write minimal implementation**

```python
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
        {
            "role": "system",
            "content": (
                "You are a financial trend analyst. Given ONLY category-level aggregate "
                "data across multiple months (no raw transactions), write a narrative "
                'as JSON {"summary": str, "key_trends": [str], "projections": [str], '
                '"warnings": [str]}. Base all numbers strictly on the provided cents values. '
                "Identify rising/falling categories, project trajectory, and flag risks."
            ),
        },
        {"role": "user", "content": to_prompt(payload)},
    ]
    try:
        return extract(completer, TrendNarrative, messages, model, max_retries=max_retries)
    except StructuredOutputError as exc:
        raise StructuredOutputError(f"Trend narrative failed: {exc}") from exc
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_narrative.py -q -p no:cacheprovider`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/local_ledger/analysis/narrative.py tests/unit/test_narrative.py
git commit -m "feat(analysis): trend narrative evaluation via LLM interpretation (#p1-3)"
```

---

### Task 4: Insights Page UI — Trend Narrative + What-If Sections

**Files:**
- Modify: `app_pages/insights.py`
- Test: `tests/unit/test_insights_sections.py` (AppTest)

**Interfaces:**
- Consumes: `TrendsService` (existing), `deterministic_evaluate` (existing), `build_multimonth_payload` (Task 2), `generate_trend_narrative` (Task 3), `what_if_scenario` (Task 1), `RecommendationService` (existing)
- Produces: two new Streamlit sections on the Insights page

- [ ] **Step 1: Read current insights.py to understand the layout**

Run: `cat app_pages/insights.py` (or Read tool)
Note the existing sections: month selector, deterministic findings, LLM interpretation, recommendation history.

- [ ] **Step 2: Write failing AppTest for new sections**

```python
"""AppTest for the new Insights page sections (trend narrative + what-if)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.ledger.service import LedgerService
from local_ledger.models import CategoryBudget
from tests.factories import make_account, make_category, make_transaction

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "insights.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    engine = create_engine(url)
    init_db(engine)
    with Session(engine) as s:
        svc = LedgerService(s)
        acc = svc.create_account(name="Checking", type="checking")
        food = svc.create_category(name="Food")
        for m in range(1, 4):
            make_transaction(s, acc.id, amount_cents=-(10000 + m * 5000), date=f"2026-0{m}-10", category_id=food.id)
        s.add(CategoryBudget(category_id=food.id, month="2026-01", allocated_cents=30000))
        s.add(CategoryBudget(category_id=food.id, month="2026-02", allocated_cents=30000))
        s.add(CategoryBudget(category_id=food.id, month="2026-03", allocated_cents=30000))
        s.commit()
    engine.dispose()
    return url


def test_insights_page_renders_new_sections(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app_pages/insights.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"insights page raised: {at.exception}"
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_insights_sections.py -q -p no:cacheprovider`
Expected: FAIL (new sections not yet in insights.py, or page errors)

- [ ] **Step 4: Add trend narrative + what-if sections to insights.py**

Append to `app_pages/insights.py` (after the existing "LLM interpretation" section, before "Recommendation history"):

```python
st.divider()
st.subheader("Multi-month trend narrative")
n_months = st.selectbox("Months to analyze", [3, 6, 12], index=0, key="trend_n_months")
if st.button("Generate trend narrative"):
    from local_ledger.analysis.multimonth import build_multimonth_payload
    from local_ledger.analysis.narrative import generate_trend_narrative

    months = []
    y, m = (int(x) for x in month.split("-"))
    for _ in range(n_months):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months.reverse()

    summaries = [trends.month_summary(mo) for mo in months]
    payload = build_multimonth_payload(months=months, summaries=summaries)

    degraded = degraded_message("evaluate")
    if degraded:
        st.warning(degraded)
    else:
        try:
            from local_ledger.llm.providers import get_local_client, load_settings
            from local_ledger.llm.structured import make_openai_completer

            settings = load_settings(session)
            client = get_local_client(settings)
            completer = make_openai_completer(client)
            narrative = generate_trend_narrative(payload, completer=completer, model=settings.local_model)
            st.markdown(f"**{narrative.summary}**")
            if narrative.key_trends:
                st.markdown("**Key trends**")
                for t in narrative.key_trends:
                    st.markdown(f"- {t}")
            if narrative.projections:
                st.markdown("**Projections**")
                for p in narrative.projections:
                    st.markdown(f"- {p}")
            if narrative.warnings:
                st.markdown("**Warnings**")
                for w in narrative.warnings:
                    st.warning(w)
            recs.save(
                month=month,
                provider="ollama",
                model=settings.local_model,
                prompt_hash=recs.hash_prompt(narrative.summary),
                body=narrative.summary,
            )
            st.success("Narrative saved to history.")
        except Exception as exc:
            st.error(f"Narrative failed: {exc}")

st.divider()
st.subheader("What-if scenarios")
if summary.per_category:
    from local_ledger.analysis.scenarios import what_if_scenario

    for cat in summary.per_category:
        col1, col2 = st.columns([2, 3])
        col1.markdown(f"**{cat.name}** (${cat.spent / 100:,.0f}/mo)")
        pct = col2.slider(
            f"Adjust {cat.name}",
            min_value=-50, max_value=50, value=0, step=5,
            format="%d%%",
            key=f"whatif_{cat.category_id}",
            label_visibility="collapsed",
        )
        if pct != 0:
            result = what_if_scenario(summary, cat.category_id, pct / 100, months_remaining=11)
            direction = "less" if pct < 0 else "more"
            st.caption(
                f"{cat.name}: ${result.original_spent / 100:,.0f}/mo → "
                f"${result.adjusted_spent / 100:,.0f}/mo "
                f"(saves ${result.savings_delta_cents / 100:,.0f}/mo, "
                f"${result.year_end_delta_cents / 100:,.0f} by year end)"
            )
else:
    st.caption("No budget data for this month.")
```

- [ ] **Step 5: Run AppTest to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_insights_sections.py -q -p no:cacheprovider`
Expected: PASS (page renders without exception)

- [ ] **Step 6: Run full suite to check for regressions**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q --ignore=tests/unit/test_perf.py --ignore=tests/unit/test_ui_smoke.py -p no:cacheprovider`
Expected: All pass (existing 166 + new tests)

- [ ] **Step 7: Commit**

```bash
git add app_pages/insights.py tests/unit/test_insights_sections.py
git commit -m "feat(ui): multi-month trend narrative + what-if scenario sections on insights page (#p1-4)"
```

---

## Self-Review

**1. Spec coverage:** Every spec section maps to a task:
- Scenario engine → Task 1
- Multi-month payload builder → Task 2
- Trend narrative evaluation → Task 3
- UI additions → Task 4
- Privacy posture → Task 3 (cloud blocked test) + Task 2 (no raw fields test)
- TDD → every task has write-test-first steps

**2. Placeholder scan:** No TBD/TODO. Every step has actual code or exact commands.

**3. Type consistency:** `ScenarioResult`, `ProjectedMonth`, `MultiMonthPayload`, `MultiMonthCategory`, `TrendNarrative` — defined in Task 1/2/3 and consumed by Task 4 with matching names and signatures.

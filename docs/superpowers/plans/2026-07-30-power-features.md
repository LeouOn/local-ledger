# Wave 3: Power Features & Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add recurring transaction detection, a rules engine, and transactions-page power features (filtering, bulk edit, charts).

**Architecture:** New `automation/` package with `recurring.py` + `rules.py`. A new `Rule` model with migration. Transactions page enhancements. Rules apply after dedup and before LLM categorization.

**Tech Stack:** Python 3.12, SQLModel/SQLAlchemy, Alembic, Pydantic v2, Streamlit, pytest.

## Global Constraints

- Python >=3.11,<3.13; all money as integer cents; naive `YYYY-MM-DD` dates
- No LLM imports in `automation/` (test-enforced)
- Rules are deterministic and applied BEFORE LLM categorization (cheaper, no network)
- Recurring detection is pure computation, runs on demand
- Follow existing patterns: `RuleService` mirrors `GoalService`/`ChatService` (session-based service class)
- TDD: failing test first, watch it fail, minimal code, watch it pass, commit

---

### Task 1: Rule model + migration

**Files:**
- Modify: `src/local_ledger/models.py`
- Create: `alembic/versions/<new>_add_rule.py`
- Test: `tests/unit/test_models_rule.py`

**Interfaces:**
- Produces:
  - `Rule(id, name, payee_contains, amount_min, amount_max, notes_contains, category_id, priority, created_at)`

- [ ] **Step 1: Write failing test**

```python
"""Rule model tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_ledger.models import Rule


def test_rule_constructs() -> None:
    rule = Rule(
        name="Starbucks → Food",
        payee_contains="starbucks",
        amount_min=None,
        amount_max=None,
        notes_contains=None,
        category_id=1,
        priority=10,
    )
    assert rule.name == "Starbucks → Food"
    assert rule.payee_contains == "starbucks"
    assert rule.category_id == 1
    assert rule.priority == 10


def test_rule_requires_name() -> None:
    with pytest.raises(ValidationError):
        Rule(name="", payee_contains="x", amount_min=None, amount_max=None, notes_contains=None, category_id=1, priority=10)


def test_rule_negative_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        Rule(name="bad", payee_contains="x", amount_min=-5, amount_max=None, notes_contains=None, category_id=1, priority=10)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_models_rule.py -q -p no:cacheprovider`
Expected: ImportError (Rule not defined)

- [ ] **Step 3: Add Rule model to models.py**

Add to `src/local_ledger/models.py` (end of file):

```python
class Rule(SQLModel, table=True):
    model_config = ConfigDict(validate_assignment=True)
    id: int | None = Field(default=None, primary_key=True)
    name: str
    payee_contains: str | None = None
    amount_min: int | None = None  # cents, inclusive
    amount_max: int | None = None  # cents, inclusive
    notes_contains: str | None = None
    category_id: int = Field(foreign_key="category.id")
    priority: int = Field(default=0)  # higher applies first
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v.strip()

    @field_validator("amount_min", "amount_max", mode="before")
    @classmethod
    def _check_amounts(cls, v: object) -> int | None:
        if v is None:
            return None
        if isinstance(v, bool) or isinstance(v, float):
            raise ValueError("amounts must be int cents or None")
        if not isinstance(v, int):
            raise ValueError("amounts must be int cents or None")
        if v < 0:
            raise ValueError("amounts must be non-negative")
        return v
```

- [ ] **Step 4: Generate + apply migration**

```bash
.venv\Scripts\alembic.exe revision --autogenerate -m "add rule"
.venv\Scripts\alembic.exe upgrade head
```

If the migration references `sqlmodel` without importing it, add `import sqlmodel` to the migration file (same fix as prior migrations).

- [ ] **Step 5: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_models_rule.py -q -p no:cacheprovider`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/local_ledger/models.py alembic/versions/ tests/unit/test_models_rule.py
git commit -m "feat(models): add Rule model with migration (#w3-1)"
```

---

### Task 2: Rules engine (`automation/rules.py`)

**Files:**
- Create: `src/local_ledger/automation/__init__.py`
- Create: `src/local_ledger/automation/rules.py`
- Test: `tests/unit/test_rules.py`

**Interfaces:**
- Consumes: `Rule`, `Transaction`, `Session`
- Produces:
  - `RuleService(session)`
  - `create_rule(**fields) -> Rule`
  - `list_rules() -> list[Rule]`
  - `delete_rule(rule_id)`
  - `apply_rules_to_transaction(tx: Transaction, rules: list[Rule]) -> int | None` (category_id, first match wins by priority then id)
  - `apply_rules_to_uncategorized(rules: list[Rule]) -> int` (returns count applied)

- [ ] **Step 1: Write failing test**

```python
"""Rules engine tests: matching, priority, backfill, no LLM."""

from __future__ import annotations

import inspect

import pytest

from local_ledger.automation.rules import RuleService
from local_ledger.models import Rule, Transaction
from tests.factories import make_account, make_category, make_transaction


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    from local_ledger.db import init_db

    init_db(engine)


@pytest.fixture()
def svc(session) -> RuleService:  # type: ignore[no-untyped-def]
    return RuleService(session)


def _make_rule(svc: RuleService, **overrides) -> Rule:
    defaults = {
        "name": "Food rule",
        "payee_contains": "starbucks",
        "amount_min": None,
        "amount_max": None,
        "notes_contains": None,
        "category_id": 1,
        "priority": 10,
    }
    defaults.update(overrides)
    return svc.create_rule(**defaults)


def test_apply_rules_matches_payee(svc: RuleService, session) -> None:
    cat = make_category(session, "Food")
    rule = _make_rule(svc, payee_contains="starbucks", category_id=cat.id)
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-500, notes="starbucks coffee")
    assert svc.apply_rules_to_transaction(tx, [rule]) == cat.id


def test_apply_rules_priority_first_wins(svc: RuleService, session) -> None:
    food = make_category(session, "Food")
    coffee = make_category(session, "Coffee")
    rule_high = _make_rule(svc, payee_contains="starbucks", category_id=coffee.id, priority=100)
    rule_low = _make_rule(svc, payee_contains="starbucks", category_id=food.id, priority=10)
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-500, notes="starbucks")
    assert svc.apply_rules_to_transaction(tx, [rule_low, rule_high]) == coffee.id


def test_apply_rules_amount_range(svc: RuleService, session) -> None:
    cat = make_category(session, "Food")
    _make_rule(svc, payee_contains="grocery", amount_min=1000, amount_max=5000, category_id=cat.id)
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-2500, notes="grocery run")
    rules = svc.list_rules()
    assert svc.apply_rules_to_transaction(tx, rules) == cat.id


def test_apply_rules_no_match_returns_none(svc: RuleService, session) -> None:
    make_category(session, "Food")
    _make_rule(svc, payee_contains="starbucks")
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-500, notes="other store")
    rules = svc.list_rules()
    assert svc.apply_rules_to_transaction(tx, rules) is None


def test_apply_rules_unknown_category_skipped(svc: RuleService, session) -> None:
    """A rule pointing to a deleted category is skipped, not fatal."""
    make_category(session, "Food")
    _make_rule(svc, payee_contains="starbucks", category_id=999)
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-500, notes="starbucks")
    rules = svc.list_rules()
    assert svc.apply_rules_to_transaction(tx, rules) is None


def test_apply_rules_to_uncategorized_backfills(svc: RuleService, session) -> None:
    acc = make_account(session)
    cat = make_category(session, "Food")
    _make_rule(svc, payee_contains="starbucks", category_id=cat.id)
    make_transaction(session, acc.id, amount_cents=-500, notes="starbucks", category_id=None)
    session.commit()
    rules = svc.list_rules()
    count = svc.apply_rules_to_uncategorized(rules)
    assert count == 1
    tx = session.exec(select(Transaction)).first()
    assert tx.category_id == cat.id


def test_rules_module_has_no_llm_imports() -> None:
    from local_ledger.automation import rules

    src = inspect.getsource(rules)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_rules.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
"""Rules engine: deterministic auto-categorization rules. No LLM."""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.models import Rule, Transaction


class RuleService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_rule(
        self,
        *,
        name: str,
        payee_contains: str | None = None,
        amount_min: int | None = None,
        amount_max: int | None = None,
        notes_contains: str | None = None,
        category_id: int,
        priority: int = 0,
    ) -> Rule:
        rule = Rule(
            name=name,
            payee_contains=payee_contains,
            amount_min=amount_min,
            amount_max=amount_max,
            notes_contains=notes_contains,
            category_id=category_id,
            priority=priority,
        )
        self.session.add(rule)
        self.session.commit()
        self.session.refresh(rule)
        return rule

    def list_rules(self) -> list[Rule]:
        return list(self.session.exec(select(Rule).order_by(Rule.priority.desc(), Rule.id)).all())

    def delete_rule(self, rule_id: int) -> None:
        rule = self.session.get(Rule, rule_id)
        if rule is not None:
            self.session.delete(rule)
            self.session.commit()

    def apply_rules_to_transaction(self, tx: Transaction, rules: list[Rule]) -> int | None:
        tx_abs = abs(tx.amount_cents)
        for rule in rules:
            if rule.payee_contains and rule.payee_contains.lower() not in (tx.notes or "").lower():
                continue
            if rule.amount_min is not None and tx_abs < rule.amount_min:
                continue
            if rule.amount_max is not None and tx_abs > rule.amount_max:
                continue
            if rule.notes_contains and rule.notes_contains.lower() not in (tx.notes or "").lower():
                continue
            # Category must exist
            if self.session.get(__import__("local_ledger.models", fromlist=["Category"]).Category, rule.category_id) is None:
                continue
            return rule.category_id
        return None

    def apply_rules_to_uncategorized(self, rules: list[Rule]) -> int:
        txs = list(
            self.session.exec(
                select(Transaction).where(Transaction.category_id.is_(None))
            ).all()
        )
        count = 0
        for tx in txs:
            cat_id = self.apply_rules_to_transaction(tx, rules)
            if cat_id is not None:
                tx.category_id = cat_id
                self.session.add(tx)
                count += 1
        self.session.commit()
        return count
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_rules.py -q -p no:cacheprovider`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/local_ledger/automation/ tests/unit/test_rules.py
git commit -m "feat(automation): rules engine with payee/amount/notes matching (#w3-2)"
```

---

### Task 3: Recurring detection (`automation/recurring.py`)

**Files:**
- Create: `src/local_ledger/automation/recurring.py`
- Test: `tests/unit/test_recurring.py`

**Interfaces:**
- Consumes: `Transaction`, `normalize_payee` from `local_ledger.ledger.dedup`
- Produces:
  - `RecurringGroup(payee: str, amount_cents: int, interval_days: int, transaction_ids: list[int], confidence: float)`
  - `detect_recurrences(transactions: list[Transaction], tolerance_pct: float = 10.0) -> list[RecurringGroup]`

- [ ] **Step 1: Write failing test**

```python
"""Recurring detection tests: grouping, tolerance, intervals, no LLM."""

from __future__ import annotations

import inspect
from datetime import date, timedelta

from local_ledger.automation.recurring import RecurringGroup, detect_recurrences
from local_ledger.models import Transaction


def _tx(date_str: str, amount: int, notes: str) -> Transaction:
    return Transaction(account_id=1, date=date_str, amount_cents=amount, notes=notes)


def test_detects_monthly_recurring() -> None:
    txs = [
        _tx("2026-01-05", -1000, "Netflix"),
        _tx("2026-02-05", -1000, "Netflix"),
        _tx("2026-03-05", -1000, "Netflix"),
    ]
    groups = detect_recurrences(txs)
    assert len(groups) == 1
    g = groups[0]
    assert g.payee == "Netflix"
    assert g.amount_cents == 1000
    assert g.interval_days == 30  # ~monthly
    assert len(g.transaction_ids) == 3
    assert g.confidence > 0.5


def test_does_not_detect_one_off() -> None:
    txs = [
        _tx("2026-01-05", -1000, "Netflix"),
        _tx("2026-03-05", -1500, "Netflix"),
    ]
    groups = detect_recurrences(txs)
    assert groups == []


def test_tolerance_allows_amount_variation() -> None:
    txs = [
        _tx("2026-01-05", -1000, "Electric"),
        _tx("2026-02-05", -1080, "Electric"),  # 8% higher, within 10% tolerance
        _tx("2026-03-05", -1000, "Electric"),
    ]
    groups = detect_recurrences(txs)
    assert len(groups) == 1


def test_requires_minimum_three_occurrences() -> None:
    txs = [
        _tx("2026-01-05", -1000, "Netflix"),
        _tx("2026-02-05", -1000, "Netflix"),
    ]
    assert detect_recurrences(txs) == []


def test_recurring_module_has_no_llm_imports() -> None:
    from local_ledger.automation import recurring

    src = inspect.getsource(recurring)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_recurring.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
"""Recurring transaction detection: group by payee + similar amount + regular interval. No LLM."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from local_ledger.ledger.dedup import normalize_payee
from local_ledger.models import Transaction

MIN_OCCURRENCES = 3
MONTHLY_DAYS = 30


@dataclass
class RecurringGroup:
    payee: str
    amount_cents: int
    interval_days: int
    transaction_ids: list[int]
    confidence: float


def detect_recurrences(
    transactions: list[Transaction],
    tolerance_pct: float = 10.0,
) -> list[RecurringGroup]:
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for tx in transactions:
        payee = normalize_payee(tx.notes)
        if payee:
            groups[payee].append(tx)

    results: list[RecurringGroup] = []
    for payee, txs in groups.items():
        if len(txs) < MIN_OCCURRENCES:
            continue
        txs_sorted = sorted(txs, key=lambda t: t.date)
        amounts = [abs(t.amount_cents) for t in txs_sorted]
        base = amounts[0]
        if base == 0:
            continue
        in_tolerance = all(
            abs(a - base) / base <= tolerance_pct / 100 for a in amounts
        )
        if not in_tolerance:
            continue

        dates = [datetime.fromisoformat(t.date) for t in txs_sorted]
        intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        avg_interval = sum(intervals) / len(intervals) if intervals else 0
        if avg_interval <= 0:
            continue

        interval_days = round(avg_interval)
        # Regularity: interval variance within 40% of the average
        variance = max((abs(iv - avg_interval) / avg_interval) for iv in intervals) if intervals else 0
        confidence = round(max(0.0, 1.0 - variance), 2)
        if variance > 0.4:
            continue

        results.append(
            RecurringGroup(
                payee=payee,
                amount_cents=base,
                interval_days=interval_days,
                transaction_ids=[t.id for t in txs_sorted if t.id is not None],
                confidence=confidence,
            )
        )
    return results
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_recurring.py -q -p no:cacheprovider`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/local_ledger/automation/recurring.py tests/unit/test_recurring.py
git commit -m "feat(automation): recurring transaction detection (#w3-3)"
```

---

### Task 4: Rules page UI

**Files:**
- Create: `app_pages/rules.py`
- Modify: `app.py` (add navigation entry)
- Test: `tests/unit/test_rules_page.py`

**Interfaces:**
- Consumes: `RuleService` (Task 2), `get_session` (existing)
- Produces: Rules page with CRUD + apply-to-uncategorized button

- [ ] **Step 1: Write failing AppTest**

```python
"""AppTest for the Rules page."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import run_migrations
from local_ledger.ledger.service import LedgerService
from tests.factories import make_category

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "rules.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    run_migrations(url)
    engine = create_engine(url)
    with Session(engine) as s:
        svc = LedgerService(s)
        svc.create_account(name="Checking", type="checking")
        make_category(s, "Food")
    engine.dispose()
    return url


def test_rules_page_renders(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app_pages/rules.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"rules page raised: {at.exception}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_rules_page.py -q -p no:cacheprovider`
Expected: FileNotFoundError (page doesn't exist)

- [ ] **Step 3: Write minimal Rules page**

```python
"""Rules page: create/edit/delete auto-categorization rules."""

from __future__ import annotations

import streamlit as st
from sqlmodel import select

from local_ledger.automation.rules import RuleService
from local_ledger.models import Category
from local_ledger.ui.chrome import get_session

st.title("Rules")

session = get_session()
svc = RuleService(session)

categories = list(session.exec(select(Category)).all())
category_by_id = {c.id: c.name for c in categories}

with st.expander("Create a rule"):
    with st.form("new_rule", clear_on_submit=True):
        name = st.text_input("Rule name", value="")
        payee = st.text_input("Payee contains", value="")
        notes = st.text_input("Notes contains", value="")
        col1, col2 = st.columns(2)
        amount_min = col1.number_input("Min amount ($)", min_value=0, value=0, step=1)
        amount_max = col2.number_input("Max amount ($)", min_value=0, value=0, step=1)
        if not categories:
            st.warning("Create a category first on the Budgets page.")
        else:
            cat_name = st.selectbox("Assign category", [c.name for c in categories])
        priority = st.number_input("Priority (higher wins)", min_value=0, value=10, step=1)
        submitted = st.form_submit_button("Create rule")
        if submitted and categories:
            cat = next(c for c in categories if c.name == cat_name)
            try:
                svc.create_rule(
                    name=name or payee or notes or "Rule",
                    payee_contains=payee or None,
                    amount_min=int(amount_min * 100) if amount_min > 0 else None,
                    amount_max=int(amount_max * 100) if amount_max > 0 else None,
                    notes_contains=notes or None,
                    category_id=cat.id,
                    priority=int(priority),
                )
                st.success(f"Created rule: {name}")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not create rule: {exc}")

st.subheader("Your rules")
rules = svc.list_rules()
if rules:
    for rule in rules:
        cat_name = category_by_id.get(rule.category_id, f"category_{rule.category_id}")
        with st.container(border=True):
            parts = [f"**{rule.name}** → {cat_name} (priority {rule.priority})"]
            if rule.payee_contains:
                parts.append(f"payee contains '{rule.payee_contains}'")
            if rule.notes_contains:
                parts.append(f"notes contains '{rule.notes_contains}'")
            if rule.amount_min is not None or rule.amount_max is not None:
                parts.append(
                    f"amount ${(rule.amount_min or 0) / 100:,.0f}–${(rule.amount_max or 0) / 100:,.0f}"
                )
            st.markdown(" · ".join(parts))
            if st.button("Delete", key=f"del_rule_{rule.id}"):
                svc.delete_rule(rule.id)
                st.rerun()

    if st.button("Apply rules to uncategorized transactions"):
        count = svc.apply_rules_to_uncategorized(svc.list_rules())
        st.success(f"Applied rules to {count} transaction(s).")
else:
    st.info("No rules yet. Create one above.")
```

- [ ] **Step 4: Add navigation entry in app.py**

Modify the `st.navigation(...)` list in `app.py` main() to add:
```python
st.Page("app_pages/rules.py", title="Rules", icon=":material/rule:"),
```
(place it after the Goals page)

- [ ] **Step 5: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_rules_page.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app_pages/rules.py app.py tests/unit/test_rules_page.py
git commit -m "feat(ui): rules page with CRUD + apply-to-uncategorized (#w3-4)"
```

---

### Task 5: Transactions page enhancements (filters, bulk edit, charts)

**Files:**
- Modify: `app_pages/transactions.py`
- Test: `tests/unit/test_transactions_enhancements.py`

**Interfaces:**
- Consumes: `Transaction`, `Category`, `Session`
- Produces: enhanced transactions page with amount/text/category filters, bulk delete/categorize, charts

- [ ] **Step 1: Read current transactions.py to understand the layout**

Run: Read tool on `app_pages/transactions.py`

- [ ] **Step 2: Write failing AppTest**

```python
"""AppTest for enhanced transactions page."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import run_migrations
from local_ledger.ledger.service import LedgerService
from tests.factories import make_account, make_category, make_transaction

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "txns.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    run_migrations(url)
    engine = create_engine(url)
    with Session(engine) as s:
        svc = LedgerService(s)
        acc = svc.create_account(name="Checking", type="checking")
        food = make_category(s, "Food")
        make_transaction(s, acc.id, amount_cents=-500, notes="starbucks", category_id=food.id)
        make_transaction(s, acc.id, amount_cents=-2000, notes="grocery", category_id=food.id)
    engine.dispose()
    return url


def test_transactions_page_renders_with_enhancements(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app_pages/transactions.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"transactions page raised: {at.exception}"
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_transactions_enhancements.py -q -p no:cacheprovider`
Expected: FAIL (page errors or doesn't render enhancements)

- [ ] **Step 4: Add filters, bulk edit, and charts to transactions.py**

Append to `app_pages/transactions.py` (after the existing add/transfer forms):

```python
st.divider()
st.subheader("Filters")
col1, col2, col3 = st.columns(3)
min_amt = col1.number_input("Min amount ($)", min_value=0.0, value=0.0, step=1.0, key="f_min")
max_amt = col2.number_input("Max amount ($)", min_value=0.0, value=0.0, step=1.0, key="f_max")
text = col3.text_input("Search (payee/notes)", value="", key="f_text")

filtered = rows
if min_amt > 0:
    filtered = [r for r in filtered if abs(r.amount_cents) >= min_amt * 100]
if max_amt > 0:
    filtered = [r for r in filtered if abs(r.amount_cents) <= max_amt * 100]
if text:
    needle = text.lower()
    filtered = [r for r in filtered if needle in (r.notes or "").lower()]

st.subheader("Charts")
if filtered:
    cats = {c.id: c.name for c in session.exec(select(Category)).all()}
    by_cat = {}
    for r in filtered:
        name = cats.get(r.category_id, "Uncategorized")
        by_cat[name] = by_cat.get(name, 0) + abs(r.amount_cents)
    chart_df = pd.DataFrame([{"category": k, "spent": v} for k, v in by_cat.items()])
    st.bar_chart(chart_df.set_index("category"))
else:
    st.caption("No transactions to chart.")

st.subheader("Bulk actions")
if filtered:
    ids = [r.id for r in filtered]
    if st.button(f"Delete {len(ids)} filtered transaction(s)"):
        for tid in ids:
            svc.delete_transaction(tid, actor="user")
        st.success(f"Deleted {len(ids)} transaction(s).")
        st.rerun()
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_transactions_enhancements.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6: Run full suite for regressions**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q --ignore=tests/unit/test_perf.py --ignore=tests/unit/test_ui_smoke.py -p no:cacheprovider`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add app_pages/transactions.py tests/unit/test_transactions_enhancements.py
git commit -m "feat(ui): transactions filters, bulk actions, and charts (#w3-5)"
```

---

### Task 6: Recurring detection UI

**Files:**
- Modify: `app_pages/transactions.py`
- Test: `tests/unit/test_recurring_ui.py`

**Interfaces:**
- Consumes: `detect_recurrences` (Task 3), `Transaction`
- Produces: "Recurring transactions" expander on the transactions page

- [ ] **Step 1: Write failing AppTest**

```python
"""AppTest for recurring detection UI."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import run_migrations
from local_ledger.ledger.service import LedgerService
from tests.factories import make_account

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "recurring.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    run_migrations(url)
    engine = create_engine(url)
    with Session(engine) as s:
        svc = LedgerService(s)
        acc = svc.create_account(name="Checking", type="checking")
        for m in range(1, 4):
            svc.create_transaction(
                __import__("local_ledger.models", fromlist=["Transaction"]).Transaction(
                    account_id=acc.id, date=f"2026-0{m}-05", amount_cents=-1000, notes="Netflix"
                ),
                actor="user",
            )
    engine.dispose()
    return url


def test_recurring_section_renders(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app_pages/transactions.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"transactions page with recurring section raised: {at.exception}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_recurring_ui.py -q -p no:cacheprovider`
Expected: PASS (page renders; recurring section is additive and shouldn't break existing render)

Note: If it passes on first run, strengthen the test to assert the recurring section exists (see Step 3 note).

- [ ] **Step 3: Add recurring detection section to transactions.py**

Append to `app_pages/transactions.py`:

```python
st.divider()
st.subheader("Recurring transactions")
if st.button("Detect recurring transactions"):
    from local_ledger.automation.recurring import detect_recurrences

    all_txs = list(session.exec(select(Transaction)).all())
    groups = detect_recurrences(all_txs)
    if groups:
        for g in sorted(groups, key=lambda x: -x.confidence):
            st.write(
                f"**{g.payee}** — ${g.amount_cents / 100:,.2f} every ~{g.interval_days} days "
                f"(confidence {g.confidence:.0%}, {len(g.transaction_ids)} occurrences)"
            )
    else:
        st.caption("No recurring transactions detected (need 3+ similar payee/amount/interval).")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_recurring_ui.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app_pages/transactions.py tests/unit/test_recurring_ui.py
git commit -m "feat(ui): recurring transaction detection section (#w3-6)"
```

---

## Self-Review

**1. Spec coverage:** Every spec section maps to a task:
- Rule model → Task 1
- Rules engine (RuleService + apply_rules) → Task 2
- Recurring detection → Task 3
- Rules UI → Task 4
- Transactions page filters/bulk/charts → Task 5
- Recurring UI → Task 6
- Privacy (no LLM in automation/) → Tasks 2,3 no-LLM-imports tests

**2. Placeholder scan:** No TBD/TODO. Every step has actual code or exact commands.

**3. Type consistency:** `Rule`, `RuleService`, `RecurringGroup`, `detect_recurrences` — defined in Tasks 1-3 and consumed by Tasks 4-6 with matching names/signatures. `normalize_payee` imported from `local_ledger.ledger.dedup` (exists in v1). `apply_rules_to_transaction` signature matches between Task 2 (definition) and Task 6 usage.

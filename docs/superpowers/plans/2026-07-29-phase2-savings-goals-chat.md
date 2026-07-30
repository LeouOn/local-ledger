# Phase 2: Savings Goals + Chat Sidebar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add savings goal tracking and a persistent chat sidebar with LLM conversation context.

**Architecture:** Two new models (SavingsGoal, ChatMessage), two services (GoalService, ChatService), a chat sidebar renderer, and a new Goals page. The chat sidebar lives in app.py's main() so it persists across page navigations.

**Tech Stack:** Python 3.12, SQLModel/SQLAlchemy, Alembic, Pydantic v2, Streamlit, pytest.

## Global Constraints

- Python >=3.11,<3.13; all money as integer cents; naive `YYYY-MM-DD` dates
- Aggregates-only for cloud chat; full aggregate context for local; no raw transaction fields in prompts
- All LLM calls are explicit user actions (buttons/inputs), never on page load
- Chat sidebar persists across page navigations via app.py main()
- Chat history persisted to database (survives refreshes)
- TDD: failing test first, watch it fail, minimal code, watch it pass, commit

---

### Task 1: Models + Migration

**Files:**
- Modify: `src/local_ledger/models.py`
- Create: `alembic/versions/<new>_add_savings_goals_and_chat.py`
- Test: `tests/unit/test_models_phase2.py`

**Interfaces:**
- Produces:
  - `SavingsGoal(id, name, target_cents, target_date, monthly_contribution_cents, created_at, notes)`
  - `ChatMessage(id, role, content, created_at)`

- [ ] **Step 1: Write failing test — new models construct**

```python
"""Phase 2 model tests: SavingsGoal + ChatMessage."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_ledger.models import ChatMessage, SavingsGoal


def test_savings_goal_constructs() -> None:
    goal = SavingsGoal(
        name="Emergency Fund",
        target_cents=1000000,
        target_date="2026-12-31",
        monthly_contribution_cents=50000,
    )
    assert goal.name == "Emergency Fund"
    assert goal.target_cents == 1000000
    assert goal.target_date == "2026-12-31"


def test_savings_goal_requires_positive_target() -> None:
    with pytest.raises(ValidationError):
        SavingsGoal(name="Bad", target_cents=0, target_date="2026-12-31", monthly_contribution_cents=100)


def test_chat_message_constructs() -> None:
    msg = ChatMessage(role="user", content="Why is food so high?")
    assert msg.role == "user"
    assert msg.content == "Why is food so high?"


def test_chat_message_rejects_invalid_role() -> None:
    with pytest.raises(ValidationError):
        ChatMessage(role="invalid", content="test")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_models_phase2.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError or ImportError

- [ ] **Step 3: Add models to models.py**

Add to `src/local_ledger/models.py`:

```python
class SavingsGoal(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    target_cents: int = Field(gt=0)
    target_date: str  # YYYY-MM-DD
    monthly_contribution_cents: int = Field(gt=0)
    created_at: datetime = Field(default_factory=_utcnow)
    notes: str | None = None

    @field_validator("target_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        return _validate_iso_date(v)


class ChatMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    role: str  # user | assistant
    content: str
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'")
        return v
```

- [ ] **Step 4: Generate migration**

```bash
.venv\Scripts\alembic.exe revision --autogenerate -m "add savings goals and chat"
```

- [ ] **Step 5: Apply migration**

```bash
.venv\Scripts\alembic.exe upgrade head
```

- [ ] **Step 6: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_models_phase2.py -q -p no:cacheprovider`
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add src/local_ledger/models.py alembic/versions/ tests/unit/test_models_phase2.py
git commit -m "feat(models): add SavingsGoal + ChatMessage with migration (#p2-1)"
```

---

### Task 2: GoalService — tracking + projection

**Files:**
- Create: `src/local_ledger/analysis/goals.py`
- Test: `tests/unit/test_goals.py`

**Interfaces:**
- Consumes: `SavingsGoal`, `Session`, ledger `Transaction`s
- Produces:
  - `GoalProgress(current_cents: int, target_cents: int, pct: float, monthly_rate_cents: float)`
  - `GoalService(session)`
  - `create_goal(name, target_cents, target_date, monthly_contribution_cents, notes) -> SavingsGoal`
  - `current_progress(goal) -> GoalProgress`
  - `projected_achievement(goal) -> str | None` (YYYY-MM-DD, or None if never)
  - `on_track(goal) -> bool`
  - `list_goals() -> list[SavingsGoal]`
  - `delete_goal(goal_id)`

- [ ] **Step 1: Write failing test — progress + projection math**

```python
"""GoalService tests: progress, projection, on-track logic."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from local_ledger.analysis.goals import GoalService
from local_ledger.models import SavingsGoal, Transaction
from tests.factories import make_account, make_category


@pytest.fixture()
def service(session: Session) -> GoalService:
    return GoalService(session)


def _make_goal(session: Session, **overrides) -> SavingsGoal:
    goal = SavingsGoal(
        name="Emergency Fund",
        target_cents=1000000,
        target_date="2026-12-31",
        monthly_contribution_cents=50000,
        **overrides,
    )
    session.add(goal)
    session.commit()
    session.refresh(goal)
    return goal


def test_create_goal(service: GoalService, session: Session) -> None:
    goal = service.create_goal("Vacation", 200000, "2026-06-30", 30000)
    assert goal.id is not None
    assert goal.name == "Vacation"


def test_current_progress_computes_from_ledger(service: GoalService, session: Session) -> None:
    acc = make_account(session)
    income = make_category(session, "Salary", is_income=True)
    make_transaction(session, acc.id, amount_cents=500000, date="2026-01-05", category_id=income.id)
    make_transaction(session, acc.id, amount_cents=-100000, date="2026-01-10")
    session.commit()

    goal = _make_goal(session, target_cents=1000000)
    progress = service.current_progress(goal)
    assert progress.current_cents == 400000  # income - expenses
    assert progress.target_cents == 1000000
    assert progress.pct == 40.0


def test_projected_achievement(service: GoalService, session: Session) -> None:
    acc = make_account(session)
    income = make_category(session, "Salary", is_income=True)
    make_transaction(session, acc.id, amount_cents=500000, date="2026-01-05", category_id=income.id)
    make_transaction(session, acc.id, amount_cents=-100000, date="2026-01-10")
    session.commit()

    goal = _make_goal(session, target_cents=1000000, target_date="2026-12-31")
    # 400K/mo savings rate, need 600K more = ~1.5 months from Jan → ~mid-Feb
    achievement = service.projected_achievement(goal)
    assert achievement is not None
    assert achievement > "2026-01-01"


def test_on_track_true_when_ahead(service: GoalService, session: Session) -> None:
    acc = make_account(session)
    income = make_category(session, "Salary", is_income=True)
    make_transaction(session, acc.id, amount_cents=1000000, date="2026-01-05", category_id=income.id)
    make_transaction(session, acc.id, amount_cents=-100000, date="2026-01-10")
    session.commit()

    goal = _make_goal(session, target_cents=500000, target_date="2026-12-31", monthly_contribution_cents=40000)
    assert service.on_track(goal) is True


def test_on_track_false_when_behind(service: GoalService, session: Session) -> None:
    goal = _make_goal(session, target_cents=1000000, target_date="2026-02-28", monthly_contribution_cents=10000)
    assert service.on_track(goal) is False


def test_zero_savings_rate_never_achieves(service: GoalService, session: Session) -> None:
    goal = _make_goal(session, target_cents=1000000, target_date="2026-12-31")
    assert service.projected_achievement(goal) is None


def test_list_and_delete_goals(service: GoalService, session: Session) -> None:
    g1 = service.create_goal("A", 100000, "2026-12-31", 10000)
    g2 = service.create_goal("B", 200000, "2026-12-31", 20000)
    goals = service.list_goals()
    assert len(goals) == 2
    service.delete_goal(g1.id)
    assert len(service.list_goals()) == 1


def test_goals_module_has_no_llm_imports() -> None:
    import inspect

    from local_ledger.analysis import goals

    src = inspect.getsource(goals)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_goals.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
"""Goal tracking service: progress, projection, on-track logic. No LLM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from local_ledger.models import SavingsGoal, Transaction


@dataclass
class GoalProgress:
    current_cents: int
    target_cents: int
    pct: float
    monthly_rate_cents: float


class GoalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_goal(
        self,
        name: str,
        target_cents: int,
        target_date: str,
        monthly_contribution_cents: int,
        notes: str | None = None,
    ) -> SavingsGoal:
        goal = SavingsGoal(
            name=name,
            target_cents=target_cents,
            target_date=target_date,
            monthly_contribution_cents=monthly_contribution_cents,
            notes=notes,
        )
        self.session.add(goal)
        self.session.commit()
        self.session.refresh(goal)
        return goal

    def list_goals(self) -> list[SavingsGoal]:
        return list(self.session.exec(select(SavingsGoal).order_by(SavingsGoal.created_at)).all())

    def delete_goal(self, goal_id: int) -> None:
        goal = self.session.get(SavingsGoal, goal_id)
        if goal is not None:
            self.session.delete(goal)
            self.session.commit()

    def current_progress(self, goal: SavingsGoal) -> GoalProgress:
        txns = list(
            self.session.exec(
                select(Transaction).where(Transaction.date >= goal.created_at.strftime("%Y-%m-%d"))
            ).all()
        )
        income = sum(t.amount_cents for t in txns if t.amount_cents > 0)
        expenses = sum(-t.amount_cents for t in txns if t.amount_cents < 0)
        current = income - expenses

        months_elapsed = max(
            1,
            (datetime.now(timezone.utc) - goal.created_at).days / 30.44,
        )
        monthly_rate = current / months_elapsed
        pct = (current / goal.target_cents * 100) if goal.target_cents > 0 else 0.0

        return GoalProgress(
            current_cents=current,
            target_cents=goal.target_cents,
            pct=round(min(pct, 999.9), 1),
            monthly_rate_cents=round(monthly_rate),
        )

    def projected_achievement(self, goal: SavingsGoal) -> str | None:
        progress = self.current_progress(goal)
        if progress.monthly_rate_cents <= 0:
            return None
        remaining = goal.target_cents - progress.current_cents
        if remaining <= 0:
            return goal.target_date
        months_needed = remaining / progress.monthly_rate_cents
        days_needed = int(months_needed * 30.44)
        base = datetime.now(timezone.utc)
        achievement = base.replace(year=base.year + (base.month + int(months_needed) - 1) // 12)
        from datetime import timedelta

        achievement = base + timedelta(days=days_needed)
        return achievement.strftime("%Y-%m-%d")

    def on_track(self, goal: SavingsGoal) -> bool:
        achievement = self.projected_achievement(goal)
        if achievement is None:
            return False
        return achievement <= goal.target_date
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_goals.py -q -p no:cacheprovider`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/local_ledger/analysis/goals.py tests/unit/test_goals.py
git commit -m "feat(analysis): goal tracking service with progress + projection (#p2-2)"
```

---

### Task 3: ChatService — message persistence

**Files:**
- Create: `src/local_ledger/analysis/chat.py`
- Test: `tests/unit/test_chat.py`

**Interfaces:**
- Consumes: `ChatMessage`, `Session`
- Produces:
  - `ChatService(session)`
  - `get_history(limit=50) -> list[ChatMessage]`
  - `add_message(role, content) -> ChatMessage`
  - `clear_history()`

- [ ] **Step 1: Write failing test**

```python
"""ChatService tests: message persistence."""

from __future__ import annotations

from local_ledger.analysis.chat import ChatService


def test_add_and_get_history(session) -> None:
    svc = ChatService(session)
    svc.add_message("user", "Hello")
    svc.add_message("assistant", "Hi there")
    history = svc.get_history()
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[1].role == "assistant"


def test_history_ordered_oldest_first(session) -> None:
    svc = ChatService(session)
    svc.add_message("user", "First")
    svc.add_message("user", "Second")
    history = svc.get_history()
    assert history[0].content == "First"
    assert history[1].content == "Second"


def test_history_limit(session) -> None:
    svc = ChatService(session)
    for i in range(60):
        svc.add_message("user", f"msg-{i}")
    assert len(svc.get_history()) == 50
    assert svc.get_history()[-1].content == "msg-59"


def test_clear_history(session) -> None:
    svc = ChatService(session)
    svc.add_message("user", "Hello")
    svc.clear_history()
    assert svc.get_history() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_chat.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
"""Chat message persistence service."""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.models import ChatMessage

HISTORY_LIMIT = 50


class ChatService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_history(self, limit: int = HISTORY_LIMIT) -> list[ChatMessage]:
        msgs = list(
            self.session.exec(
                select(ChatMessage).order_by(ChatMessage.created_at.desc()).limit(limit)
            ).all()
        )
        return list(reversed(msgs))

    def add_message(self, role: str, content: str) -> ChatMessage:
        msg = ChatMessage(role=role, content=content)
        self.session.add(msg)
        self.session.commit()
        self.session.refresh(msg)
        return msg

    def clear_history(self) -> None:
        for msg in self.session.exec(select(ChatMessage)).all():
            self.session.delete(msg)
        self.session.commit()
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_chat.py -q -p no:cacheprovider`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/local_ledger/analysis/chat.py tests/unit/test_chat.py
git commit -m "feat(analysis): chat message persistence service (#p2-3)"
```

---

### Task 4: Goals page

**Files:**
- Create: `app_pages/goals.py`
- Test: `tests/unit/test_goals_page.py` (AppTest)

**Interfaces:**
- Consumes: `GoalService` (Task 2), `TrendsService` (existing)
- Produces: Streamlit Goals page with create form + goal list with progress

- [ ] **Step 1: Write failing AppTest**

```python
"""AppTest for the Goals page."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.ledger.service import LedgerService
from tests.factories import make_account, make_category, make_transaction

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "goals.db"
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
        income = svc.create_category(name="Salary", is_income=True)
        make_transaction(s, acc.id, amount_cents=500000, date="2026-01-05", category_id=income.id)
        make_transaction(s, acc.id, amount_cents=-100000, date="2026-01-10")
        s.commit()
    engine.dispose()
    return url


def test_goals_page_renders(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app_pages/goals.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"goals page raised: {at.exception}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_goals_page.py -q -p no:cacheprovider`
Expected: ModuleNotFoundError

- [ ] **Step 3: Write minimal Goals page**

```python
"""Savings goals page: create goals, track progress, project achievement."""

from __future__ import annotations

import streamlit as st

from local_ledger.analysis.goals import GoalService
from local_ledger.ui.chrome import get_session

st.title("Savings Goals")

session = get_session()
service = GoalService(session)

with st.expander("Create a new goal"):
    with st.form("new_goal", clear_on_submit=True):
        name = st.text_input("Goal name", value="Emergency Fund")
        col1, col2 = st.columns(2)
        target = col1.number_input("Target amount ($)", min_value=100, value=10000, step=500)
        target_date = col2.text_input("Target date (YYYY-MM-DD)", value="2026-12-31")
        monthly = st.number_input("Planned monthly savings ($)", min_value=0, value=500, step=50)
        notes = st.text_area("Notes (optional)", value="")
        submitted = st.form_submit_button("Create goal")
        if submitted:
            try:
                service.create_goal(name, int(target * 100), target_date, int(monthly * 100), notes or None)
                st.success(f"Created goal: {name}")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not create goal: {exc}")

st.subheader("Your goals")
goals = service.list_goals()
if goals:
    for goal in goals:
        progress = service.current_progress(goal)
        achievement = service.projected_achievement(goal)
        on_track = service.on_track(goal)

        status = "On track" if on_track else "Behind schedule"
        emoji = "green" if on_track else "red"

        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            col1.markdown(f"**{goal.name}** — ${goal.target_cents / 100:,.0f} by {goal.target_date}")
            col2.markdown(f":{emoji}[{status}]")
            st.progress(min(progress.pct / 100, 1.0))
            st.caption(
                f"${progress.current_cents / 100:,.0f} saved ({progress.pct:.0f}%) · "
                f"Projected: {achievement or 'never'}"
            )
            if st.button("Delete", key=f"del_goal_{goal.id}"):
                service.delete_goal(goal.id)
                st.rerun()
else:
    st.info("No savings goals yet. Create one above.")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_goals_page.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app_pages/goals.py tests/unit/test_goals_page.py
git commit -m "feat(ui): savings goals page with create form + progress tracking (#p2-4)"
```

---

### Task 5: Chat sidebar + app.py wiring

**Files:**
- Create: `src/local_ledger/ui/chat_sidebar.py`
- Modify: `app.py`
- Test: `tests/unit/test_chat_sidebar.py` (AppTest)

**Interfaces:**
- Consumes: `ChatService` (Task 3), `generate_trend_narrative` (Phase 1), `st.session_state`
- Produces: `render_chat_sidebar(context: dict | None = None)`

- [ ] **Step 1: Write failing AppTest**

```python
"""AppTest for the chat sidebar."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.ledger.service import LedgerService
from tests.factories import make_account, make_category, make_transaction

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "chat.db"
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
        income = svc.create_category(name="Salary", is_income=True)
        make_transaction(s, acc.id, amount_cents=500000, date="2026-01-05", category_id=income.id)
        s.commit()
    engine.dispose()
    return url


def test_chat_sidebar_renders(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app_pages/insights.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"page with chat sidebar raised: {at.exception}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_chat_sidebar.py -q -p no:cacheprovider`
Expected: FAIL (chat sidebar not yet wired in app.py)

- [ ] **Step 3: Write chat_sidebar.py**

```python
"""Persistent chat sidebar renderer."""

from __future__ import annotations

import json

import streamlit as st

from local_ledger.analysis.chat import ChatService
from local_ledger.analysis.narrative import generate_trend_narrative
from local_ledger.analysis.multimonth import build_multimonth_payload, to_prompt
from local_ledger.ledger.trends import TrendsService
from local_ledger.llm.structured import make_openai_completer
from local_ledger.llm.providers import get_local_client, load_settings
from local_ledger.ui.chrome import get_session, degraded_message

MAX_HISTORY = 20


def _build_context_summary(context: dict | None) -> str:
    if not context:
        return ""
    parts = []
    if "month" in context:
        parts.append(f"Current month: {context['month']}")
    if "payload_summary" in context:
        parts.append(context["payload_summary"])
    return "\n".join(parts)


def render_chat_sidebar(context: dict | None = None) -> None:
    session = get_session()
    chat = ChatService(session)

    with st.sidebar:
        st.subheader("Budget Chat")
        degraded = degraded_message("evaluate")
        if degraded:
            st.warning(degraded)

        history = chat.get_history(limit=MAX_HISTORY)
        for msg in history:
            with st.chat_message(msg.role):
                st.markdown(msg.content)

        if prompt := st.chat_input("Ask about your budget...", key="chat_input"):
            chat.add_message("user", prompt)
            with st.chat_message("user"):
                st.markdown(prompt)

            if degraded:
                assistant_msg = f"I can't respond right now: {degraded}"
            else:
                try:
                    settings = load_settings(session)
                    client = get_local_client(settings)
                    completer = make_openai_completer(client)

                    context_text = _build_context_summary(context)
                    full_prompt = f"{context_text}\n\nUser question: {prompt}" if context_text else prompt

                    messages = [
                        {
                            "role": "system",
                            "content": (
                                "You are a budget assistant. Answer the user's question using ONLY "
                                "the provided aggregate financial data. Never invent numbers. "
                                "Keep answers concise and actionable."
                            ),
                        },
                        {"role": "user", "content": full_prompt},
                    ]
                    from local_ledger.llm.structured import extract

                    class ChatResponse(BaseModel):
                        answer: str

                    from pydantic import BaseModel

                    response = extract(completer, ChatResponse, messages, settings.local_model)
                    assistant_msg = response.answer
                except Exception as exc:
                    assistant_msg = f"Sorry, I couldn't process that: {exc}"

            chat.add_message("assistant", assistant_msg)
            with st.chat_message("assistant"):
                st.markdown(assistant_msg)
```

- [ ] **Step 4: Wire into app.py**

Modify `app.py` main() to render the chat sidebar after page.run():

```python
from local_ledger.ui.chat_sidebar import render_chat_sidebar

def main() -> None:
    # ... existing navigation + page.run() ...
    render_chat_sidebar(context=st.session_state.get("chat_context"))
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_chat_sidebar.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/local_ledger/ui/chat_sidebar.py app.py tests/unit/test_chat_sidebar.py
git commit -m "feat(ui): persistent chat sidebar with LLM conversation context (#p2-5)"
```

---

## Self-Review

**1. Spec coverage:** Every spec section maps to a task:
- SavingsGoal + ChatMessage models → Task 1
- GoalService (progress, projection, on-track) → Task 2
- ChatService (persistence) → Task 3
- Goals page → Task 4
- Chat sidebar + app.py wiring → Task 5
- Privacy posture → Task 5 (aggregates-only), Task 2 (no LLM imports)
- TDD → every task has write-test-first steps

**2. Placeholder scan:** No TBD/TODO. Every step has actual code or exact commands.

**3. Type consistency:** `GoalProgress`, `GoalService`, `ChatService`, `render_chat_sidebar` — defined in Tasks 2/3/5 and consumed consistently.

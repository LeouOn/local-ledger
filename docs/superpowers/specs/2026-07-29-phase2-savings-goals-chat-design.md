# Phase 2: Savings Goals + Chat Sidebar — Design Doc

**Date:** 2026-07-29
**Status:** Approved direction (two-phase build; Phase 2 of 2)
**Depends on:** Phase 1 complete (commit fcfa98c)

## Scope

Two features:

1. **Savings goal tracking** — Define goals (target amount + target date). The app tracks progress from actual income/expense data, projects achievement dates, and the LLM suggests spending adjustments to hit them faster.
2. **Full chat sidebar** — A persistent chat sidebar available on every page. The LLM maintains conversation context across questions. Aggregates-only privacy posture (local-first, cloud opt-in with consent).

## Out of scope (this phase)

- Recurring/schedule auto-detection (separate feature)
- New input formats (OFX/PDF — separate feature)
- Investment/portfolio tracking
- Multi-user or sharing

## Architecture

### Savings Goals

```
SavingsGoal (new model: name, target_cents, target_date, monthly_contribution_cents)
  → GoalService.current_progress(goal) → progress from actual ledger data
  → GoalService.projected_achievement(goal) → date when target is met at current rate
  → GoalService.on_track(goal) → bool: will goal hit target_date?
  → LLM suggests adjustments to close gaps
```

Progress is computed from actual ledger data: cumulative net income (income − expenses) over the goal's lifetime, compared to target. The projection uses the average monthly savings rate to forecast the achievement date.

### Chat Sidebar

```
app.py main()
  → st.navigation([...])  ← existing
  → page.run()            ← page content in main area
  → render_chat_sidebar() ← persistent sidebar with chat

ChatMessage (new model: role, content, created_at)
  → ChatService.add_message / get_history
  → Context builder: aggregates for selected month + conversation history
  → LLM responds with aggregates-only context
```

The chat sidebar lives in `app.py`'s `main()` so it persists across page navigations. Each page can inject context (e.g., the Insights page injects the current month's aggregates) via `st.session_state`. Chat history is persisted to the database so it survives refreshes.

## Components

### 1. `models.py` — Add `SavingsGoal` and `ChatMessage`

```python
class SavingsGoal(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    target_cents: int
    target_date: str  # YYYY-MM-DD
    monthly_contribution_cents: int  # planned monthly savings
    created_at: datetime = Field(default_factory=_utcnow)
    notes: str | None = None

class ChatMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    role: str  # user | assistant
    content: str
    created_at: datetime = Field(default_factory=_utcnow)
```

### 2. `analysis/goals.py` — Goal tracking service

- `GoalService(session)` 
- `create_goal(name, target_cents, target_date, monthly_contribution_cents, notes) -> SavingsGoal`
- `current_progress(goal) -> GoalProgress` — actual net savings vs target, percentage
- `projected_achievement(goal) -> str` — YYYY-MM-DD when target is met at current rate
- `on_track(goal) -> bool` — will goal hit target_date at current rate?
- `list_goals() -> list[SavingsGoal]`
- `delete_goal(goal_id)`

Progress computation: sum of (income − expenses) across all transactions from goal creation to now. Projection: if average monthly savings rate is R, months remaining = (target − current) / R.

### 3. `analysis/chat.py` — Chat service

- `ChatService(session)`
- `get_history(limit=50) -> list[ChatMessage]`
- `add_message(role, content) -> ChatMessage`
- `clear_history()`

### 4. `ui/chat_sidebar.py` — Chat sidebar renderer

- `render_chat_sidebar(context: dict | None = None)` — renders the persistent sidebar
- Uses `st.chat_message` and `st.chat_input`
- Builds LLM context from: conversation history + injected aggregates context
- Calls `narrative.generate_trend_narrative` or a new `chat_respond` function
- Persists messages via `ChatService`

### 5. `app.py` — Wire the chat sidebar

- After `page.run()`, render the chat sidebar
- Pass context from session_state

### 6. `app_pages/goals.py` — New Goals page

- List existing goals with progress bars
- Create new goal form
- Projected achievement dates + on-track indicators
- "Ask LLM how to reach this goal faster" button

### 7. UI additions to existing pages

- Insights page: inject current month's aggregates into chat context
- Settings page: clear chat history button

## Data flow

### Savings goal

```
User creates goal: "Emergency Fund", $10000, 2026-12-31, $500/mo
  → GoalService.create_goal(...)
  → Each month, actual net income is computed from ledger
  → GoalService.current_progress(goal) → 45% complete
  → GoalService.projected_achievement(goal) → 2027-03-15 (behind schedule)
  → LLM: "To hit your goal by Dec, increase monthly savings to $700. Consider cutting dining by 15%."
```

### Chat sidebar

```
User types "Why is my food spending so high?" in sidebar
  → Context builder: current month aggregates + conversation history
  → LLM responds with aggregates-only context
  → Message persisted to ChatMessage table
  → History visible across page navigations
```

## Error handling

- Goal with zero savings rate: projection returns "∞" (never), LLM suggests starting point
- Chat LLM unavailable: degradation banner in sidebar, input disabled
- Chat context too large: truncate oldest history, keep last 20 messages
- Cloud chat without consent: show consent prompt in sidebar

## Privacy

- Chat prompts: aggregates-only for cloud (same as v1)
- Chat history: stored locally in SQLite (never leaves machine unless cloud is used)
- Goals: purely local computation, no LLM needed for tracking

## Testing strategy

- **goals.py**: progress math, projection math, on-track logic, zero-savings edge case
- **chat.py**: message persistence, history ordering, clear history
- **chat_sidebar**: AppTest — sidebar renders, message sent → response appears, history persists
- **goals_page**: AppTest — page renders, create goal form, progress display
- **Invariants**: grep — no raw transaction fields in chat prompts; no LLM imports in goals.py

## Effort estimate

~10 todos: 2 models + 2 services + chat sidebar + goals page + app.py wiring + 4 test suites.

"""Trends engine: deterministic month summaries and on-read carryover.

Pure integer arithmetic over queried rows - no LLM, no floats, no stored
carryover. Carryover into month M for a category is computed by walking every
prior month in order: enabled months add (allocated - spent) to the running
balance; a disabled month resets it to zero.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, select

from local_ledger.models import Category, CategoryBudget, Transaction


@dataclass
class CategoryMonth:
    category_id: int
    name: str
    budgeted: int
    spent: int
    balance: int
    carryover: int


@dataclass
class MonthSummary:
    month: str
    income_cents: int = 0
    spent_cents: int = 0
    budgeted_cents: int = 0
    per_category: list[CategoryMonth] = field(default_factory=list)


def _month_of(date_str: str) -> str:
    return date_str[:7]


class TrendsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def month_summary(self, month: str) -> MonthSummary:
        return self._summary_from(
            month,
            list(self.session.exec(select(Transaction)).all()),
            list(self.session.exec(select(CategoryBudget)).all()),
            list(self.session.exec(select(Category)).all()),
        )

    def mom_deltas(self, month: str, n: int = 6) -> list[tuple[str, int, int]]:
        deltas: list[tuple[str, int, int]] = []
        y, m = (int(x) for x in month.split("-"))
        for _ in range(n):
            label = f"{y:04d}-{m:02d}"
            summary = self.month_summary(label)
            deltas.append((label, summary.income_cents, summary.spent_cents))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        return deltas

    # ---- pure core (session-free; easy to reason about) ----

    def _summary_from(
        self,
        month: str,
        transactions: list[Transaction],
        budgets: list[CategoryBudget],
        categories: list[Category],
    ) -> MonthSummary:
        summary = MonthSummary(month=month)

        # spent per (category, month) and income per month (integer sums).
        spent_by_cat_month: dict[tuple[int | None, str], int] = defaultdict(int)
        for tx in transactions:
            m = _month_of(tx.date)
            if tx.amount_cents < 0:
                spent_by_cat_month[(tx.category_id, m)] += -tx.amount_cents
            elif tx.amount_cents > 0 and m == month:
                summary.income_cents += tx.amount_cents

        # total spent in the target month (across all categories incl. None).
        for (_cat_id, m), spent in spent_by_cat_month.items():
            if m == month:
                summary.spent_cents += spent

        # budget rows indexed by (category, month).
        budget_by_cat_month: dict[tuple[int, str], CategoryBudget] = {
            (b.category_id, b.month): b for b in budgets
        }
        all_months = sorted({_month_of(tx.date) for tx in transactions} | {b.month for b in budgets})

        # which categories appear in this month?
        cats_this_month: set[int] = {
            cat_id
            for (cat_id, m) in spent_by_cat_month
            if m == month and cat_id is not None
        } | {b.category_id for b in budgets if b.month == month}

        for cat in sorted(categories, key=lambda c: c.id):
            if cat.id not in cats_this_month:
                continue
            allocated = budget_by_cat_month.get((cat.id, month))
            budgeted = allocated.allocated_cents if allocated else 0
            spent = spent_by_cat_month.get((cat.id, month), 0)
            carryover = self._carryover_into(cat.id, month, all_months, spent_by_cat_month, budget_by_cat_month)
            summary.per_category.append(
                CategoryMonth(
                    category_id=cat.id,
                    name=cat.name,
                    budgeted=budgeted,
                    spent=spent,
                    balance=budgeted - spent + carryover,
                    carryover=carryover,
                )
            )
            summary.budgeted_cents += budgeted

        return summary

    @staticmethod
    def _carryover_into(
        category_id: int,
        month: str,
        all_months: list[str],
        spent_by_cat_month: dict[tuple[int | None, str], int],
        budget_by_cat_month: dict[tuple[int, str], CategoryBudget],
    ) -> int:
        running = 0
        for m in all_months:
            if m >= month:
                break
            budget = budget_by_cat_month.get((category_id, m))
            if budget is None or not budget.carryover_enabled:
                running = 0
                continue
            allocated = budget.allocated_cents
            spent = spent_by_cat_month.get((category_id, m), 0)
            running += allocated - spent
        return running

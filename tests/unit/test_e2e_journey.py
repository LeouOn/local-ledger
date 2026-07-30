"""End-to-end user journey: import → budgets → evaluate → recommendation → followed.

Service-level (deterministic). The widget-rendering surface is covered by
test_ui_smoke; this exercises the real data pipeline end to end.
"""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.analysis.evaluate import deterministic_evaluate
from local_ledger.analysis.recommendations import RecommendationService
from local_ledger.db import init_db
from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ingest.importer import ImportService
from local_ledger.ledger.service import LedgerService
from local_ledger.ledger.trends import TrendsService
from local_ledger.models import CategoryBudget, Recommendation, Transaction


def test_full_user_journey(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)
    session = Session(engine)
    ledger = LedgerService(session)
    importer = ImportService(session)
    trends = TrendsService(session)
    recs = RecommendationService(session)

    # 1. account + categories
    acc = ledger.create_account(name="Checking", type="checking")
    food = ledger.create_category(name="Food")
    rent = ledger.create_category(name="Rent")
    fun = ledger.create_category(name="Fun")

    # 2. import 50 transactions
    parsed = [
        ParsedTransaction(date="2026-01-02", amount_cents=-(100 + i), payee_raw=f"Merchant {i}")
        for i in range(50)
    ]
    staged = importer.stage(parsed, acc.id, batch_label="jan-statement")
    importer.commit([s.id for s in staged], actor="import:jan.csv")

    # 3. set 3 budgets
    for cat, amt in [(food, 20000), (rent, 100000), (fun, 5000)]:
        session.add(CategoryBudget(category_id=cat.id, month="2026-01", allocated_cents=amt))
    session.commit()

    # 4. evaluate budget
    summary = trends.month_summary("2026-01")
    evaluation = deterministic_evaluate(summary)
    assert summary.spent_cents == sum(p.amount_cents for p in parsed) * -1
    assert isinstance(evaluation.anomalies, list)

    # 5. save a recommendation + mark followed
    rec = recs.save(
        month="2026-01",
        provider="ollama",
        model="llama3.3:8b",
        prompt_hash="h",
        body="Reduce Fun spend next month",
    )
    recs.set_followed(rec.id, True, actor="user")

    # assertions: 50 transactions, 3 budgets, 1 recommendation followed
    assert len(list(session.exec(select(Transaction)).all())) == 50
    assert len(list(session.exec(select(CategoryBudget)).all())) == 3
    recs_db = list(session.exec(select(Recommendation)).all())
    assert len(recs_db) == 1
    assert recs_db[0].followed is True

"""UI journey: review a staged batch and commit the intended subset.

Runs the real Import page under AppTest in a child process (Streamlit leaks
widget state across runs in one process) and drives the review controls: pick
some of the staged rows, accept them, commit them, then try to commit again.

What this pins down, which the service tests cannot: the page commits *only* the
rows the user accepted, leaves the rest staged, and a second click on commit
does not duplicate anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from local_ledger.db import run_migrations
from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ingest.importer import ImportService
from local_ledger.models import Account

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_CSV = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    import local_ledger.ui.chrome as chrome
    from local_ledger.llm.ollama import OllamaState

    chrome.detect_state = lambda *a, **k: OllamaState.NOT_RUNNING

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(r"{page}", default_timeout=60)
    at.run()
    if at.exception:
        sys.stderr.write(str(at.exception))
        sys.exit(1)

    def fail(msg):
        sys.stderr.write(msg)
        sys.exit(2)

    # The page must be showing the batch with all three rows pending.
    commit = at.button(key="csv_review_commit")
    if commit is None:
        fail("no commit button on the review block")
    if not commit.disabled:
        fail("commit must be disabled while nothing is accepted")

    rows = [r for r in Path("{staged_file}").read_text().splitlines() if r]
    if len(rows) != 3:
        fail("expected 3 seeded staged rows")

    # Review a subset: the first two, not the third.
    at.multiselect(key="csv_review_rows").set_value(rows[:2])
    at.run()
    if at.exception:
        fail(str(at.exception))
    at.button(key="csv_review_accept").click().run()
    if at.exception:
        fail(str(at.exception))

    commit = at.button(key="csv_review_commit")
    if commit is None or commit.disabled:
        fail("commit must be enabled once rows are accepted")
    if "2" not in commit.label:
        fail(f"commit button should name the accepted count, got {{commit.label!r}}")

    at.button(key="csv_review_commit").click().run()
    if at.exception:
        fail(str(at.exception))
    if not any("Committed 2" in el.value for el in at.success):
        fail("page did not report the committed rows")

    # A second click must not commit the leftover row or duplicate anything.
    commit = at.button(key="csv_review_commit")
    if commit is not None and not commit.disabled:
        fail("commit must be disabled after the accepted rows are written")
    print("JOURNEY-OK")
    """
)


@pytest.fixture()
def journey(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Seed a batch, drive the page, and return the db path for assertions."""
    db = tmp_path / "journey.db"
    url = f"sqlite:///{db.as_posix()}"
    os.environ["LOCAL_LEDGER_DB_URL"] = url
    run_migrations(url)

    with Session(create_engine(url)) as session:
        acc = Account(name="Checking")
        session.add(acc)
        session.commit()
        importer = ImportService(session)
        staged = importer.stage(
            [
                ParsedTransaction(
                    date="2026-01-05", amount_cents=-450, payee_raw="Coffee", source_row=2
                ),
                ParsedTransaction(
                    date="2026-01-06", amount_cents=-1250, payee_raw="Bookshop", source_row=3
                ),
                ParsedTransaction(
                    date="2026-01-07", amount_cents=-9900, payee_raw="Landlord", source_row=4
                ),
            ],
            acc.id,
            batch_label="bank.csv",
        )
        session.commit()
        labels = [
            f"#{s.id} | {s.date} | {s.amount_cents / 100:+.2f} | {s.payee_raw} | {s.dedup_status}"
            for s in staged
        ]

    staged_file = tmp_path / "rows.txt"
    staged_file.write_text("\n".join(labels))

    script = tmp_path / "journey.py"
    script.write_text(
        _CSV.format(page=REPO_ROOT / "app_pages" / "import_.py", staged_file=staged_file),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "LOCAL_LEDGER_DB_URL": url},
    )
    assert result.returncode == 0, f"journey failed:\n{result.stdout}\n{result.stderr[-3000:]}"
    assert "JOURNEY-OK" in result.stdout
    return url


def test_ui_commits_only_the_accepted_subset(journey: str) -> None:
    eng = create_engine(journey)
    try:
        with eng.connect() as conn:
            txs = conn.execute(
                text('SELECT date, amount_cents, imported_id FROM "transaction" ORDER BY date')
            ).fetchall()
            staged = conn.execute(
                text(
                    "SELECT id, review_state, committed_transaction_id FROM stagedtransaction "
                    "ORDER BY id"
                )
            ).fetchall()
    finally:
        eng.dispose()

    assert len(txs) == 2, "only the two accepted rows belong in the ledger"
    assert [t[0] for t in txs] == ["2026-01-05", "2026-01-06"]
    accepted = [r for r in staged if r[2] is not None]
    assert len(accepted) == 2
    # The row the user did not pick is still staged, still uncommitted.
    leftover = [r for r in staged if r[2] is None]
    assert len(leftover) == 1
    assert leftover[0][1] == "PENDING"

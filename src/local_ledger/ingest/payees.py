"""Payee and category resolution for ingest.

Imported rows carry payee and category *names*; the ledger stores foreign keys.
This module is the only place that turns one into the other, so CSV imports,
paste-parse and future sources all agree on matching rules:

- a payee is resolved case-insensitively on its normalized name and created on
  miss, because a statement payee that resolves to nothing is a lost payee;
- a category is only ever *matched*, never created, so a model or a sloppy
  column mapping cannot invent budget categories. An unmatched name stays
  unresolved and the transaction keeps ``category_id = NULL``.
"""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.ledger.dedup import normalize_payee
from local_ledger.models import Category, Payee


def resolve_or_create_payee(session: Session, name: str | None) -> Payee | None:
    """Return the payee for ``name``, creating it when it does not exist yet.

    Matching compares :func:`normalize_payee` on both sides, so a payee created
    elsewhere with a looser ``normalized_name`` (``LedgerService.create_payee``
    lowercases and strips only) is still recognised instead of duplicated.
    """
    cleaned = (name or "").strip()
    normalized = normalize_payee(cleaned)
    if not normalized:
        return None
    payees = list(session.exec(select(Payee)).all())
    for payee in payees:
        if payee.normalized_name == normalized:
            return payee
    for payee in payees:
        if normalize_payee(payee.normalized_name) == normalized:
            return payee
    payee = Payee(name=cleaned, normalized_name=normalized)
    session.add(payee)
    session.flush()
    return payee


def resolve_category(session: Session, name: str | None) -> Category | None:
    """Return the category whose name matches ``name``, or ``None``.

    Matching is exact-then-case-insensitive; ambiguous matches (the same name in
    two groups) resolve to ``None`` rather than an arbitrary pick.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return None
    exact = session.exec(select(Category).where(Category.name == cleaned)).all()
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    lowered = cleaned.lower()
    matches = [c for c in session.exec(select(Category)).all() if c.name.lower() == lowered]
    return matches[0] if len(matches) == 1 else None

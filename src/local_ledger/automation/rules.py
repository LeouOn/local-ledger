"""Rules engine: deterministic auto-categorization rules. No LLM."""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.ledger import audit
from local_ledger.models import Category, Rule, Transaction


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
        self.session.flush()
        audit.record(
            self.session,
            entity_type="rule",
            entity_id=rule.id,
            action="create",
            actor="user",
            after=rule,
        )
        self.session.commit()
        self.session.refresh(rule)
        return rule

    def list_rules(self) -> list[Rule]:
        return list(
            self.session.exec(select(Rule).order_by(Rule.priority.desc(), Rule.id)).all()
        )

    def delete_rule(self, rule_id: int) -> None:
        rule = self.session.get(Rule, rule_id)
        if rule is not None:
            audit.record(
                self.session,
                entity_type="rule",
                entity_id=rule.id,
                action="delete",
                actor="user",
                before=rule,
            )
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
            if self.session.get(Category, rule.category_id) is None:
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

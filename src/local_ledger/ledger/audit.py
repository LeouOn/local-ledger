"""Audit log helper. Every ledger mutation records one AuditLog row."""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session

from local_ledger.models import AuditLog


def _to_jsonable(model: Any) -> dict | None:
    if model is None:
        return None
    data = {k: v for k, v in model.model_dump().items() if v is not None}
    # enums/datetimes -> json-safe
    return json.loads(json.dumps(data, default=str, sort_keys=True))


def record(
    session: Session,
    *,
    entity_type: str,
    entity_id: int | None,
    action: str,
    actor: str,
    before: Any = None,
    after: Any = None,
) -> AuditLog:
    entry = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        before_json=json.dumps(_to_jsonable(before), default=str, sort_keys=True) if before is not None else None,
        after_json=json.dumps(_to_jsonable(after), default=str, sort_keys=True) if after is not None else None,
    )
    session.add(entry)
    return entry

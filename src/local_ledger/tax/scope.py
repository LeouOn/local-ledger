"""Persisted review scope: what a workspace's totals are allowed to cover.

Reconciliation used to infer its boundary from whatever classifications happened
to exist, which meant a report could be silently narrow - a 50,000c transaction
with no classification was simply not there. A stored run or packet has to be
able to prove which accounts and properties it considered, and "no transactions
in scope" has to be distinguishable from "an account was forgotten". So the
workspace owns its scope, versioned.

Three modes, because the distinction matters:

- ``UNCONFIGURED`` - the initial state. It cannot produce a complete run. This
  is the visible-incomplete case, not an error to be papered over.
- ``DOCUMENTS_ONLY`` - the user is reviewing documents without banking detail.
  Legitimate, and it is a real mode rather than an empty account list, because
  an empty list must never be silently treated as "everything".
- ``SELECTED_ACCOUNTS`` - an explicit account list, plus properties.

Scope is versioned so a run can record what it covered and still explain itself
after the scope changes. Changing membership or mode bumps the version and
invalidates results, which is the same atomic rule the rest of the tax evidence
uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import ConfigDict
from sqlmodel import Field, Session, SQLModel, select

from local_ledger.tax.models import TaxProperty, TaxWorkspace, WorkspaceStatus


class ReviewScopeMode(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    DOCUMENTS_ONLY = "DOCUMENTS_ONLY"
    SELECTED_ACCOUNTS = "SELECTED_ACCOUNTS"


class ScopeError(Exception):
    """A scope change was refused."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewScope(SQLModel, table=True):
    """The review boundary for one workspace, with a version for reproducibility."""

    model_config = ConfigDict(validate_assignment=True)

    workspace_id: str = Field(primary_key=True, foreign_key="taxworkspace.id")
    mode: str = Field(default=ReviewScopeMode.UNCONFIGURED, index=True)
    account_ids_json: str = "[]"
    property_ids_json: str = "[]"
    #: True once the user has confirmed the property list is deliberately empty
    #: (no rentals this year). Without it, an empty list is indistinguishable
    #: from never having asked.
    properties_confirmed: bool = False
    version: int = Field(default=1, index=True)
    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def account_ids(self) -> list[int]:
        return [int(i) for i in json.loads(self.account_ids_json or "[]")]

    @property
    def property_ids(self) -> list[str]:
        return [str(i) for i in json.loads(self.property_ids_json or "[]")]

    @property
    def is_configured(self) -> bool:
        return self.mode != ReviewScopeMode.UNCONFIGURED

    def covers_account(self, account_id: int) -> bool:
        return self.mode == ReviewScopeMode.SELECTED_ACCOUNTS and account_id in self.account_ids

    def covers_property(self, property_id: str) -> bool:
        return self.is_configured and property_id in self.property_ids

    def to_snapshot(self) -> dict:
        """The serialised scope a run stores, so it can explain itself later."""
        return {
            "mode": self.mode,
            "version": self.version,
            "account_ids": sorted(self.account_ids),
            "property_ids": sorted(self.property_ids),
            "properties_confirmed": self.properties_confirmed,
        }


class WorkspaceScopeService:
    """Reads and updates one workspace's review scope."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, workspace_id: str) -> ReviewScope:
        """Read the scope, or an UNCONFIGURED default without a database write."""
        scope = self.session.get(ReviewScope, workspace_id, populate_existing=True)
        if scope is not None:
            return scope
        return ReviewScope(workspace_id=workspace_id)

    def require_configured(self, workspace_id: str) -> ReviewScope:
        """The scope, or a refusal: an unconfigured workspace cannot total."""
        scope = self.get(workspace_id)
        if not scope.is_configured:
            raise ScopeError(
                "review scope is UNCONFIGURED for this workspace; select the accounts and "
                "properties in scope, or explicitly choose DOCUMENTS_ONLY, before totals "
                "can be claimed complete"
            )
        return scope

    def configure(
        self,
        *,
        workspace_id: str,
        mode: str,
        account_ids: list[int] | None = None,
        property_ids: list[str] | None = None,
        properties_confirmed: bool | None = None,
    ) -> ReviewScope:
        """Set the scope, validating every reference and bumping the version."""
        workspace = self.session.get(TaxWorkspace, workspace_id)
        if workspace is None:
            raise ScopeError(f"workspace {workspace_id} not found")
        if workspace.status == WorkspaceStatus.LOCKED:
            from local_ledger.tax.service import WorkspaceLockedError

            raise WorkspaceLockedError(
                f"Workspace for year {workspace.tax_year} ({workspace.jurisdiction}) is "
                "LOCKED and immutable"
            )

        scope = self.get(workspace_id)
        resolved_mode = ReviewScopeMode(mode)
        accounts = sorted({int(a) for a in (account_ids or [])})
        props = sorted({str(p) for p in (property_ids or [])})

        if (
            resolved_mode is ReviewScopeMode.SELECTED_ACCOUNTS
            and not accounts
            and not props
            and not (properties_confirmed or scope.properties_confirmed)
        ):
            raise ScopeError(
                "SELECTED_ACCOUNTS requires at least one account, or an explicit "
                "confirmation that no accounts or properties apply"
            )

        # Validate references: a scope that names a property from another
        # workspace would let a report span years.
        if props:
            known = {
                p.id
                for p in self.session.exec(
                    select(TaxProperty).where(TaxProperty.workspace_id == workspace_id)
                ).all()
            }
            unknown = [p for p in props if p not in known]
            if unknown:
                raise ScopeError(
                    f"properties {unknown} do not belong to workspace {workspace_id}"
                )
        if accounts:
            # Imported here: this module is registered from local_ledger/models.py,
            # so importing it at module scope would be a cycle.
            from local_ledger.models import Account

            # An account with no rows yet is still legitimately in scope, so the
            # check is that the account exists at all, not that it has activity.
            existing = {
                a.id
                for a in self.session.exec(select(Account).where(Account.id.in_(accounts))).all()
            }
            missing = [a for a in accounts if a not in existing]
            if missing:
                raise ScopeError(f"accounts {missing} do not exist")

        confirmed = properties_confirmed
        if confirmed is None:
            confirmed = bool(props) or scope.properties_confirmed

        scope.mode = resolved_mode
        scope.account_ids_json = json.dumps(accounts)
        scope.property_ids_json = json.dumps(props)
        scope.properties_confirmed = confirmed
        scope.version += 1
        scope.updated_at = _utcnow()
        self.session.add(scope)

        # A run that covered the old scope must not stay CURRENT. Done in the
        # same transaction as the change, so a report is either staled or the
        # scope never moved. Imported here: service.py reaches back into this
        # module's tables, so a module-level import would be a cycle.
        from local_ledger.tax.service import TaxWorkspaceService

        TaxWorkspaceService(self.session)._invalidate_downstream(workspace_id)
        self.session.commit()
        return scope

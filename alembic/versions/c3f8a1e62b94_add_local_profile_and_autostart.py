"""add local_profile and local_autostart to providersettings

Revision ID: c3f8a1e62b94
Revises: d96730f10a82
Create Date: 2026-09-14 11:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  # needed for sqlmodel.sql.sqltypes below


# revision identifiers, used by Alembic.
revision: str = 'c3f8a1e62b94'
down_revision: Union[str, Sequence[str], None] = 'd96730f10a82'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('providersettings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'local_profile',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default='ollama',
            )
        )
        batch_op.add_column(
            sa.Column(
                'local_autostart',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('providersettings', schema=None) as batch_op:
        batch_op.drop_column('local_autostart')
        batch_op.drop_column('local_profile')

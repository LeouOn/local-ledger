"""add theme_mode to providersettings

Revision ID: d96730f10a82
Revises: 51c952895790
Create Date: 2026-08-19 21:02:11.131474

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  # needed for sqlmodel.sql.sqltypes below


# revision identifiers, used by Alembic.
revision: str = 'd96730f10a82'
down_revision: Union[str, Sequence[str], None] = '51c952895790'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('providersettings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'theme_mode',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="light",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('providersettings', schema=None) as batch_op:
        batch_op.drop_column('theme_mode')

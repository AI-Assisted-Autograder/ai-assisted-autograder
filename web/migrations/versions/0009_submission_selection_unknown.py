"""Distinguish unsynchronized submissions from superseded submissions.

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("submission", "is_active_attempt", existing_type=sa.Boolean(),
                    nullable=True, server_default=None)


def downgrade() -> None:
    op.execute("UPDATE submission SET is_active_attempt = FALSE WHERE is_active_attempt IS NULL")
    op.alter_column("submission", "is_active_attempt", existing_type=sa.Boolean(),
                    nullable=False, server_default=sa.true())

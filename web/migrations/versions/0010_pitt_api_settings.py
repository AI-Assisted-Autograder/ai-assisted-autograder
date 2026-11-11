"""Store the shared Pitt API credential independently of model selection.

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    table = op.create_table(
        "pitt_api_settings",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("encrypted_key", sa.Text(), nullable=True),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name=op.f("ck_pitt_api_settings_singleton")),
        sa.CheckConstraint("key_version >= 0", name=op.f("ck_pitt_api_settings_key_version")),
    )
    op.bulk_insert(table, [{"id": 1, "encrypted_key": None, "key_version": 0}])


def downgrade() -> None:
    op.drop_table("pitt_api_settings")

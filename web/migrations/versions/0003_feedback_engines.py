"""Add the backend connection engine to global LLM configuration.

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_provider_config",
        sa.Column("engine", sa.Text(), server_default="vertex", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_llm_provider_config_engine"), "llm_provider_config",
        "engine IN ('vertex', 'pitt', 'gemini_api')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_llm_provider_config_engine"), "llm_provider_config", type_="check")
    op.drop_column("llm_provider_config", "engine")

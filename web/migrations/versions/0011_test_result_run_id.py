"""Key test results by autograder run, so a retried report adds no duplicates.

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("test_result", sa.Column("run_id", sa.Text(), nullable=True))
    op.create_index(
        "uq_test_result_target_run",
        "test_result",
        ["submission_target_id", "run_id"],
        unique=True,
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_test_result_target_run", table_name="test_result")
    op.drop_column("test_result", "run_id")

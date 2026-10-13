"""Add single-run protection and per-submission nightly audit rows.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_trigger_run_one_running",
        "trigger_run",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        "trigger_run_submission",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("trigger_run_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_trigger_run_submission"),
        sa.ForeignKeyConstraint(["trigger_run_id"], ["trigger_run.id"], name="fk_trigger_run_submission_run"),
        sa.ForeignKeyConstraint(["submission_id"], ["submission.id"], name="fk_trigger_run_submission_submission"),
        sa.UniqueConstraint("trigger_run_id", "submission_id", name="uq_trigger_run_submission_run_submission"),
        sa.CheckConstraint(
            "status IN ('AUTHORIZED', 'RERUN_SUCCEEDED', 'RERUN_FAILED')",
            name="ck_trigger_run_submission_status",
        ),
    )
    op.create_index("ix_trigger_run_submission_trigger_run_id", "trigger_run_submission", ["trigger_run_id"])
    op.create_index("ix_trigger_run_submission_submission_id", "trigger_run_submission", ["submission_id"])


def downgrade() -> None:
    op.drop_table("trigger_run_submission")
    op.drop_index("uq_trigger_run_one_running", table_name="trigger_run")

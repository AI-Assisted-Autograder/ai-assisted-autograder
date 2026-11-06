"""Allow nightly runs to record accepted reruns before completion polling.

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_trigger_run_submission_status", "trigger_run_submission", type_="check")
    op.create_check_constraint(
        "ck_trigger_run_submission_status",
        "trigger_run_submission",
        "status IN ('AUTHORIZED', 'RERUN_SUBMITTED', 'RERUN_SUCCEEDED', 'RERUN_FAILED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_trigger_run_submission_status", "trigger_run_submission", type_="check")
    op.create_check_constraint(
        "ck_trigger_run_submission_status",
        "trigger_run_submission",
        "status IN ('AUTHORIZED', 'RERUN_SUCCEEDED', 'RERUN_FAILED')",
    )

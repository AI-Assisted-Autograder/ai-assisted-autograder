"""Record submission AI outcomes for external rerun automation.

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("submission", sa.Column("ai_status", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_submission_ai_status"), "submission",
        "ai_status IN ('SUCCEEDED', 'FAILED')",
    )
    # Existing target errors establish failure; an absent report does not prove success.
    op.execute(sa.text("""
        UPDATE submission AS s
        SET ai_status = 'FAILED', ai_graded_submitted_at = NULL
        WHERE EXISTS (
            SELECT 1 FROM submission_target AS st
            WHERE st.submission_id = s.id AND st.ai_needed AND st.last_error IS NOT NULL
        )
    """))


def downgrade() -> None:
    op.drop_constraint(op.f("ck_submission_ai_status"), "submission", type_="check")
    op.drop_column("submission", "ai_status")

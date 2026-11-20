"""Store replayable grading results on successful AI feedback."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_feedback", sa.Column("cache_digest", sa.Text(), nullable=True))
    op.add_column("ai_feedback", sa.Column("cached_result", postgresql.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_feedback", "cached_result")
    op.drop_column("ai_feedback", "cache_digest")

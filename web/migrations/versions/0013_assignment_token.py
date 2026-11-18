"""Generate one private connector token per assignment."""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assignment", sa.Column(
        "assignment_token", sa.Uuid(), nullable=False, server_default=sa.func.gen_random_uuid(),
    ))
    op.create_unique_constraint("uq_assignment_assignment_token", "assignment", ["assignment_token"])


def downgrade() -> None:
    op.drop_column("assignment", "assignment_token")

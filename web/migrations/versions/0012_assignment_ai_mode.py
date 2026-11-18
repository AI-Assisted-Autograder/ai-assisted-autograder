"""Store the assignment's AI mode and preserve ambiguous legacy configurations."""

from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assignment", sa.Column("ai_mode", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_assignment_ai_mode"), "assignment", "ai_mode IN ('GRADING', 'FEEDBACK')")
    op.execute("""
        UPDATE assignment AS a SET ai_mode = t.mode
        FROM (
            SELECT assignment_id, CASE
                WHEN count(*) FILTER (WHERE analysis_config->>'behavior' = 'ai_grading') = count(*)
                    THEN 'GRADING'
                WHEN count(*) = 1 AND min(analysis_config->>'behavior') = 'functional_test_feedback'
                    THEN 'FEEDBACK'
            END AS mode
            FROM grading_target GROUP BY assignment_id
        ) AS t
        WHERE a.id = t.assignment_id AND t.mode IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_assignment_ai_mode"), "assignment", type_="check")
    op.drop_column("assignment", "ai_mode")

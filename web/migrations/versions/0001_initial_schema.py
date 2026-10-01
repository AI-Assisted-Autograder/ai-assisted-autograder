"""Initial schema from docs/architecture/02-data-model.puml

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def uuid_pk() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def tstz(name: str, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if now else None,
        nullable=nullable,
    )


def flag(name: str, default: bool) -> sa.Column:
    return sa.Column(name, sa.Boolean(), server_default=sa.text(str(default).lower()), nullable=False)


def upgrade() -> None:
    # --- Instructor configuration -----------------------------------------
    op.create_table(
        "course",
        uuid_pk(),
        sa.Column("name", sa.Text()),
        sa.Column("term", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_course"),
    )

    op.create_table(
        "assignment",
        uuid_pk(),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_assignment_id", sa.Text(), nullable=False),
        sa.Column("grading_mode", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        flag("is_active", True),
        tstz("created_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("id", name="pk_assignment"),
        sa.ForeignKeyConstraint(["course_id"], ["course.id"], name="fk_assignment_course_id_course"),
        sa.UniqueConstraint("platform", "platform_assignment_id", name="uq_assignment_platform_platform_assignment_id"),
        sa.CheckConstraint("platform IN ('GRADESCOPE')", name=op.f("ck_assignment_platform")),
        sa.CheckConstraint("grading_mode IN ('IMMEDIATE', 'NIGHTLY')", name=op.f("ck_assignment_grading_mode")),
    )
    op.create_index("ix_assignment_course_id", "assignment", ["course_id"])

    op.create_table(
        "grading_target",
        uuid_pk(),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("max_score", sa.Numeric()),
        sa.Column("display_order", sa.Integer()),
        sa.PrimaryKeyConstraint("id", name="pk_grading_target"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignment.id"], name="fk_grading_target_assignment_id_assignment"),
        sa.UniqueConstraint("assignment_id", "identifier", name="uq_grading_target_assignment_id_identifier"),
        sa.CheckConstraint("kind IN ('METHOD', 'CLASS', 'FILE')", name=op.f("ck_grading_target_kind")),
    )

    op.create_table(
        "rubric",
        uuid_pk(),
        sa.Column("grading_target_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("criteria_text", sa.Text()),
        sa.Column("feedback_guidance", sa.Text()),
        flag("is_current", False),
        tstz("created_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("id", name="pk_rubric"),
        sa.ForeignKeyConstraint(["grading_target_id"], ["grading_target.id"], name="fk_rubric_grading_target_id_grading_target"),
        sa.UniqueConstraint("grading_target_id", "version", name="uq_rubric_grading_target_id_version"),
    )
    op.create_index(
        "uq_rubric_current_per_target", "rubric", ["grading_target_id"],
        unique=True, postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "llm_provider_config",
        uuid_pk(),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        flag("is_enabled", True),
        flag("is_primary", False),
        sa.PrimaryKeyConstraint("id", name="pk_llm_provider_config"),
        sa.CheckConstraint("provider IN ('ANTHROPIC', 'OPENAI', 'GOOGLE')", name=op.f("ck_llm_provider_config_provider")),
    )
    op.create_index(
        "uq_llm_provider_config_one_primary", "llm_provider_config", ["is_primary"],
        unique=True, postgresql_where=sa.text("is_primary"),
    )

    # --- Runtime: written by the autograder script ------------------------
    op.create_table(
        "submission",
        uuid_pk(),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("platform_submission_id", sa.Text(), nullable=False),
        sa.Column("student_identifier", sa.Text()),
        tstz("submitted_at", nullable=False),
        tstz("ai_authorized_at"),
        # NULL until grading finishes; the diagram's NOT NULL is a mistake.
        tstz("ai_graded_submitted_at"),
        flag("is_active_attempt", True),
        sa.PrimaryKeyConstraint("id", name="pk_submission"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignment.id"], name="fk_submission_assignment_id_assignment"),
        sa.UniqueConstraint("assignment_id", "platform_submission_id", name="uq_submission_assignment_id_platform_submission_id"),
    )

    op.create_table(
        "submission_target",
        uuid_pk(),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("grading_target_id", sa.Uuid(), nullable=False),
        flag("ai_needed", True),
        tstz("completed_at"),
        sa.Column("last_error", sa.Text()),
        sa.Column("agreement_score", sa.Numeric()),
        sa.Column("score_spread", sa.Numeric()),
        sa.Column("selection_reason", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_submission_target"),
        sa.ForeignKeyConstraint(["submission_id"], ["submission.id"], name="fk_submission_target_submission_id_submission"),
        sa.ForeignKeyConstraint(["grading_target_id"], ["grading_target.id"], name="fk_submission_target_grading_target_id_grading_target"),
        sa.UniqueConstraint("submission_id", "grading_target_id", name="uq_submission_target_submission_id_grading_target_id"),
    )
    op.create_index("ix_submission_target_grading_target_id", "submission_target", ["grading_target_id"])
    op.create_index(
        "ix_submission_target_pending", "submission_target", ["submission_id"],
        postgresql_where=sa.text("ai_needed AND completed_at IS NULL"),
    )

    op.create_table(
        "test_result",
        uuid_pk(),
        sa.Column("submission_target_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("functional_test_feedback", sa.Text()),
        tstz("recorded_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("id", name="pk_test_result"),
        sa.ForeignKeyConstraint(["submission_target_id"], ["submission_target.id"], name="fk_test_result_submission_target_id_submission_target"),
        sa.CheckConstraint("status IN ('PASS', 'FAIL', 'ERROR', 'SKIP')", name=op.f("ck_test_result_status")),
    )
    op.create_index("ix_test_result_submission_target_id", "test_result", ["submission_target_id"])

    # --- AI-generated feedback --------------------------------------------
    op.create_table(
        "ai_feedback",
        uuid_pk(),
        sa.Column("submission_target_id", sa.Uuid(), nullable=False),
        sa.Column("rubric_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("suggested_score", sa.Numeric()),
        sa.Column("confidence", sa.Numeric()),
        flag("is_selected", False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        tstz("generated_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_feedback"),
        sa.ForeignKeyConstraint(["submission_target_id"], ["submission_target.id"], name="fk_ai_feedback_submission_target_id_submission_target"),
        sa.ForeignKeyConstraint(["rubric_id"], ["rubric.id"], name="fk_ai_feedback_rubric_id_rubric"),
    )
    op.create_index("ix_ai_feedback_submission_target_id", "ai_feedback", ["submission_target_id"])
    op.create_index("ix_ai_feedback_rubric_id", "ai_feedback", ["rubric_id"])
    op.create_index(
        "uq_ai_feedback_one_selected", "ai_feedback", ["submission_target_id"],
        unique=True, postgresql_where=sa.text("is_selected"),
    )

    op.create_table(
        "ai_feedback_review",
        uuid_pk(),
        sa.Column("ai_feedback_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer", sa.Text()),
        sa.Column("instructor_rating", sa.Integer()),
        sa.Column("instructor_comment", sa.Text()),
        tstz("reviewed_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_feedback_review"),
        sa.ForeignKeyConstraint(["ai_feedback_id"], ["ai_feedback.id"], name="fk_ai_feedback_review_ai_feedback_id_ai_feedback"),
    )
    op.create_index("ix_ai_feedback_review_ai_feedback_id", "ai_feedback_review", ["ai_feedback_id"])

    # --- Audit ------------------------------------------------------------
    op.create_table(
        "trigger_run",
        uuid_pk(),
        sa.Column("trigger", sa.Text(), nullable=False),
        tstz("started_at", nullable=False, now=True),
        tstz("finished_at"),
        sa.Column("status", sa.Text()),
        sa.Column("submissions_authorized", sa.Integer()),
        sa.Column("submissions_rerun", sa.Integer()),
        sa.Column("submissions_failed_to_trigger", sa.Integer()),
        sa.Column("log_uri", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_trigger_run"),
        sa.CheckConstraint("trigger IN ('SCHEDULE', 'MANUAL')", name=op.f("ck_trigger_run_trigger")),
    )


def downgrade() -> None:
    for table in (
        "trigger_run",
        "ai_feedback_review",
        "ai_feedback",
        "test_result",
        "submission_target",
        "submission",
        "llm_provider_config",
        "rubric",
        "grading_target",
        "assignment",
        "course",
    ):
        op.drop_table(table)

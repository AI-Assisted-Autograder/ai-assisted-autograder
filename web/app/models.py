"""SQLAlchemy models for the grading database.

Mirrors docs/architecture/02-data-model.puml. Where this file departs from
the diagram, a comment says why. Enum-like columns are text with a CHECK
constraint, as the diagram specifies, rather than native Postgres enums,
so adding a value is a one-line migration.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

# Deterministic constraint names, so Alembic migrations can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )


def created_now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


PLATFORMS = ("GRADESCOPE",)
GRADING_MODES = ("IMMEDIATE", "NIGHTLY")
TARGET_KINDS = ("METHOD", "CLASS", "FILE")
PROVIDERS = ("ANTHROPIC", "OPENAI", "GOOGLE")
ENGINES = ("vertex", "pitt", "gemini_api")
TEST_STATUSES = ("PASS", "FAIL", "ERROR", "SKIP")
TRIGGERS = ("SCHEDULE", "MANUAL")


# ---------------------------------------------------------------------
# Instructor configuration
# ---------------------------------------------------------------------
class Course(Base):
    __tablename__ = "course"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str | None] = mapped_column(Text)
    term: Mapped[str | None] = mapped_column(Text)


class Assignment(Base):
    __tablename__ = "assignment"
    __table_args__ = (
        UniqueConstraint("platform", "platform_assignment_id"),
        one_of("platform", PLATFORMS),
        one_of("grading_mode", GRADING_MODES),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("course.id"), index=True)
    platform: Mapped[str] = mapped_column(Text)
    platform_assignment_id: Mapped[str] = mapped_column(Text)
    grading_mode: Mapped[str] = mapped_column(Text)
    # Open-ended (JAVA, PYTHON, ...): no CHECK, because the valid set is
    # whatever the AI phase's per-language mapping supports (AD-12).
    language: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(server_default=true())
    created_at: Mapped[datetime] = created_now()


class GradingTarget(Base):
    __tablename__ = "grading_target"
    __table_args__ = (
        # Not in the diagram, but AD-3 treats identifiers as the lookup key
        # within an assignment, so two targets with one name would be ambiguous.
        UniqueConstraint("assignment_id", "identifier"),
        one_of("kind", TARGET_KINDS),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assignment.id"))
    identifier: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    max_score: Mapped[Decimal | None] = mapped_column(Numeric)
    display_order: Mapped[int | None] = mapped_column(Integer)
    analysis_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class Rubric(Base):
    __tablename__ = "rubric"
    __table_args__ = (
        UniqueConstraint("grading_target_id", "version"),
        # At most one current rubric per target.
        Index(
            "uq_rubric_current_per_target",
            "grading_target_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    grading_target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grading_target.id"))
    version: Mapped[int] = mapped_column(Integer)
    criteria_text: Mapped[str | None] = mapped_column(Text)
    feedback_guidance: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(server_default=false())
    created_at: Mapped[datetime] = created_now()


class LlmProviderConfig(Base):
    """Global, as drawn: not yet linked to a course or assignment."""

    __tablename__ = "llm_provider_config"
    __table_args__ = (
        one_of("provider", PROVIDERS),
        one_of("engine", ENGINES),
        # At most one primary provider.
        Index(
            "uq_llm_provider_config_one_primary",
            "is_primary",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(Text, server_default="vertex")
    model_name: Mapped[str] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(server_default=true())
    is_primary: Mapped[bool] = mapped_column(server_default=false())


# ---------------------------------------------------------------------
# Runtime: written by the autograder script
# ---------------------------------------------------------------------
class Submission(Base):
    __tablename__ = "submission"
    __table_args__ = (UniqueConstraint("assignment_id", "platform_submission_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assignment.id"))
    platform_submission_id: Mapped[str] = mapped_column(Text)
    student_identifier: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ai_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The diagram marks this NOT NULL, but it is NULL until grading
    # finishes and the AD-9 guard depends on that, so it is nullable here.
    ai_graded_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active_attempt: Mapped[bool] = mapped_column(server_default=true())


class SubmissionTarget(Base):
    __tablename__ = "submission_target"
    __table_args__ = (
        UniqueConstraint("submission_id", "grading_target_id"),
        # The nightly queue: ai_needed AND completed_at IS NULL (AD-8).
        Index(
            "ix_submission_target_pending",
            "submission_id",
            postgresql_where=text("ai_needed AND completed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submission.id"))
    grading_target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grading_target.id"), index=True
    )
    ai_needed: Mapped[bool] = mapped_column(server_default=true())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    agreement_score: Mapped[Decimal | None] = mapped_column(Numeric)
    score_spread: Mapped[Decimal | None] = mapped_column(Numeric)
    selection_reason: Mapped[str | None] = mapped_column(Text)


class TestResult(Base):
    __test__ = False  # Not a pytest test class, despite the name.
    __tablename__ = "test_result"
    __table_args__ = (one_of("status", TEST_STATUSES),)

    id: Mapped[uuid.UUID] = uuid_pk()
    submission_target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission_target.id"), index=True
    )
    status: Mapped[str] = mapped_column(Text)
    functional_test_feedback: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = created_now()


# ---------------------------------------------------------------------
# AI-generated feedback
# ---------------------------------------------------------------------
class AiFeedback(Base):
    __tablename__ = "ai_feedback"
    __table_args__ = (
        # One row per provider, and exactly one of them written to results.json.
        Index(
            "uq_ai_feedback_one_selected",
            "submission_target_id",
            unique=True,
            postgresql_where=text("is_selected"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    submission_target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission_target.id"), index=True
    )
    rubric_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rubric.id"), index=True)
    provider: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    suggested_score: Mapped[Decimal | None] = mapped_column(Numeric)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    is_selected: Mapped[bool] = mapped_column(server_default=false())
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    generated_at: Mapped[datetime] = created_now()


class AiFeedbackReview(Base):
    __tablename__ = "ai_feedback_review"

    id: Mapped[uuid.UUID] = uuid_pk()
    ai_feedback_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_feedback.id"), index=True)
    reviewer: Mapped[str | None] = mapped_column(Text)
    instructor_rating: Mapped[int | None] = mapped_column(Integer)
    instructor_comment: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime] = created_now()


# ---------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------
class TriggerRun(Base):
    __tablename__ = "trigger_run"
    __table_args__ = (one_of("trigger", TRIGGERS),)

    id: Mapped[uuid.UUID] = uuid_pk()
    trigger: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = created_now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    submissions_authorized: Mapped[int | None] = mapped_column(Integer)
    submissions_rerun: Mapped[int | None] = mapped_column(Integer)
    submissions_failed_to_trigger: Mapped[int | None] = mapped_column(Integer)
    log_uri: Mapped[str | None] = mapped_column(Text)


class TriggerRunSubmission(Base):
    __tablename__ = "trigger_run_submission"
    __table_args__ = (
        UniqueConstraint("trigger_run_id", "submission_id"),
        one_of("status", ("AUTHORIZED", "RERUN_SUCCEEDED", "RERUN_FAILED")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    trigger_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trigger_run.id"), index=True)
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submission.id"), index=True)
    status: Mapped[str] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = created_now()

"""The repository interface and the types that cross it.

See IGradingRepository in docs/architecture/07-interfaces.puml. Only the
submission-run methods exist so far; the rest are added with their
InMemoryRepository counterparts, as note N4 requires.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Action(str, Enum):
    GRADE = "GRADE"
    DEFER = "DEFER"
    REEMIT = "REEMIT"


@dataclass(frozen=True)
class SubmissionRef:
    """What the container knows about a submission when it first reports in.

    The assignment comes from the caller's per-assignment token, not from
    the container, so it is resolved before this is built.
    """

    assignment_id: uuid.UUID
    platform_submission_id: str
    submitted_at: datetime
    student_identifier: str | None = None


@dataclass(frozen=True)
class SubmissionTargetRef:
    id: uuid.UUID
    identifier: str
    ai_needed: bool
    completed_at: datetime | None


@dataclass(frozen=True)
class ActionResult:
    action: Action
    # Targets still wanting feedback. Filled only for GRADE.
    targets: tuple[SubmissionTargetRef, ...] = field(default=())


class RepositoryError(Exception):
    pass


class NotFoundError(RepositoryError):
    pass


class UnknownTargetError(RepositoryError):
    """A target name not registered on the assignment (AD-3).

    Carries the registered names so the API's 4xx can list them.
    """

    def __init__(self, target: str, registered: list[str]):
        listed = ", ".join(registered) if registered else "(none)"
        super().__init__(f"Unknown target {target!r}. Registered targets: {listed}")
        self.target = target
        self.registered = registered


def derive_action(
    grading_mode: str,
    submitted_at: datetime,
    ai_authorized_at: datetime | None,
    ai_graded_submitted_at: datetime | None,
) -> Action:
    """The AD-11 table. Kept pure so both repositories share one rule."""
    # AD-9 guard first: a graded submission is never graded again.
    if ai_graded_submitted_at is not None and ai_graded_submitted_at == submitted_at:
        return Action.REEMIT
    if grading_mode == "IMMEDIATE":
        return Action.GRADE
    if ai_authorized_at is None:
        return Action.DEFER
    return Action.GRADE


def sort_key(display_order: int | None, identifier: str) -> tuple[bool, int, str]:
    """Targets in display order, unordered ones last, then by name."""
    return (display_order is None, display_order or 0, identifier)


class IGradingRepository(ABC):
    @abstractmethod
    def upsert_submission(self, ref: SubmissionRef) -> uuid.UUID:
        """Return the submission's id, creating it on first report.

        A rerun reports the same platform_submission_id and gets the same
        row back unchanged.
        """

    @abstractmethod
    def create_targets_default_needed(self, submission_id: uuid.UUID) -> list[SubmissionTargetRef]:
        """One submission_target per registered target, ai_needed TRUE (AD-7).

        Idempotent: rows that already exist keep their state, so a rerun
        does not undo an opt-out or reopen a completed target.
        """

    @abstractmethod
    def set_ai_not_needed(self, submission_id: uuid.UUID, target: str) -> bool:
        """Opt a target out. Returns whether it was previously needed.

        Raises UnknownTargetError for a name not registered (AD-3).
        """

    @abstractmethod
    def add_test_result(
        self,
        submission_id: uuid.UUID,
        target: str,
        status: str,
        functional_test_feedback: str | None,
    ) -> None:
        """Record a test outcome against a target (AD-10).

        Takes the target name rather than a submission_target id, unlike
        the diagram, so the AD-3 check lives here for both calls.
        """

    @abstractmethod
    def decide_action(self, submission_id: uuid.UUID) -> ActionResult:
        """GRADE, DEFER or REEMIT for this run (AD-11)."""

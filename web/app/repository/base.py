"""The repository interface and the types that cross it.

See IGradingRepository in docs/architecture/07-interfaces.puml. Adding an
abstract method means adding it to InMemoryRepository in the same commit
(note N4).

Methods that act on one target take the submission id and the target's
identifier, rather than a submission_target id as the diagram has it, so
the AD-3 unregistered-name check covers every one of them.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from app.feedback_config import TargetSettings, resolve_target_analysis
from app.models import GRADING_MODES, TARGET_KINDS


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


@dataclass(frozen=True)
class TestResultRecord:
    __test__ = False  # Not a pytest test class, despite the name.

    status: str
    functional_test_feedback: str | None
    recorded_at: datetime


@dataclass(frozen=True)
class ProviderFeedback:
    """One provider's response. Mirrors ai_grading's FeedbackResponse."""

    provider: str
    model_name: str
    body: str
    prompt_version: str | None = None
    suggested_score: Decimal | float | None = None
    confidence: Decimal | float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class FeedbackResult:
    """Every provider's response for one target, and which one was chosen.

    Mirrors ai_grading's GradingResult. All providers were prompted with
    the same rubric, so it is recorded once.
    """

    rubric_id: uuid.UUID
    responses: tuple[ProviderFeedback, ...]
    selected_index: int
    selection_reason: str | None = None
    score_spread: Decimal | float | None = None
    agreement_score: Decimal | float | None = None

    def __post_init__(self):
        if not self.responses:
            raise ValueError("a feedback result needs at least one response")
        if not 0 <= self.selected_index < len(self.responses):
            raise ValueError("selected_index must point at one of the responses")


@dataclass(frozen=True)
class RubricConfig:
    id: uuid.UUID
    version: int
    criteria_text: str | None
    feedback_guidance: str | None


@dataclass(frozen=True)
class TargetConfig:
    id: uuid.UUID
    identifier: str
    kind: str
    max_score: Decimal | None
    # The current rubric, or None if the instructor has not written one.
    rubric: RubricConfig | None
    analysis_config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model_name: str
    is_primary: bool
    engine: str = "vertex"


@dataclass(frozen=True)
class AssignmentConfig:
    """What the AI phase needs to grade any submission of an assignment."""

    id: uuid.UUID
    platform: str
    platform_assignment_id: str
    name: str | None
    grading_mode: str
    language: str
    is_active: bool
    targets: tuple[TargetConfig, ...]
    # Enabled providers, the primary first. llm_provider_config is global
    # for now, as drawn, so this is the same for every assignment.
    providers: tuple[ProviderConfig, ...]


@dataclass(frozen=True)
class SelectedFeedback:
    provider: str
    model_name: str
    body: str | None
    suggested_score: Decimal | None


@dataclass(frozen=True)
class TargetFeedback:
    """One ai_needed target's outcome. At most one of feedback and error is set;
    neither means the target has not been attempted yet."""

    identifier: str
    max_score: Decimal | None
    feedback: SelectedFeedback | None
    error: str | None


@dataclass(frozen=True)
class FeedbackPackage:
    """What REEMIT splices back into results.json."""

    submission_id: uuid.UUID
    targets: tuple[TargetFeedback, ...]


@dataclass(frozen=True)
class SubmissionLocator:
    """Enough for the nightly trigger to find the submission on the platform."""

    submission_id: uuid.UUID
    platform: str
    platform_assignment_id: str
    platform_submission_id: str
    student_identifier: str | None


class TargetStatus(str, Enum):
    """Where one submission target stands (06-submission-target-state)."""

    OPTED_OUT = "OPTED_OUT"
    WAITING = "WAITING"
    FAILED = "FAILED"
    DONE = "DONE"


@dataclass(frozen=True)
class AssignmentSummary:
    """One row of the instructor's assignment list."""

    id: uuid.UUID
    platform: str
    platform_assignment_id: str
    name: str | None
    grading_mode: str
    language: str
    is_active: bool
    target_count: int
    submission_count: int


@dataclass(frozen=True)
class SubmissionSummary:
    """One row of an assignment's submission list, with its targets counted by status."""

    id: uuid.UUID
    platform_submission_id: str
    student_identifier: str | None
    submitted_at: datetime
    is_active_attempt: bool
    action: Action
    done: int
    failed: int
    waiting: int
    opted_out: int


@dataclass(frozen=True)
class FeedbackDetail:
    """One provider's response, as the instructor reviews it."""

    id: uuid.UUID
    provider: str
    model_name: str
    prompt_version: str | None
    body: str | None
    suggested_score: Decimal | None
    confidence: Decimal | None
    is_selected: bool
    rubric_version: int
    input_tokens: int | None
    output_tokens: int | None
    generated_at: datetime


@dataclass(frozen=True)
class TargetDetail:
    identifier: str
    kind: str
    max_score: Decimal | None
    status: TargetStatus
    completed_at: datetime | None
    last_error: str | None
    selection_reason: str | None
    score_spread: Decimal | None
    agreement_score: Decimal | None
    # Oldest first.
    test_results: tuple[TestResultRecord, ...]
    # The selected response first, then by provider.
    feedback: tuple[FeedbackDetail, ...]


@dataclass(frozen=True)
class SubmissionDetail:
    """Everything the instructor's submission page shows (05-sequence-instructor)."""

    id: uuid.UUID
    assignment_id: uuid.UUID
    assignment_name: str | None
    grading_mode: str
    platform_submission_id: str
    student_identifier: str | None
    submitted_at: datetime
    ai_authorized_at: datetime | None
    ai_graded_submitted_at: datetime | None
    is_active_attempt: bool
    action: Action
    # Display order.
    targets: tuple[TargetDetail, ...]


@dataclass(frozen=True)
class TargetSummary:
    """One row of an assignment's by-target view.

    Counts only active attempts the autograder recorded a test result for.
    """

    id: uuid.UUID
    identifier: str
    kind: str
    max_score: Decimal | None
    done: int
    failed: int
    waiting: int
    opted_out: int

    @property
    def submission_count(self) -> int:
        return self.done + self.failed + self.waiting + self.opted_out


@dataclass(frozen=True)
class TargetSubmission:
    """One student's active attempt at a target."""

    submission_id: uuid.UUID
    platform_submission_id: str
    student_identifier: str | None
    submitted_at: datetime
    status: TargetStatus
    # The most recently recorded one.
    latest_test: TestResultRecord
    # The selected provider's, once feedback is given.
    suggested_score: Decimal | None


@dataclass(frozen=True)
class TargetSubmissions:
    """Everything the instructor's target page shows."""

    assignment_id: uuid.UUID
    assignment_name: str | None
    grading_mode: str
    id: uuid.UUID
    identifier: str
    kind: str
    max_score: Decimal | None
    # By student, unknown students last.
    submissions: tuple[TargetSubmission, ...]


class RepositoryError(Exception):
    pass


class NotFoundError(RepositoryError):
    pass


class ConflictError(RepositoryError):
    """The request is valid but the row is not in a state that allows it."""


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


def to_decimal(value: Decimal | float | None) -> Decimal | None:
    # Through str, so 0.1 is stored as 0.1 and not its binary expansion.
    return None if value is None else Decimal(str(value))


def check_gradable(identifier: str, ai_needed: bool, completed_at: datetime | None) -> None:
    """Only a target still wanting feedback can be completed (06-state)."""
    if not ai_needed:
        raise ConflictError(f"Target {identifier!r} was opted out of AI feedback")
    if completed_at is not None:
        raise ConflictError(f"Target {identifier!r} is already completed")


def sort_key(display_order: int | None, identifier: str) -> tuple[bool, int, str]:
    """Targets in display order, unordered ones last, then by name."""
    return (display_order is None, display_order or 0, identifier)


def target_status(ai_needed: bool, completed_at: datetime | None, last_error: str | None) -> TargetStatus:
    if not ai_needed:
        return TargetStatus.OPTED_OUT
    if completed_at is None:
        return TargetStatus.WAITING
    return TargetStatus.FAILED if last_error is not None else TargetStatus.DONE


def assignment_sort_key(a: AssignmentSummary) -> tuple:
    """Active assignments first, then by name, unnamed ones last.

    Sorted in Python by both repositories, so Postgres collation cannot
    make them disagree.
    """
    return (not a.is_active, a.name is None, a.name or "", a.platform_assignment_id)


def summarize(
    sub_id: uuid.UUID,
    platform_submission_id: str,
    student_identifier: str | None,
    submitted_at: datetime,
    is_active_attempt: bool,
    action: Action,
    statuses: list[TargetStatus],
) -> SubmissionSummary:
    return SubmissionSummary(
        sub_id,
        platform_submission_id,
        student_identifier,
        submitted_at,
        is_active_attempt,
        action,
        statuses.count(TargetStatus.DONE),
        statuses.count(TargetStatus.FAILED),
        statuses.count(TargetStatus.WAITING),
        statuses.count(TargetStatus.OPTED_OUT),
    )


def summarize_target(
    target_id: uuid.UUID,
    identifier: str,
    kind: str,
    max_score: Decimal | None,
    statuses: list[TargetStatus],
) -> TargetSummary:
    return TargetSummary(
        target_id,
        identifier,
        kind,
        max_score,
        statuses.count(TargetStatus.DONE),
        statuses.count(TargetStatus.FAILED),
        statuses.count(TargetStatus.WAITING),
        statuses.count(TargetStatus.OPTED_OUT),
    )


def student_sort_key(s: TargetSubmission) -> tuple:
    """By student, unknown students last, then by platform id so ties are stable."""
    return (s.student_identifier is None, s.student_identifier or "", s.platform_submission_id)


def feedback_sort_key(f: FeedbackDetail) -> tuple:
    return (not f.is_selected, f.provider, f.model_name)


def clean(value: str | None) -> str | None:
    """Strip a form value, treating blank as absent."""
    if value is None:
        return None
    return value.strip() or None


def required(value: str | None, label: str) -> str:
    cleaned = clean(value)
    if cleaned is None:
        raise ValueError(f"{label} is required")
    return cleaned


def check_assignment_fields(grading_mode: str, language: str) -> tuple[str, str]:
    if grading_mode not in GRADING_MODES:
        raise ValueError(f"grading mode must be one of {', '.join(GRADING_MODES)}, not {grading_mode!r}")
    return grading_mode, required(language, "Language").upper()


def check_target_fields(identifier: str, kind: str, max_score: Decimal | None) -> str:
    if kind not in TARGET_KINDS:
        raise ValueError(f"kind must be one of {', '.join(TARGET_KINDS)}, not {kind!r}")
    if max_score is not None and max_score < 0:
        raise ValueError("max score cannot be negative")
    return required(identifier, "Identifier")


def check_target_analysis(identifier: str, kind: str, language: str, analysis: dict,
                          max_score: Decimal | None) -> dict:
    settings = TargetSettings(
        identifier=identifier, kind=kind, max_score=max_score,
        analysis=resolve_target_analysis(identifier, kind, language, analysis),
    )
    return asdict(settings.analysis)


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

    @abstractmethod
    def authorize_for_grading(self, submission_id: uuid.UUID) -> None:
        """Set ai_authorized_at, so the next run's action is GRADE (AD-11).

        The nightly trigger calls this BEFORE clicking rerun. Calling it
        again refreshes the timestamp, which is harmless.
        """

    @abstractmethod
    def get_test_results(self, submission_id: uuid.UUID, target: str) -> list[TestResultRecord]:
        """A target's test results in recorded_at order, for the prompt (AD-10)."""

    @abstractmethod
    def save_feedback(self, submission_id: uuid.UUID, target: str, result: FeedbackResult) -> None:
        """Store every provider's feedback and complete the target.

        One ai_feedback row per response, the selected one marked. Raises
        ConflictError if the target was opted out or is already completed,
        and ValueError if the rubric is not this target's.
        """

    @abstractmethod
    def mark_target_failed(self, submission_id: uuid.UUID, target: str, error: str) -> None:
        """Complete the target with last_error set (AD-8).

        completed_at is set here too, or the nightly trigger would pick the
        row up again every night forever.
        """

    @abstractmethod
    def mark_ai_complete_if_all_done(self, submission_id: uuid.UUID) -> bool:
        """Set the AD-9 guard if every ai_needed target is completed.

        Returns whether the guard is set after the call, so a repeat call
        on a finished submission returns True. A submission whose targets
        were all opted out is done.
        """

    @abstractmethod
    def get_assignment_config(self, assignment_id: uuid.UUID) -> AssignmentConfig:
        """Targets in display order with their current rubric, and providers."""

    @abstractmethod
    def get_selected_feedback(self, submission_id: uuid.UUID) -> FeedbackPackage:
        """Stored outcome of every ai_needed target, for REEMIT.

        Opted-out targets are left out: nothing was promised for them.
        """

    @abstractmethod
    def list_submissions_pending_ai(self, mode: str) -> list[SubmissionLocator]:
        """Submissions the nightly trigger should authorize and rerun.

        The 04-sequence-nightly query: the assignment is in this mode, a
        target is still ai_needed with completed_at NULL, and the guard is
        unset. Also restricted to active attempts (AD-14) on active
        assignments. Oldest submission first.
        """

    # Instructor UI reads (05-sequence-instructor). Read-only.

    @abstractmethod
    def list_assignments(self) -> list[AssignmentSummary]:
        """Every assignment with its target and submission counts.

        Active first, then by name.
        """

    @abstractmethod
    def list_submissions(self, assignment_id: uuid.UUID) -> list[SubmissionSummary]:
        """An assignment's submissions, newest first, including inactive attempts."""

    @abstractmethod
    def get_submission_detail(self, submission_id: uuid.UUID) -> SubmissionDetail:
        """A submission's targets with their test results and every provider's feedback."""

    @abstractmethod
    def list_target_summaries(self, assignment_id: uuid.UUID) -> list[TargetSummary]:
        """Every target of an assignment in display order, with its submissions counted by status.

        Counts each student's active attempt (AD-14) once, and only where the
        autograder recorded a test result for the target.
        """

    @abstractmethod
    def get_target_submissions(self, assignment_id: uuid.UUID, target_id: uuid.UUID) -> TargetSubmissions:
        """A target's active attempts that have a test result, by student.

        Raises NotFoundError if the target is not registered on this
        assignment, so it cannot be read under another assignment's URL.
        """

    # Instructor configuration writes (05-sequence-instructor).

    @abstractmethod
    def create_assignment(
        self,
        *,
        course_name: str,
        term: str | None,
        platform_assignment_id: str,
        name: str | None,
        grading_mode: str,
        language: str,
    ) -> uuid.UUID:
        """Create an assignment, in the course with this name and term.

        The course is created if it does not exist yet. Raises ValueError
        for a blank required field or an unknown grading mode, and
        ConflictError if the platform assignment id is already registered.
        """

    @abstractmethod
    def create_target(
        self,
        assignment_id: uuid.UUID,
        *,
        identifier: str,
        kind: str,
        max_score: Decimal | None,
        analysis_config: dict | None = None,
    ) -> uuid.UUID:
        """Register a target, after the assignment's existing ones in display order.

        Raises ConflictError if the identifier is already registered on the
        assignment (AD-3), and ValueError for a blank identifier, an
        unknown kind, or a negative max score.
        """

    @abstractmethod
    def delete_target(self, assignment_id: uuid.UUID, target_id: uuid.UUID) -> None:
        """Remove a target and its rubric versions.

        Only a target no submission has used yet: ConflictError once any
        submission_target points at it, since its results and feedback
        would go with it. NotFoundError if the target is not registered on
        this assignment.
        """

    @abstractmethod
    def upsert_rubric(
        self,
        grading_target_id: uuid.UUID,
        *,
        criteria_text: str,
        feedback_guidance: str | None,
        analysis_config: dict | None = None,
        max_score: Decimal | None = None,
    ) -> RubricConfig:
        """Make this the target's current rubric, as version n+1.

        Rubrics are versioned, not overwritten, so feedback keeps pointing
        at the rubric it was generated from. Saving text identical to the
        current rubric returns it unchanged rather than adding a version.
        When analysis_config is supplied, update it and max_score atomically
        with the rubric; otherwise preserve the target configuration.
        Raises ValueError for blank criteria.
        """


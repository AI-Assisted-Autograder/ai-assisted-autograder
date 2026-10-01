"""In-memory IGradingRepository, so the API and its callers test offline.

Not thread-safe, and holds only the columns the implemented methods use.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.models import TEST_STATUSES
from app.repository.base import (
    Action,
    ActionResult,
    FeedbackResult,
    IGradingRepository,
    NotFoundError,
    SubmissionRef,
    SubmissionTargetRef,
    TestResultRecord,
    UnknownTargetError,
    check_gradable,
    derive_action,
    sort_key,
    to_decimal,
)


@dataclass
class _Assignment:
    id: uuid.UUID
    grading_mode: str


@dataclass
class _GradingTarget:
    id: uuid.UUID
    assignment_id: uuid.UUID
    identifier: str
    display_order: int | None


@dataclass
class _Rubric:
    id: uuid.UUID
    grading_target_id: uuid.UUID


@dataclass
class _Submission:
    id: uuid.UUID
    assignment_id: uuid.UUID
    platform_submission_id: str
    submitted_at: datetime
    student_identifier: str | None
    ai_authorized_at: datetime | None = None
    ai_graded_submitted_at: datetime | None = None


@dataclass
class _SubmissionTarget:
    id: uuid.UUID
    submission_id: uuid.UUID
    grading_target_id: uuid.UUID
    ai_needed: bool = True
    completed_at: datetime | None = None
    last_error: str | None = None
    agreement_score: Decimal | None = None
    score_spread: Decimal | None = None
    selection_reason: str | None = None


@dataclass
class _TestResult:
    submission_target_id: uuid.UUID
    status: str
    functional_test_feedback: str | None
    recorded_at: datetime


@dataclass
class _AiFeedback:
    submission_target_id: uuid.UUID
    rubric_id: uuid.UUID
    provider: str
    model_name: str
    prompt_version: str | None
    body: str
    suggested_score: Decimal | None
    confidence: Decimal | None
    is_selected: bool
    input_tokens: int | None
    output_tokens: int | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryRepository(IGradingRepository):
    def __init__(self):
        self.assignments: dict[uuid.UUID, _Assignment] = {}
        self.grading_targets: dict[uuid.UUID, _GradingTarget] = {}
        self.rubrics: dict[uuid.UUID, _Rubric] = {}
        self.submissions: dict[uuid.UUID, _Submission] = {}
        self.submission_targets: dict[uuid.UUID, _SubmissionTarget] = {}
        self.test_results: list[_TestResult] = []
        self.ai_feedback: list[_AiFeedback] = []

    # Seeding, standing in for the instructor's web UI.
    def add_assignment(self, grading_mode: str) -> uuid.UUID:
        a = _Assignment(uuid.uuid4(), grading_mode)
        self.assignments[a.id] = a
        return a.id

    def add_grading_target(
        self, assignment_id: uuid.UUID, identifier: str, display_order: int | None = None
    ) -> uuid.UUID:
        gt = _GradingTarget(uuid.uuid4(), assignment_id, identifier, display_order)
        self.grading_targets[gt.id] = gt
        return gt.id

    def add_rubric(self, grading_target_id: uuid.UUID) -> uuid.UUID:
        r = _Rubric(uuid.uuid4(), grading_target_id)
        self.rubrics[r.id] = r
        return r.id

    def upsert_submission(self, ref: SubmissionRef) -> uuid.UUID:
        if ref.assignment_id not in self.assignments:
            raise NotFoundError(f"Assignment {ref.assignment_id} not found")
        for sub in self.submissions.values():
            if (sub.assignment_id, sub.platform_submission_id) == (
                ref.assignment_id,
                ref.platform_submission_id,
            ):
                return sub.id
        sub = _Submission(
            uuid.uuid4(),
            ref.assignment_id,
            ref.platform_submission_id,
            ref.submitted_at,
            ref.student_identifier,
        )
        self.submissions[sub.id] = sub
        return sub.id

    def create_targets_default_needed(self, submission_id: uuid.UUID) -> list[SubmissionTargetRef]:
        sub = self._submission(submission_id)
        for gt in self._registered(sub.assignment_id):
            self._submission_target(submission_id, gt)
        return self._refs(submission_id, lambda st: True)

    def set_ai_not_needed(self, submission_id: uuid.UUID, target: str) -> bool:
        st = self._target(submission_id, target)
        was_needed = st.ai_needed
        st.ai_needed = False
        return was_needed

    def add_test_result(
        self,
        submission_id: uuid.UUID,
        target: str,
        status: str,
        functional_test_feedback: str | None,
    ) -> None:
        if status not in TEST_STATUSES:
            raise ValueError(f"status must be one of {', '.join(TEST_STATUSES)}, not {status!r}")
        st = self._target(submission_id, target)
        self.test_results.append(
            _TestResult(st.id, status, functional_test_feedback, _now())
        )

    def decide_action(self, submission_id: uuid.UUID) -> ActionResult:
        sub = self._submission(submission_id)
        mode = self.assignments[sub.assignment_id].grading_mode
        action = derive_action(mode, sub.submitted_at, sub.ai_authorized_at, sub.ai_graded_submitted_at)
        if action is not Action.GRADE:
            return ActionResult(action)
        pending = self._refs(submission_id, lambda st: st.ai_needed and st.completed_at is None)
        return ActionResult(action, tuple(pending))

    def authorize_for_grading(self, submission_id: uuid.UUID) -> None:
        self._submission(submission_id).ai_authorized_at = _now()

    def get_test_results(self, submission_id: uuid.UUID, target: str) -> list[TestResultRecord]:
        st = self._target(submission_id, target)
        # Appended in recorded order already; sorted is stable on ties.
        rows = sorted(
            (r for r in self.test_results if r.submission_target_id == st.id),
            key=lambda r: r.recorded_at,
        )
        return [TestResultRecord(r.status, r.functional_test_feedback, r.recorded_at) for r in rows]

    def save_feedback(self, submission_id: uuid.UUID, target: str, result: FeedbackResult) -> None:
        st = self._target(submission_id, target)
        check_gradable(target, st.ai_needed, st.completed_at)
        rubric = self.rubrics.get(result.rubric_id)
        if rubric is None or rubric.grading_target_id != st.grading_target_id:
            raise ValueError(f"Rubric {result.rubric_id} is not a rubric for target {target!r}")
        for i, r in enumerate(result.responses):
            self.ai_feedback.append(
                _AiFeedback(
                    st.id,
                    result.rubric_id,
                    r.provider,
                    r.model_name,
                    r.prompt_version,
                    r.body,
                    to_decimal(r.suggested_score),
                    to_decimal(r.confidence),
                    i == result.selected_index,
                    r.input_tokens,
                    r.output_tokens,
                )
            )
        st.completed_at = _now()
        st.last_error = None
        st.selection_reason = result.selection_reason
        st.score_spread = to_decimal(result.score_spread)
        st.agreement_score = to_decimal(result.agreement_score)

    def mark_target_failed(self, submission_id: uuid.UUID, target: str, error: str) -> None:
        if not error or not error.strip():
            raise ValueError("error must be a non-empty message")
        st = self._target(submission_id, target)
        check_gradable(target, st.ai_needed, st.completed_at)
        st.completed_at = _now()
        st.last_error = error

    def mark_ai_complete_if_all_done(self, submission_id: uuid.UUID) -> bool:
        sub = self._submission(submission_id)
        pending = any(
            st.submission_id == submission_id and st.ai_needed and st.completed_at is None
            for st in self.submission_targets.values()
        )
        if not pending:
            sub.ai_graded_submitted_at = sub.submitted_at
        return sub.ai_graded_submitted_at is not None and sub.ai_graded_submitted_at == sub.submitted_at

    def _submission(self, submission_id: uuid.UUID) -> _Submission:
        sub = self.submissions.get(submission_id)
        if sub is None:
            raise NotFoundError(f"Submission {submission_id} not found")
        return sub

    def _registered(self, assignment_id: uuid.UUID) -> list[_GradingTarget]:
        return [gt for gt in self.grading_targets.values() if gt.assignment_id == assignment_id]

    def _submission_target(self, submission_id: uuid.UUID, gt: _GradingTarget) -> _SubmissionTarget:
        for st in self.submission_targets.values():
            if (st.submission_id, st.grading_target_id) == (submission_id, gt.id):
                return st
        st = _SubmissionTarget(uuid.uuid4(), submission_id, gt.id)
        self.submission_targets[st.id] = st
        return st

    def _target(self, submission_id: uuid.UUID, identifier: str) -> _SubmissionTarget:
        sub = self._submission(submission_id)
        registered = self._registered(sub.assignment_id)
        for gt in registered:
            if gt.identifier == identifier:
                return self._submission_target(submission_id, gt)
        raise UnknownTargetError(identifier, sorted(gt.identifier for gt in registered))

    def _refs(self, submission_id: uuid.UUID, keep) -> list[SubmissionTargetRef]:
        pairs = [
            (st, self.grading_targets[st.grading_target_id])
            for st in self.submission_targets.values()
            if st.submission_id == submission_id and keep(st)
        ]
        pairs.sort(key=lambda p: sort_key(p[1].display_order, p[1].identifier))
        return [SubmissionTargetRef(st.id, gt.identifier, st.ai_needed, st.completed_at) for st, gt in pairs]

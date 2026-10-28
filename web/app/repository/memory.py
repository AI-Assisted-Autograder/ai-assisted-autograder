"""In-memory IGradingRepository, so the API and its callers test offline.

Not thread-safe, and holds only the columns the implemented methods use.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from app.feedback_config import RubricInput
from app.models import GRADING_MODES, TEST_STATUSES
from app.repository.base import (
    Action,
    ActionResult,
    AssignmentConfig,
    AssignmentSummary,
    ConflictError,
    FeedbackDetail,
    FeedbackPackage,
    FeedbackResult,
    IGradingRepository,
    NotFoundError,
    ProviderConfig,
    RubricConfig,
    SelectedFeedback,
    SubmissionDetail,
    SubmissionLocator,
    SubmissionRef,
    SubmissionSummary,
    SubmissionTargetRef,
    TargetConfig,
    TargetDetail,
    TargetFeedback,
    TargetSubmission,
    TargetSubmissions,
    TargetSummary,
    TestResultRecord,
    UnknownTargetError,
    assignment_sort_key,
    check_assignment_fields,
    check_gradable,
    check_target_analysis,
    check_target_fields,
    clean,
    derive_action,
    feedback_sort_key,
    required,
    sort_key,
    student_sort_key,
    summarize,
    summarize_target,
    target_status,
    to_decimal,
)


@dataclass
class _Course:
    id: uuid.UUID
    name: str
    term: str | None


@dataclass
class _Assignment:
    id: uuid.UUID
    grading_mode: str
    platform: str
    platform_assignment_id: str
    language: str
    name: str | None
    is_active: bool = True
    course_id: uuid.UUID | None = None


@dataclass
class _GradingTarget:
    id: uuid.UUID
    assignment_id: uuid.UUID
    identifier: str
    display_order: int | None
    kind: str
    max_score: Decimal | None
    analysis_config: dict = field(default_factory=dict)


@dataclass
class _Rubric:
    id: uuid.UUID
    grading_target_id: uuid.UUID
    version: int
    criteria_text: str | None
    feedback_guidance: str | None
    is_current: bool


@dataclass
class _LlmProviderConfig:
    provider: str
    model_name: str
    is_enabled: bool
    is_primary: bool
    engine: str = "vertex"


@dataclass
class _Submission:
    id: uuid.UUID
    assignment_id: uuid.UUID
    platform_submission_id: str
    submitted_at: datetime
    student_identifier: str | None
    ai_authorized_at: datetime | None = None
    ai_graded_submitted_at: datetime | None = None
    is_active_attempt: bool = True


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
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    generated_at: datetime = field(default_factory=lambda: _now())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryRepository(IGradingRepository):
    def __init__(self):
        self.courses: dict[uuid.UUID, _Course] = {}
        self.assignments: dict[uuid.UUID, _Assignment] = {}
        self.grading_targets: dict[uuid.UUID, _GradingTarget] = {}
        self.rubrics: dict[uuid.UUID, _Rubric] = {}
        self.submissions: dict[uuid.UUID, _Submission] = {}
        self.submission_targets: dict[uuid.UUID, _SubmissionTarget] = {}
        self.test_results: list[_TestResult] = []
        self.ai_feedback: list[_AiFeedback] = []
        self.providers: list[_LlmProviderConfig] = []

    # Seeding, standing in for the instructor's web UI.
    def add_assignment(
        self,
        grading_mode: str,
        *,
        platform_assignment_id: str | None = None,
        language: str = "PYTHON",
        name: str | None = None,
        is_active: bool = True,
    ) -> uuid.UUID:
        a = _Assignment(
            uuid.uuid4(),
            grading_mode,
            "GRADESCOPE",
            platform_assignment_id or str(uuid.uuid4()),
            language,
            name,
            is_active,
        )
        self.assignments[a.id] = a
        return a.id

    def add_grading_target(
        self,
        assignment_id: uuid.UUID,
        identifier: str,
        display_order: int | None = None,
        *,
        kind: str = "METHOD",
        max_score: Decimal | None = None,
        analysis_config: dict | None = None,
    ) -> uuid.UUID:
        gt = _GradingTarget(uuid.uuid4(), assignment_id, identifier, display_order, kind, max_score,
                            dict(analysis_config or {}))
        self.grading_targets[gt.id] = gt
        return gt.id

    def add_rubric(
        self,
        grading_target_id: uuid.UUID,
        *,
        version: int = 1,
        criteria_text: str | None = "Correct and readable.",
        feedback_guidance: str | None = None,
        is_current: bool = True,
    ) -> uuid.UUID:
        # At most one current rubric per target, as the SQL partial index enforces.
        if is_current:
            for r in self.rubrics.values():
                if r.grading_target_id == grading_target_id:
                    r.is_current = False
        r = _Rubric(uuid.uuid4(), grading_target_id, version, criteria_text, feedback_guidance, is_current)
        self.rubrics[r.id] = r
        return r.id

    def add_provider(
        self, provider: str, model_name: str, *, is_enabled: bool = True,
        is_primary: bool = False, engine: str = "vertex",
    ) -> None:
        self.providers.append(_LlmProviderConfig(provider, model_name, is_enabled, is_primary, engine))

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

    def get_assignment_config(self, assignment_id: uuid.UUID) -> AssignmentConfig:
        a = self.assignments.get(assignment_id)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found")
        targets = sorted(self._registered(assignment_id), key=lambda gt: sort_key(gt.display_order, gt.identifier))
        providers = sorted(
            (p for p in self.providers if p.is_enabled),
            key=lambda p: (not p.is_primary, p.provider, p.model_name),
        )
        return AssignmentConfig(
            a.id,
            a.platform,
            a.platform_assignment_id,
            a.name,
            a.grading_mode,
            a.language,
            a.is_active,
            tuple(
                TargetConfig(gt.id, gt.identifier, gt.kind, gt.max_score, self._current_rubric(gt.id),
                             dict(gt.analysis_config))
                for gt in targets
            ),
            tuple(ProviderConfig(p.provider, p.model_name, p.is_primary, p.engine) for p in providers),
        )

    def get_selected_feedback(self, submission_id: uuid.UUID) -> FeedbackPackage:
        self._submission(submission_id)
        pairs = [
            (st, self.grading_targets[st.grading_target_id])
            for st in self.submission_targets.values()
            if st.submission_id == submission_id and st.ai_needed
        ]
        pairs.sort(key=lambda p: sort_key(p[1].display_order, p[1].identifier))
        targets = []
        for st, gt in pairs:
            f = next((f for f in self.ai_feedback if f.submission_target_id == st.id and f.is_selected), None)
            selected = None if f is None else SelectedFeedback(f.provider, f.model_name, f.body, f.suggested_score)
            targets.append(TargetFeedback(gt.identifier, gt.max_score, selected, st.last_error))
        return FeedbackPackage(submission_id, tuple(targets))

    def list_submissions_pending_ai(self, mode: str) -> list[SubmissionLocator]:
        if mode not in GRADING_MODES:
            raise ValueError(f"mode must be one of {', '.join(GRADING_MODES)}, not {mode!r}")
        found = []
        for sub in self.submissions.values():
            a = self.assignments[sub.assignment_id]
            pending = any(
                st.submission_id == sub.id and st.ai_needed and st.completed_at is None
                for st in self.submission_targets.values()
            )
            if (a.grading_mode == mode and a.is_active and sub.is_active_attempt
                    and sub.ai_graded_submitted_at is None and pending):
                found.append((sub, a))
        found.sort(key=lambda p: (p[0].submitted_at, p[0].id))
        return [
            SubmissionLocator(
                sub.id, a.platform, a.platform_assignment_id, sub.platform_submission_id, sub.student_identifier
            )
            for sub, a in found
        ]

    def list_assignments(self) -> list[AssignmentSummary]:
        found = [
            AssignmentSummary(
                a.id,
                a.platform,
                a.platform_assignment_id,
                a.name,
                a.grading_mode,
                a.language,
                a.is_active,
                len(self._registered(a.id)),
                sum(1 for sub in self.submissions.values() if sub.assignment_id == a.id),
            )
            for a in self.assignments.values()
        ]
        return sorted(found, key=assignment_sort_key)

    def list_submissions(self, assignment_id: uuid.UUID) -> list[SubmissionSummary]:
        a = self.assignments.get(assignment_id)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found")
        subs = sorted(
            (sub for sub in self.submissions.values() if sub.assignment_id == assignment_id),
            key=lambda sub: (sub.submitted_at, sub.platform_submission_id),
            reverse=True,
        )
        return [
            summarize(
                sub.id,
                sub.platform_submission_id,
                sub.student_identifier,
                sub.submitted_at,
                sub.is_active_attempt,
                derive_action(a.grading_mode, sub.submitted_at, sub.ai_authorized_at, sub.ai_graded_submitted_at),
                [
                    target_status(st.ai_needed, st.completed_at, st.last_error)
                    for st in self.submission_targets.values()
                    if st.submission_id == sub.id
                ],
            )
            for sub in subs
        ]

    def get_submission_detail(self, submission_id: uuid.UUID) -> SubmissionDetail:
        sub = self._submission(submission_id)
        a = self.assignments[sub.assignment_id]
        pairs = [
            (st, self.grading_targets[st.grading_target_id])
            for st in self.submission_targets.values()
            if st.submission_id == submission_id
        ]
        pairs.sort(key=lambda p: sort_key(p[1].display_order, p[1].identifier))
        targets = []
        for st, gt in pairs:
            tests = sorted(
                (r for r in self.test_results if r.submission_target_id == st.id),
                key=lambda r: r.recorded_at,
            )
            feedback = sorted(
                (
                    FeedbackDetail(
                        f.id,
                        f.provider,
                        f.model_name,
                        f.prompt_version,
                        f.body,
                        f.suggested_score,
                        f.confidence,
                        f.is_selected,
                        self.rubrics[f.rubric_id].version,
                        f.input_tokens,
                        f.output_tokens,
                        f.generated_at,
                    )
                    for f in self.ai_feedback
                    if f.submission_target_id == st.id
                ),
                key=feedback_sort_key,
            )
            targets.append(
                TargetDetail(
                    gt.identifier,
                    gt.kind,
                    gt.max_score,
                    target_status(st.ai_needed, st.completed_at, st.last_error),
                    st.completed_at,
                    st.last_error,
                    st.selection_reason,
                    st.score_spread,
                    st.agreement_score,
                    tuple(TestResultRecord(r.status, r.functional_test_feedback, r.recorded_at) for r in tests),
                    tuple(feedback),
                )
            )
        return SubmissionDetail(
            sub.id,
            a.id,
            a.name,
            a.grading_mode,
            sub.platform_submission_id,
            sub.student_identifier,
            sub.submitted_at,
            sub.ai_authorized_at,
            sub.ai_graded_submitted_at,
            sub.is_active_attempt,
            derive_action(a.grading_mode, sub.submitted_at, sub.ai_authorized_at, sub.ai_graded_submitted_at),
            tuple(targets),
        )

    def _latest_test(self, submission_target_id: uuid.UUID) -> TestResultRecord | None:
        r = max(
            (r for r in self.test_results if r.submission_target_id == submission_target_id),
            key=lambda r: r.recorded_at,
            default=None,
        )
        return None if r is None else TestResultRecord(r.status, r.functional_test_feedback, r.recorded_at)

    def _active_tested(self, gt: _GradingTarget) -> list[tuple[_SubmissionTarget, _Submission, TestResultRecord]]:
        found = []
        for st in self.submission_targets.values():
            sub = self.submissions[st.submission_id]
            latest = self._latest_test(st.id)
            if st.grading_target_id == gt.id and sub.is_active_attempt and latest is not None:
                found.append((st, sub, latest))
        return found

    def list_target_summaries(self, assignment_id: uuid.UUID) -> list[TargetSummary]:
        if assignment_id not in self.assignments:
            raise NotFoundError(f"Assignment {assignment_id} not found")
        return [
            summarize_target(
                gt.id,
                gt.identifier,
                gt.kind,
                gt.max_score,
                [target_status(st.ai_needed, st.completed_at, st.last_error) for st, _, _ in self._active_tested(gt)],
            )
            for gt in sorted(self._registered(assignment_id), key=lambda gt: sort_key(gt.display_order, gt.identifier))
        ]

    def get_target_submissions(self, assignment_id: uuid.UUID, target_id: uuid.UUID) -> TargetSubmissions:
        a = self.assignments.get(assignment_id)
        gt = self.grading_targets.get(target_id)
        if a is None or gt is None or gt.assignment_id != a.id:
            raise NotFoundError(f"Target {target_id} not found on assignment {assignment_id}")
        submissions = [
            TargetSubmission(
                sub.id,
                sub.platform_submission_id,
                sub.student_identifier,
                sub.submitted_at,
                target_status(st.ai_needed, st.completed_at, st.last_error),
                latest,
                next(
                    (f.suggested_score for f in self.ai_feedback if f.submission_target_id == st.id and f.is_selected),
                    None,
                ),
            )
            for st, sub, latest in self._active_tested(gt)
        ]
        return TargetSubmissions(
            a.id,
            a.name,
            a.grading_mode,
            gt.id,
            gt.identifier,
            gt.kind,
            gt.max_score,
            tuple(sorted(submissions, key=student_sort_key)),
        )

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
        course_name = required(course_name, "Course")
        term = clean(term)
        platform_assignment_id = required(platform_assignment_id, "Gradescope assignment id")
        grading_mode, language = check_assignment_fields(grading_mode, language)
        if any(a.platform_assignment_id == platform_assignment_id for a in self.assignments.values()):
            raise ConflictError(f"Gradescope assignment {platform_assignment_id!r} is already registered")
        course = next((c for c in self.courses.values() if (c.name, c.term) == (course_name, term)), None)
        if course is None:
            course = _Course(uuid.uuid4(), course_name, term)
            self.courses[course.id] = course
        a = _Assignment(
            uuid.uuid4(), grading_mode, "GRADESCOPE", platform_assignment_id, language, clean(name),
            course_id=course.id,
        )
        self.assignments[a.id] = a
        return a.id

    def create_target(
        self,
        assignment_id: uuid.UUID,
        *,
        identifier: str,
        kind: str,
        max_score: Decimal | None,
        analysis_config: dict | None = None,
    ) -> uuid.UUID:
        identifier = check_target_fields(identifier, kind, max_score)
        if assignment_id not in self.assignments:
            raise NotFoundError(f"Assignment {assignment_id} not found")
        if analysis_config:
            analysis_config = check_target_analysis(
                identifier, kind, self.assignments[assignment_id].language, analysis_config, max_score,
            )
        registered = self._registered(assignment_id)
        if any(gt.identifier == identifier for gt in registered):
            raise ConflictError(f"Target {identifier!r} is already registered on this assignment")
        last = max((gt.display_order or 0 for gt in registered), default=0)
        return self.add_grading_target(assignment_id, identifier, last + 1, kind=kind, max_score=max_score,
                                       analysis_config=analysis_config)

    def delete_target(self, assignment_id: uuid.UUID, target_id: uuid.UUID) -> None:
        gt = self.grading_targets.get(target_id)
        if gt is None or gt.assignment_id != assignment_id:
            raise NotFoundError(f"Grading target {target_id} not found")
        if any(st.grading_target_id == target_id for st in self.submission_targets.values()):
            raise ConflictError(f"Target {gt.identifier!r} already has submissions, so it cannot be deleted")
        self.rubrics = {rid: r for rid, r in self.rubrics.items() if r.grading_target_id != target_id}
        del self.grading_targets[target_id]

    def upsert_rubric(
        self,
        grading_target_id: uuid.UUID,
        *,
        criteria_text: str,
        feedback_guidance: str | None,
        analysis_config: dict | None = None,
        max_score: Decimal | None = None,
    ) -> RubricConfig:
        criteria_text = required(criteria_text, "Criteria")
        feedback_guidance = clean(feedback_guidance)
        RubricInput(criteria_text=criteria_text, feedback_guidance=feedback_guidance or "")
        if grading_target_id not in self.grading_targets:
            raise NotFoundError(f"Grading target {grading_target_id} not found")
        target = self.grading_targets[grading_target_id]
        if analysis_config is None:
            analysis_config, max_score = target.analysis_config, target.max_score
        check_target_fields(target.identifier, target.kind, max_score)
        target.analysis_config = check_target_analysis(
            target.identifier, target.kind, self.assignments[target.assignment_id].language,
            analysis_config, max_score,
        )
        target.max_score = max_score
        current = self._current_rubric(grading_target_id)
        if current is not None and (current.criteria_text, current.feedback_guidance) == (
            criteria_text,
            feedback_guidance,
        ):
            return current
        last = max((r.version for r in self.rubrics.values() if r.grading_target_id == grading_target_id), default=0)
        self.add_rubric(
            grading_target_id, version=last + 1, criteria_text=criteria_text, feedback_guidance=feedback_guidance
        )
        return self._current_rubric(grading_target_id)

    def _current_rubric(self, grading_target_id: uuid.UUID) -> RubricConfig | None:
        for r in self.rubrics.values():
            if r.grading_target_id == grading_target_id and r.is_current:
                return RubricConfig(r.id, r.version, r.criteria_text, r.feedback_guidance)
        return None

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

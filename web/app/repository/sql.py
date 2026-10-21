import uuid

from sqlalchemy import Uuid, and_, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    GRADING_MODES,
    TEST_STATUSES,
    AiFeedback,
    Assignment,
    GradingTarget,
    LlmProviderConfig,
    Rubric,
    Submission,
    SubmissionTarget,
    TestResult,
)
from app.repository.base import (
    Action,
    ActionResult,
    AssignmentConfig,
    AssignmentSummary,
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
    TestResultRecord,
    UnknownTargetError,
    assignment_sort_key,
    check_gradable,
    derive_action,
    feedback_sort_key,
    sort_key,
    summarize,
    target_status,
    to_decimal,
)


class SqlGradingRepository(IGradingRepository):
    """Each method is its own transaction."""

    def __init__(self, sessions: sessionmaker[Session]):
        self._sessions = sessions

    def upsert_submission(self, ref: SubmissionRef) -> uuid.UUID:
        with self._sessions.begin() as s:
            if s.get(Assignment, ref.assignment_id) is None:
                raise NotFoundError(f"Assignment {ref.assignment_id} not found")
            inserted = s.scalar(
                insert(Submission)
                .values(
                    assignment_id=ref.assignment_id,
                    platform_submission_id=ref.platform_submission_id,
                    submitted_at=ref.submitted_at,
                    student_identifier=ref.student_identifier,
                )
                .on_conflict_do_nothing(index_elements=["assignment_id", "platform_submission_id"])
                .returning(Submission.id)
            )
            if inserted is not None:
                return inserted
            return s.scalar(
                select(Submission.id).where(
                    Submission.assignment_id == ref.assignment_id,
                    Submission.platform_submission_id == ref.platform_submission_id,
                )
            )

    def create_targets_default_needed(self, submission_id: uuid.UUID) -> list[SubmissionTargetRef]:
        with self._sessions.begin() as s:
            sub = self._submission(s, submission_id)
            s.execute(
                insert(SubmissionTarget)
                .from_select(
                    # id explicitly: the model's Python-side uuid4 default
                    # runs once per statement, not once per selected row.
                    ["id", "submission_id", "grading_target_id"],
                    select(func.gen_random_uuid(), literal(submission_id, Uuid), GradingTarget.id).where(
                        GradingTarget.assignment_id == sub.assignment_id
                    ),
                )
                .on_conflict_do_nothing(index_elements=["submission_id", "grading_target_id"])
            )
            rows = s.execute(
                select(SubmissionTarget, GradingTarget)
                .join(GradingTarget, SubmissionTarget.grading_target_id == GradingTarget.id)
                .where(SubmissionTarget.submission_id == submission_id)
            ).all()
            rows.sort(key=lambda r: sort_key(r[1].display_order, r[1].identifier))
            return [_ref(st, gt) for st, gt in rows]

    def set_ai_not_needed(self, submission_id: uuid.UUID, target: str) -> bool:
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target)
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
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target)
            s.add(
                TestResult(
                    submission_target_id=st.id,
                    status=status,
                    functional_test_feedback=functional_test_feedback,
                )
            )

    def decide_action(self, submission_id: uuid.UUID) -> ActionResult:
        with self._sessions.begin() as s:
            sub = self._submission(s, submission_id)
            mode = s.scalar(select(Assignment.grading_mode).where(Assignment.id == sub.assignment_id))
            action = derive_action(mode, sub.submitted_at, sub.ai_authorized_at, sub.ai_graded_submitted_at)
            if action is not Action.GRADE:
                return ActionResult(action)
            rows = s.execute(
                select(SubmissionTarget, GradingTarget)
                .join(GradingTarget, SubmissionTarget.grading_target_id == GradingTarget.id)
                .where(
                    SubmissionTarget.submission_id == submission_id,
                    SubmissionTarget.ai_needed,
                    SubmissionTarget.completed_at.is_(None),
                )
            ).all()
            rows.sort(key=lambda r: sort_key(r[1].display_order, r[1].identifier))
            return ActionResult(action, tuple(_ref(st, gt) for st, gt in rows))

    def authorize_for_grading(self, submission_id: uuid.UUID) -> None:
        with self._sessions.begin() as s:
            self._submission(s, submission_id).ai_authorized_at = func.now()

    def get_test_results(self, submission_id: uuid.UUID, target: str) -> list[TestResultRecord]:
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target)
            rows = s.scalars(
                select(TestResult)
                .where(TestResult.submission_target_id == st.id)
                .order_by(TestResult.recorded_at)
            ).all()
            return [TestResultRecord(r.status, r.functional_test_feedback, r.recorded_at) for r in rows]

    def save_feedback(self, submission_id: uuid.UUID, target: str, result: FeedbackResult) -> None:
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target, lock=True)
            check_gradable(target, st.ai_needed, st.completed_at)
            rubric = s.get(Rubric, result.rubric_id)
            if rubric is None or rubric.grading_target_id != st.grading_target_id:
                raise ValueError(f"Rubric {result.rubric_id} is not a rubric for target {target!r}")
            for i, r in enumerate(result.responses):
                s.add(
                    AiFeedback(
                        submission_target_id=st.id,
                        rubric_id=result.rubric_id,
                        provider=r.provider,
                        model_name=r.model_name,
                        prompt_version=r.prompt_version,
                        body=r.body,
                        suggested_score=to_decimal(r.suggested_score),
                        confidence=to_decimal(r.confidence),
                        is_selected=i == result.selected_index,
                        input_tokens=r.input_tokens,
                        output_tokens=r.output_tokens,
                    )
                )
            st.completed_at = func.now()
            st.last_error = None
            st.selection_reason = result.selection_reason
            st.score_spread = to_decimal(result.score_spread)
            st.agreement_score = to_decimal(result.agreement_score)

    def mark_target_failed(self, submission_id: uuid.UUID, target: str, error: str) -> None:
        # A NULL last_error on a completed row means success, so it must say something.
        if not error or not error.strip():
            raise ValueError("error must be a non-empty message")
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target, lock=True)
            check_gradable(target, st.ai_needed, st.completed_at)
            st.completed_at = func.now()
            st.last_error = error

    def mark_ai_complete_if_all_done(self, submission_id: uuid.UUID) -> bool:
        with self._sessions.begin() as s:
            self._submission(s, submission_id)
            pending = exists().where(
                SubmissionTarget.submission_id == submission_id,
                SubmissionTarget.ai_needed,
                SubmissionTarget.completed_at.is_(None),
            )
            # One statement, so a target completing concurrently cannot slip
            # between the check and the write.
            s.execute(
                update(Submission)
                .where(Submission.id == submission_id, ~pending)
                .values(ai_graded_submitted_at=Submission.submitted_at)
            )
            guard, submitted = s.execute(
                select(Submission.ai_graded_submitted_at, Submission.submitted_at).where(
                    Submission.id == submission_id
                )
            ).one()
            return guard is not None and guard == submitted

    def get_assignment_config(self, assignment_id: uuid.UUID) -> AssignmentConfig:
        with self._sessions.begin() as s:
            a = s.get(Assignment, assignment_id)
            if a is None:
                raise NotFoundError(f"Assignment {assignment_id} not found")
            rows = s.execute(
                select(GradingTarget, Rubric)
                .outerjoin(Rubric, and_(Rubric.grading_target_id == GradingTarget.id, Rubric.is_current))
                .where(GradingTarget.assignment_id == assignment_id)
            ).all()
            rows.sort(key=lambda r: sort_key(r[0].display_order, r[0].identifier))
            targets = tuple(
                TargetConfig(
                    gt.id,
                    gt.identifier,
                    gt.kind,
                    gt.max_score,
                    None if r is None else RubricConfig(r.id, r.version, r.criteria_text, r.feedback_guidance),
                )
                for gt, r in rows
            )
            providers = s.scalars(
                select(LlmProviderConfig)
                .where(LlmProviderConfig.is_enabled)
                .order_by(LlmProviderConfig.is_primary.desc(), LlmProviderConfig.provider, LlmProviderConfig.model_name)
            ).all()
            return AssignmentConfig(
                a.id,
                a.platform,
                a.platform_assignment_id,
                a.name,
                a.grading_mode,
                a.language,
                a.is_active,
                targets,
                tuple(ProviderConfig(p.provider, p.model_name, p.is_primary, p.engine) for p in providers),
            )

    def get_selected_feedback(self, submission_id: uuid.UUID) -> FeedbackPackage:
        with self._sessions.begin() as s:
            self._submission(s, submission_id)
            rows = s.execute(
                select(SubmissionTarget, GradingTarget, AiFeedback)
                .join(GradingTarget, SubmissionTarget.grading_target_id == GradingTarget.id)
                .outerjoin(
                    AiFeedback,
                    and_(AiFeedback.submission_target_id == SubmissionTarget.id, AiFeedback.is_selected),
                )
                .where(SubmissionTarget.submission_id == submission_id, SubmissionTarget.ai_needed)
            ).all()
            rows.sort(key=lambda r: sort_key(r[1].display_order, r[1].identifier))
            return FeedbackPackage(
                submission_id,
                tuple(
                    TargetFeedback(
                        gt.identifier,
                        gt.max_score,
                        None if f is None else SelectedFeedback(f.provider, f.model_name, f.body, f.suggested_score),
                        st.last_error,
                    )
                    for st, gt, f in rows
                ),
            )

    def list_submissions_pending_ai(self, mode: str) -> list[SubmissionLocator]:
        if mode not in GRADING_MODES:
            raise ValueError(f"mode must be one of {', '.join(GRADING_MODES)}, not {mode!r}")
        with self._sessions.begin() as s:
            pending = exists().where(
                SubmissionTarget.submission_id == Submission.id,
                SubmissionTarget.ai_needed,
                SubmissionTarget.completed_at.is_(None),
            )
            rows = s.execute(
                select(Submission, Assignment)
                .join(Assignment, Submission.assignment_id == Assignment.id)
                .where(
                    Assignment.grading_mode == mode,
                    Assignment.is_active,
                    Submission.is_active_attempt,
                    Submission.ai_graded_submitted_at.is_(None),
                    pending,
                )
                .order_by(Submission.submitted_at, Submission.id)
            ).all()
            return [
                SubmissionLocator(
                    sub.id, a.platform, a.platform_assignment_id, sub.platform_submission_id, sub.student_identifier
                )
                for sub, a in rows
            ]

    def list_assignments(self) -> list[AssignmentSummary]:
        with self._sessions.begin() as s:
            target_count = (
                select(func.count())
                .where(GradingTarget.assignment_id == Assignment.id)
                .scalar_subquery()
            )
            submission_count = (
                select(func.count())
                .where(Submission.assignment_id == Assignment.id)
                .scalar_subquery()
            )
            rows = s.execute(select(Assignment, target_count, submission_count)).all()
            found = [
                AssignmentSummary(
                    a.id,
                    a.platform,
                    a.platform_assignment_id,
                    a.name,
                    a.grading_mode,
                    a.language,
                    a.is_active,
                    targets,
                    submissions,
                )
                for a, targets, submissions in rows
            ]
            return sorted(found, key=assignment_sort_key)

    def list_submissions(self, assignment_id: uuid.UUID) -> list[SubmissionSummary]:
        with self._sessions.begin() as s:
            a = s.get(Assignment, assignment_id)
            if a is None:
                raise NotFoundError(f"Assignment {assignment_id} not found")
            subs = sorted(
                s.scalars(select(Submission).where(Submission.assignment_id == assignment_id)),
                key=lambda sub: (sub.submitted_at, sub.platform_submission_id),
                reverse=True,
            )
            statuses: dict[uuid.UUID, list] = {sub.id: [] for sub in subs}
            for st in s.scalars(
                select(SubmissionTarget).where(SubmissionTarget.submission_id.in_(statuses))
            ):
                statuses[st.submission_id].append(target_status(st.ai_needed, st.completed_at, st.last_error))
            return [
                summarize(
                    sub.id,
                    sub.platform_submission_id,
                    sub.student_identifier,
                    sub.submitted_at,
                    sub.is_active_attempt,
                    derive_action(a.grading_mode, sub.submitted_at, sub.ai_authorized_at, sub.ai_graded_submitted_at),
                    statuses[sub.id],
                )
                for sub in subs
            ]

    def get_submission_detail(self, submission_id: uuid.UUID) -> SubmissionDetail:
        with self._sessions.begin() as s:
            sub = self._submission(s, submission_id)
            a = s.get(Assignment, sub.assignment_id)
            rows = s.execute(
                select(SubmissionTarget, GradingTarget)
                .join(GradingTarget, SubmissionTarget.grading_target_id == GradingTarget.id)
                .where(SubmissionTarget.submission_id == submission_id)
            ).all()
            rows.sort(key=lambda r: sort_key(r[1].display_order, r[1].identifier))
            st_ids = [st.id for st, _ in rows]
            tests: dict[uuid.UUID, list[TestResultRecord]] = {i: [] for i in st_ids}
            for r in s.scalars(
                select(TestResult)
                .where(TestResult.submission_target_id.in_(st_ids))
                .order_by(TestResult.recorded_at)
            ):
                tests[r.submission_target_id].append(
                    TestResultRecord(r.status, r.functional_test_feedback, r.recorded_at)
                )
            feedback: dict[uuid.UUID, list[FeedbackDetail]] = {i: [] for i in st_ids}
            for f, version in s.execute(
                select(AiFeedback, Rubric.version)
                .join(Rubric, AiFeedback.rubric_id == Rubric.id)
                .where(AiFeedback.submission_target_id.in_(st_ids))
            ):
                feedback[f.submission_target_id].append(
                    FeedbackDetail(
                        f.id,
                        f.provider,
                        f.model_name,
                        f.prompt_version,
                        f.body,
                        f.suggested_score,
                        f.confidence,
                        f.is_selected,
                        version,
                        f.input_tokens,
                        f.output_tokens,
                        f.generated_at,
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
                tuple(
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
                        tuple(tests[st.id]),
                        tuple(sorted(feedback[st.id], key=feedback_sort_key)),
                    )
                    for st, gt in rows
                ),
            )

    @staticmethod
    def _submission(s: Session, submission_id: uuid.UUID) -> Submission:
        sub = s.get(Submission, submission_id)
        if sub is None:
            raise NotFoundError(f"Submission {submission_id} not found")
        return sub

    def _target(
        self, s: Session, submission_id: uuid.UUID, identifier: str, lock: bool = False
    ) -> SubmissionTarget:
        sub = self._submission(s, submission_id)
        gt = s.scalar(
            select(GradingTarget).where(
                GradingTarget.assignment_id == sub.assignment_id,
                GradingTarget.identifier == identifier,
            )
        )
        if gt is None:
            registered = s.scalars(
                select(GradingTarget.identifier)
                .where(GradingTarget.assignment_id == sub.assignment_id)
                .order_by(GradingTarget.identifier)
            ).all()
            raise UnknownTargetError(identifier, list(registered))
        query = select(SubmissionTarget).where(
            SubmissionTarget.submission_id == submission_id,
            SubmissionTarget.grading_target_id == gt.id,
        )
        # Locked for the writes that complete a target, so two runs cannot
        # both pass the not-yet-completed check.
        st = s.scalar(query.with_for_update() if lock else query)
        if st is None:
            # Registered after this submission's targets were created.
            st = SubmissionTarget(submission_id=submission_id, grading_target_id=gt.id, ai_needed=True)
            s.add(st)
            s.flush()
        return st


def _ref(st: SubmissionTarget, gt: GradingTarget) -> SubmissionTargetRef:
    return SubmissionTargetRef(st.id, gt.identifier, st.ai_needed, st.completed_at)

import uuid

from sqlalchemy import Uuid, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    TEST_STATUSES,
    AiFeedback,
    Assignment,
    GradingTarget,
    Rubric,
    Submission,
    SubmissionTarget,
    TestResult,
)
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

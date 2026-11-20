import uuid
from decimal import Decimal

from sqlalchemy import Uuid, and_, delete, exists, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.assignment_context import check_context
from app.feedback_config import RubricInput
from app.models import (
    AI_STATUSES,
    GRADING_MODES,
    TEST_STATUSES,
    AiFeedback,
    AiFeedbackReview,
    Assignment,
    AssignmentContextFile,
    Course,
    GradingTarget,
    LlmProviderConfig,
    PittApiSettings,
    Rubric,
    Submission,
    SubmissionTarget,
    TestResult,
)
from app.repository.base import (
    Action,
    ActionResult,
    AssignmentConfig,
    AssignmentResults,
    AssignmentSummary,
    ConflictError,
    ContextFile,
    FeedbackDetail,
    FeedbackPackage,
    FeedbackResult,
    FeedbackReview,
    IGradingRepository,
    NotFoundError,
    PittCredential,
    PittSettings,
    ProviderConfig,
    RepositoryError,
    ResultCell,
    ResultColumn,
    ResultRow,
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
    TargetTestOutcome,
    TestResultRecord,
    UnknownTargetError,
    assignment_sort_key,
    check_ai_mode,
    check_assignment_fields,
    check_gradable,
    check_review,
    check_target_analysis,
    check_test_outcomes,
    check_target_fields,
    clean,
    derive_action,
    feedback_history_only,
    feedback_sort_key,
    is_deduction,
    required,
    result_sort_key,
    sort_key,
    student_sort_key,
    summarize,
    summarize_target,
    target_status,
    to_decimal,
)
from app.pitt_settings import PITT_MODELS, validate_pitt_model


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
                    is_active_attempt=None,
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

    def sync_active_submissions(
        self, assignment_id: uuid.UUID, platform_submission_ids: list[str],
    ) -> list[str]:
        with self._sessions.begin() as s:
            if s.get(Assignment, assignment_id, with_for_update=True) is None:
                raise NotFoundError(f"Assignment {assignment_id} not found")
            selected = set(platform_submission_ids)
            known = set(s.scalars(
                select(Submission.platform_submission_id).where(Submission.assignment_id == assignment_id)
            ))
            missing = sorted(selected - known)
            if missing:
                return missing
            s.execute(
                update(Submission).where(Submission.assignment_id == assignment_id)
                .values(is_active_attempt=Submission.platform_submission_id.in_(selected))
            )
            return []

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

    def get_ai_execution_action(self, submission_id: uuid.UUID) -> str:
        with self._sessions.begin() as s:
            sub = self._submission(s, submission_id)
            mode = s.scalar(select(Assignment.grading_mode).where(Assignment.id == sub.assignment_id))
            if mode == "IMMEDIATE":
                return "execute"
            if sub.ai_authorized_at is None:
                return "deferred"
            return "execute" if sub.is_active_attempt else "inactive"

    def defer_submission_target(self, submission_id: uuid.UUID, target: str) -> str:
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target, lock=True)
            # Match target-result writers' lock order; serialize the decision
            # with submission authorization and completion updates.
            sub = s.get(Submission, submission_id, with_for_update=True, populate_existing=True)
            mode = s.scalar(select(Assignment.grading_mode).where(Assignment.id == sub.assignment_id))
            if mode == "IMMEDIATE":
                return "execute"
            if sub.ai_authorized_at is not None:
                return "execute" if sub.is_active_attempt else "inactive"
            check_gradable(target, st.ai_needed)
            st.completed_at = None
            sub.ai_graded_submitted_at = None
            return "deferred"

    def authorize_for_grading(self, submission_id: uuid.UUID, trigger_source: str) -> bool:
        if trigger_source not in ("PENDING_NIGHTLY", "AI_FAILURES"):
            raise ValueError("trigger_source must be PENDING_NIGHTLY or AI_FAILURES")
        with self._sessions.begin() as s:
            sub = s.get(Submission, submission_id, with_for_update=True)
            if sub is None:
                raise NotFoundError(f"Submission {submission_id} not found")
            assignment = s.get(Assignment, sub.assignment_id)
            if not assignment.is_active or not sub.is_active_attempt:
                raise ConflictError("Submission is not active")
            if trigger_source == "AI_FAILURES":
                if sub.ai_status != "FAILED" or (
                    assignment.grading_mode == "NIGHTLY" and sub.ai_authorized_at is None
                ):
                    raise ConflictError("Submission is not eligible for AI failure retry")
                return False
            unfinished = s.scalar(select(exists().where(
                SubmissionTarget.submission_id == submission_id,
                SubmissionTarget.ai_needed,
                SubmissionTarget.completed_at.is_(None),
            )))
            eligible = (
                sub.ai_authorized_at is None if sub.ai_status == "FAILED"
                else sub.ai_graded_submitted_at is None and unfinished
            )
            if assignment.grading_mode != "NIGHTLY" or not eligible:
                raise ConflictError("Submission is not eligible for nightly processing")
            if sub.ai_authorized_at is not None:
                return False
            sub.ai_authorized_at = func.now()
            return True

    def get_test_results(self, submission_id: uuid.UUID, target: str) -> list[TestResultRecord]:
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target)
            rows = s.scalars(
                select(TestResult)
                .where(TestResult.submission_target_id == st.id)
                .order_by(TestResult.recorded_at)
            ).all()
            return [TestResultRecord(r.status, r.functional_test_feedback, r.recorded_at) for r in rows]

    def get_cached_feedback(self, submission_id: uuid.UUID, target: str, digest: str) -> dict | None:
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target)
            return s.scalar(
                select(AiFeedback.cached_result)
                .join(SubmissionTarget, SubmissionTarget.id == AiFeedback.submission_target_id)
                .where(
                    SubmissionTarget.id == st.id,
                    SubmissionTarget.ai_needed,
                    SubmissionTarget.completed_at.is_not(None),
                    SubmissionTarget.last_error.is_(None),
                    AiFeedback.is_selected,
                    AiFeedback.cache_digest == digest,
                )
            )

    def save_feedback(self, submission_id: uuid.UUID, target: str, result: FeedbackResult) -> None:
        with self._sessions.begin() as s:
            st = self._target(s, submission_id, target, lock=True)
            check_gradable(target, st.ai_needed)
            rubric = s.get(Rubric, result.rubric_id)
            if rubric is None or rubric.grading_target_id != st.grading_target_id:
                raise ValueError(f"Rubric {result.rubric_id} is not a rubric for target {target!r}")
            s.execute(
                update(AiFeedback)
                .where(AiFeedback.submission_target_id == st.id, AiFeedback.is_selected)
                .values(is_selected=False)
            )
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
                        cache_digest=r.cache_digest,
                        cached_result=r.cached_result,
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
            check_gradable(target, st.ai_needed)
            s.execute(
                update(AiFeedback)
                .where(AiFeedback.submission_target_id == st.id, AiFeedback.is_selected)
                .values(is_selected=False)
            )
            st.completed_at = func.now()
            st.last_error = error
            st.selection_reason = None
            st.score_spread = None
            st.agreement_score = None
            sub = self._submission(s, submission_id)
            sub.ai_status = "FAILED"
            sub.ai_graded_submitted_at = None

    def set_submission_ai_status(self, submission_id: uuid.UUID, status: str) -> None:
        if status not in AI_STATUSES:
            raise ValueError(f"status must be one of {', '.join(AI_STATUSES)}, not {status!r}")
        with self._sessions.begin() as s:
            sub = self._submission(s, submission_id)
            sub.ai_status = status
            sub.ai_graded_submitted_at = sub.submitted_at if status == "SUCCEEDED" else None

    def mark_ai_complete_if_all_done(self, submission_id: uuid.UUID) -> bool:
        with self._sessions.begin() as s:
            self._submission(s, submission_id)
            unfinished = exists().where(
                SubmissionTarget.submission_id == submission_id,
                SubmissionTarget.ai_needed,
                or_(SubmissionTarget.completed_at.is_(None), SubmissionTarget.last_error.is_not(None)),
            )
            # One statement, so a target completing concurrently cannot slip
            # between the check and the write.
            completed_at = s.scalar(
                update(Submission)
                .where(
                    Submission.id == submission_id,
                    Submission.ai_status.is_distinct_from("FAILED"),
                    ~unfinished,
                )
                .values(ai_graded_submitted_at=Submission.submitted_at)
                .returning(Submission.ai_graded_submitted_at)
            )
            return completed_at is not None

    def get_assignment_token(self, assignment_id: uuid.UUID) -> str:
        with self._sessions() as session:
            token = session.scalar(select(Assignment.assignment_token).where(Assignment.id == assignment_id))
            if token is None:
                raise NotFoundError(f"Assignment {assignment_id} not found")
            return str(token)

    def get_assignment_config(self, assignment_id: uuid.UUID) -> AssignmentConfig:
        with self._sessions.begin() as s:
            # Targets, rubrics and the selected provider must describe one DB snapshot.
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
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
                    None if r is None else _rubric_config(r),
                    gt.analysis_config,
                )
                for gt, r in rows
            )
            providers = s.scalars(
                select(LlmProviderConfig)
                .where(LlmProviderConfig.is_enabled)
                .order_by(LlmProviderConfig.is_primary.desc(), LlmProviderConfig.provider, LlmProviderConfig.model_name)
            ).all()
            credential = s.execute(
                select(PittApiSettings.encrypted_key.is_not(None), PittApiSettings.key_version)
                .where(PittApiSettings.id == 1)
            ).one_or_none()
            has_custom_key, key_version = credential or (False, 0)
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
                a.context_notes,
                tuple(self._context_files(s, a.id)),
                "custom" if has_custom_key else "server",
                key_version,
                a.ai_mode,
            )

    def get_pitt_settings(self) -> PittSettings:
        with self._sessions.begin() as s:
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            model = s.scalar(select(LlmProviderConfig.model_name).where(
                LlmProviderConfig.engine == "pitt", LlmProviderConfig.is_enabled,
                LlmProviderConfig.is_primary,
            ))
            credential = s.execute(
                select(PittApiSettings.encrypted_key.is_not(None), PittApiSettings.key_version)
                .where(PittApiSettings.id == 1)
            ).one_or_none()
            has_custom_key, key_version = credential or (False, 0)
            return PittSettings(model, has_custom_key, key_version)

    def get_pitt_credential(self) -> PittCredential:
        with self._sessions.begin() as s:
            row = s.get(PittApiSettings, 1)
            return PittCredential(row.encrypted_key, row.key_version) if row else PittCredential(None, 0)

    def save_pitt_settings(self, model: str, encrypted_key: str | None) -> None:
        model = validate_pitt_model(model)
        vendor = PITT_MODELS[model]["provider"]
        with self._sessions.begin() as s:
            credential = s.get(PittApiSettings, 1, with_for_update=True)
            if credential is None:
                raise RepositoryError("Pitt settings are unavailable; apply database migrations.")
            providers = s.scalars(
                select(LlmProviderConfig).order_by(LlmProviderConfig.id).with_for_update()
            ).all()
            selected = next((p for p in providers if p.engine == "pitt" and p.model_name == model), None)
            for provider in providers:
                provider.is_primary = False
            # Release the existing partial unique index entry within this transaction.
            s.flush()
            if selected is None:
                selected = LlmProviderConfig(engine="pitt", provider=vendor, model_name=model)
                s.add(selected)
            selected.provider = vendor
            selected.is_enabled = True
            selected.is_primary = True
            credential.encrypted_key = encrypted_key
            credential.key_version += 1

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
                    or_(
                        and_(
                            Submission.ai_status.is_distinct_from("FAILED"),
                            Submission.ai_graded_submitted_at.is_(None),
                            pending,
                        ),
                        and_(Submission.ai_status == "FAILED", Submission.ai_authorized_at.is_(None)),
                    ),
                )
                .order_by(Submission.submitted_at, Submission.id)
            ).all()
            return [
                SubmissionLocator(
                    sub.id, a.platform, a.platform_assignment_id, sub.platform_submission_id, sub.student_identifier
                )
                for sub, a in rows
            ]

    def list_submissions_failed_ai(self, retry_eligible: bool = False) -> list[SubmissionLocator]:
        with self._sessions.begin() as s:
            query = (
                select(Submission, Assignment)
                .join(Assignment, Submission.assignment_id == Assignment.id)
                .where(Assignment.is_active, Submission.is_active_attempt, Submission.ai_status == "FAILED")
                .order_by(Submission.submitted_at, Submission.id)
            )
            if retry_eligible:
                query = query.where(or_(
                    Assignment.grading_mode == "IMMEDIATE", Submission.ai_authorized_at.is_not(None),
                ))
            rows = s.execute(query).all()
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
                    a.ai_mode,
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
                    ai_status=sub.ai_status,
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
            latest_review: dict[uuid.UUID, FeedbackReview] = {}
            for r in s.scalars(
                select(AiFeedbackReview)
                .join(AiFeedback, AiFeedbackReview.ai_feedback_id == AiFeedback.id)
                .where(AiFeedback.submission_target_id.in_(st_ids))
                .order_by(AiFeedbackReview.reviewed_at)
            ):
                latest_review[r.ai_feedback_id] = FeedbackReview(
                    r.instructor_rating, r.instructor_comment, r.reviewer, r.reviewed_at
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
                        latest_review.get(f.id),
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
                        feedback_history_only=feedback_history_only(st.ai_needed, gt.analysis_config),
                    )
                    for st, gt in rows
                ),
            )

    def record_test_run(
        self, submission_id: uuid.UUID, run_id: str, outcomes: list[TargetTestOutcome]
    ) -> int:
        check_test_outcomes(outcomes)
        try:
            with self._sessions.begin() as s:
                self._submission(s, submission_id)
                targets = {
                    gt.identifier: st
                    for st, gt in s.execute(
                        select(SubmissionTarget, GradingTarget)
                        .join(GradingTarget, SubmissionTarget.grading_target_id == GradingTarget.id)
                        .where(SubmissionTarget.submission_id == submission_id)
                        .with_for_update(of=SubmissionTarget)
                    )
                }
                ids = [st.id for st in targets.values()]
                if ids and s.scalar(select(exists().where(
                    TestResult.submission_target_id.in_(ids), TestResult.run_id == run_id,
                ))):
                    return 0
                added = 0
                for o in outcomes:
                    st = targets.get(o.target)
                    if st is None:
                        continue
                    if o.status is not None:
                        s.add(TestResult(submission_target_id=st.id, status=o.status,
                                         functional_test_feedback=o.message, run_id=run_id))
                        added += 1
                    if o.ai_needed is not None:
                        st.ai_needed = o.ai_needed
                s.flush()
                return added
        except IntegrityError:
            # A concurrent retry of the same run got there first.
            return 0

    def create_review(
        self, ai_feedback_id: uuid.UUID, *, rating: int, comment: str | None, reviewer: str | None
    ) -> uuid.UUID:
        comment = check_review(rating, comment)
        with self._sessions.begin() as s:
            submission_id = s.scalar(
                select(SubmissionTarget.submission_id)
                .join(AiFeedback, AiFeedback.submission_target_id == SubmissionTarget.id)
                .where(AiFeedback.id == ai_feedback_id)
            )
            if submission_id is None:
                raise NotFoundError(f"AI feedback {ai_feedback_id} not found")
            s.add(AiFeedbackReview(
                ai_feedback_id=ai_feedback_id,
                instructor_rating=rating,
                instructor_comment=comment,
                reviewer=clean(reviewer),
            ))
            return submission_id

    def list_target_summaries(self, assignment_id: uuid.UUID) -> list[TargetSummary]:
        with self._sessions.begin() as s:
            if s.get(Assignment, assignment_id) is None:
                raise NotFoundError(f"Assignment {assignment_id} not found")
            targets = sorted(
                s.scalars(select(GradingTarget).where(GradingTarget.assignment_id == assignment_id)),
                key=lambda gt: sort_key(gt.display_order, gt.identifier),
            )
            statuses: dict[uuid.UUID, list] = {gt.id: [] for gt in targets}
            tested = exists().where(TestResult.submission_target_id == SubmissionTarget.id)
            for st in s.scalars(
                select(SubmissionTarget)
                .join(Submission, SubmissionTarget.submission_id == Submission.id)
                .where(Submission.assignment_id == assignment_id, Submission.is_active_attempt.is_not(False), tested)
            ):
                statuses[st.grading_target_id].append(target_status(st.ai_needed, st.completed_at, st.last_error))
            return [
                summarize_target(gt.id, gt.identifier, gt.kind, gt.max_score, statuses[gt.id])
                for gt in targets
            ]

    def get_target_submissions(self, assignment_id: uuid.UUID, target_id: uuid.UUID) -> TargetSubmissions:
        with self._sessions.begin() as s:
            a = s.get(Assignment, assignment_id)
            gt = s.get(GradingTarget, target_id)
            if a is None or gt is None or gt.assignment_id != a.id:
                raise NotFoundError(f"Target {target_id} not found on assignment {assignment_id}")
            rows = s.execute(
                select(SubmissionTarget, Submission)
                .join(Submission, SubmissionTarget.submission_id == Submission.id)
                .where(SubmissionTarget.grading_target_id == target_id, Submission.is_active_attempt.is_not(False))
            ).all()
            st_ids = [st.id for st, _ in rows]
            # In recorded order, so each target ends up with its latest.
            latest: dict[uuid.UUID, TestResultRecord] = {}
            for r in s.scalars(
                select(TestResult)
                .where(TestResult.submission_target_id.in_(st_ids))
                .order_by(TestResult.recorded_at)
            ):
                latest[r.submission_target_id] = TestResultRecord(r.status, r.functional_test_feedback, r.recorded_at)
            scores = dict(s.execute(
                select(AiFeedback.submission_target_id, AiFeedback.suggested_score)
                .where(AiFeedback.submission_target_id.in_(st_ids), AiFeedback.is_selected)
            ).all())
            submissions = [
                TargetSubmission(
                    sub.id,
                    sub.platform_submission_id,
                    sub.student_identifier,
                    sub.submitted_at,
                    target_status(st.ai_needed, st.completed_at, st.last_error),
                    latest[st.id],
                    scores.get(st.id),
                )
                for st, sub in rows
                if st.id in latest
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

    def list_results(self, assignment_id: uuid.UUID) -> AssignmentResults:
        with self._sessions.begin() as s:
            a = s.get(Assignment, assignment_id)
            if a is None:
                raise NotFoundError(f"Assignment {assignment_id} not found")
            targets = sorted(
                s.scalars(select(GradingTarget).where(GradingTarget.assignment_id == assignment_id)),
                key=lambda gt: sort_key(gt.display_order, gt.identifier),
            )
            subs = list(s.scalars(
                select(Submission).where(Submission.assignment_id == assignment_id, Submission.is_active_attempt.is_not(False))
            ))
            sts = list(s.scalars(
                select(SubmissionTarget).where(SubmissionTarget.submission_id.in_([sub.id for sub in subs]))
            ))
            st_ids = [st.id for st in sts]
            # In recorded order, so each target ends up with its latest.
            latest: dict[uuid.UUID, str] = {}
            for st_id, status in s.execute(
                select(TestResult.submission_target_id, TestResult.status)
                .where(TestResult.submission_target_id.in_(st_ids))
                .order_by(TestResult.recorded_at)
            ):
                latest[st_id] = status
            selected = {
                st_id: (score, body)
                for st_id, score, body in s.execute(
                    select(AiFeedback.submission_target_id, AiFeedback.suggested_score, AiFeedback.body)
                    .where(AiFeedback.submission_target_id.in_(st_ids), AiFeedback.is_selected)
                )
            }
            by_pair = {(st.submission_id, st.grading_target_id): st for st in sts}

            def cell(sub: Submission, gt: GradingTarget) -> ResultCell | None:
                st = by_pair.get((sub.id, gt.id))
                if st is None:
                    return None
                test = latest.get(st.id)
                score, body = selected.get(st.id, (None, None))
                if feedback_history_only(st.ai_needed, gt.analysis_config):
                    body = None
                return ResultCell(
                    target_status(st.ai_needed, st.completed_at, st.last_error),
                    test, score, is_deduction(test, score, gt.max_score),
                    body, st.last_error,
                )

            rows = [
                ResultRow(
                    sub.id,
                    sub.platform_submission_id,
                    sub.student_identifier,
                    sub.submitted_at,
                    derive_action(a.grading_mode, sub.submitted_at, sub.ai_authorized_at, sub.ai_graded_submitted_at),
                    sub.ai_status,
                    tuple(cell(sub, gt) for gt in targets),
                )
                for sub in subs
            ]
            return AssignmentResults(
                a.id,
                a.name,
                a.grading_mode,
                tuple(ResultColumn(gt.id, gt.identifier, gt.kind, gt.max_score) for gt in targets),
                tuple(sorted(rows, key=result_sort_key)),
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
        ai_mode: str | None = None,
    ) -> uuid.UUID:
        course_name = required(course_name, "Course")
        term = clean(term)
        platform_assignment_id = required(platform_assignment_id, "Gradescope assignment id")
        grading_mode, language = check_assignment_fields(grading_mode, language, ai_mode)
        with self._sessions.begin() as s:
            course_id = s.scalar(
                select(Course.id)
                .where(Course.name == course_name, Course.term.is_not_distinct_from(term))
                .limit(1)
            )
            if course_id is None:
                course = Course(name=course_name, term=term)
                s.add(course)
                s.flush()
                course_id = course.id
            inserted = s.scalar(
                insert(Assignment)
                .values(
                    course_id=course_id,
                    platform="GRADESCOPE",
                    platform_assignment_id=platform_assignment_id,
                    name=clean(name),
                    grading_mode=grading_mode,
                    ai_mode=ai_mode,
                    language=language,
                )
                .on_conflict_do_nothing(index_elements=["platform", "platform_assignment_id"])
                .returning(Assignment.id)
            )
            if inserted is None:
                raise ConflictError(f"Gradescope assignment {platform_assignment_id!r} is already registered")
            return inserted

    def set_ai_mode(self, assignment_id: uuid.UUID, ai_mode: str) -> None:
        check_ai_mode(ai_mode)
        with self._sessions.begin() as s:
            assignment = self._locked_assignment(s, assignment_id)
            if assignment.ai_mode != ai_mode and s.scalar(
                select(exists().where(GradingTarget.assignment_id == assignment_id))
            ):
                raise ConflictError("The AI mode is locked while this assignment has targets")
            assignment.ai_mode = ai_mode

    def create_target(
        self,
        assignment_id: uuid.UUID,
        *,
        identifier: str,
        kind: str,
        max_score: Decimal | None,
        analysis_config: dict | None = None,
        rubric: RubricInput | None = None,
    ) -> uuid.UUID:
        identifier = check_target_fields(identifier, kind, max_score)
        with self._sessions.begin() as s:
            # Locked so two targets added at once cannot take the same display_order.
            assignment = self._locked_assignment(s, assignment_id)
            if analysis_config or rubric or assignment.ai_mode:
                analysis_config, max_score = check_target_analysis(
                    identifier, kind, assignment.language, analysis_config or {}, max_score,
                    ai_mode=assignment.ai_mode, rubric=rubric, draft=rubric is None,
                )
            if rubric and analysis_config.get("behavior") == "functional_test_feedback":
                self._check_feedback_target(s, assignment)
            last = s.scalar(
                select(func.max(GradingTarget.display_order)).where(GradingTarget.assignment_id == assignment_id)
            )
            inserted = s.scalar(
                insert(GradingTarget)
                .values(
                    assignment_id=assignment_id,
                    identifier=identifier,
                    kind=kind,
                    max_score=max_score,
                    analysis_config=analysis_config or {},
                    display_order=(last or 0) + 1,
                )
                .on_conflict_do_nothing(index_elements=["assignment_id", "identifier"])
                .returning(GradingTarget.id)
            )
            if inserted is None:
                raise ConflictError(f"Target {identifier!r} is already registered on this assignment")
            if rubric:
                s.add(Rubric(
                    grading_target_id=inserted, version=1, is_current=True,
                    criteria_text=rubric.criteria_text.strip(), feedback_guidance=clean(rubric.feedback_guidance),
                ))
            return inserted

    def delete_target(self, assignment_id: uuid.UUID, target_id: uuid.UUID) -> None:
        with self._sessions.begin() as s:
            # Locked so a submission run cannot register against it mid-delete.
            gt = s.get(GradingTarget, target_id, with_for_update=True)
            if gt is None or gt.assignment_id != assignment_id:
                raise NotFoundError(f"Grading target {target_id} not found")
            links = s.scalars(select(SubmissionTarget).where(
                SubmissionTarget.grading_target_id == target_id,
            ).order_by(SubmissionTarget.id).with_for_update()).all()
            rubrics = s.execute(select(Rubric.id, Rubric.is_current).where(
                Rubric.grading_target_id == target_id,
            ).with_for_update()).all()
            link_ids = [link.id for link in links]
            rubric_ids = [rubric.id for rubric in rubrics]
            test_results = exists().where(TestResult.submission_target_id.in_(link_ids))
            feedback = exists().where(or_(AiFeedback.submission_target_id.in_(link_ids),
                                          AiFeedback.rubric_id.in_(rubric_ids)))
            has_processing_state = any(
                not link.ai_needed or any(value is not None for value in (
                    link.completed_at, link.last_error, link.agreement_score, link.score_spread, link.selection_reason,
                ))
                for link in links
            )
            if has_processing_state or s.scalar(select(or_(test_results, feedback))):
                raise ConflictError(f"Target {gt.identifier!r} has grading history and cannot be deleted")
            if links and any(rubric.is_current for rubric in rubrics):
                raise ConflictError(f"Target {gt.identifier!r} has a current rubric and submission links and cannot be deleted")
            if links:
                s.execute(delete(SubmissionTarget).where(SubmissionTarget.id.in_(link_ids)))
            s.execute(delete(Rubric).where(Rubric.grading_target_id == target_id))
            s.delete(gt)

    def set_context_notes(self, assignment_id: uuid.UUID, notes: str | None) -> None:
        with self._sessions.begin() as s:
            a = self._locked_assignment(s, assignment_id)
            notes = clean(notes)
            check_context(notes, [f.content for f in self._context_files(s, a.id)])
            a.context_notes = notes

    def add_context_files(self, assignment_id: uuid.UUID, files: list[tuple[str, str]]) -> list[uuid.UUID]:
        with self._sessions.begin() as s:
            a = self._locked_assignment(s, assignment_id)
            existing = [f.content for f in self._context_files(s, a.id)]
            check_context(a.context_notes, existing + [content for _, content in files])
            start = s.scalar(
                select(func.coalesce(func.max(AssignmentContextFile.position), 0))
                .where(AssignmentContextFile.assignment_id == a.id)
            )
            rows = [
                AssignmentContextFile(assignment_id=a.id, filename=name, content=content, position=start + i)
                for i, (name, content) in enumerate(files, 1)
            ]
            s.add_all(rows)
            s.flush()
            return [r.id for r in rows]

    def delete_context_file(self, assignment_id: uuid.UUID, file_id: uuid.UUID) -> None:
        with self._sessions.begin() as s:
            f = s.get(AssignmentContextFile, file_id)
            if f is None or f.assignment_id != assignment_id:
                raise NotFoundError(f"Context file {file_id} not found")
            s.delete(f)

    @staticmethod
    def _locked_assignment(s: Session, assignment_id: uuid.UUID) -> Assignment:
        a = s.get(Assignment, assignment_id, with_for_update=True)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found")
        return a

    @staticmethod
    def _check_feedback_target(s: Session, assignment: Assignment, editing: uuid.UUID | None = None) -> None:
        others = select(GradingTarget.id).where(GradingTarget.assignment_id == assignment.id)
        if editing is not None:
            others = others.where(GradingTarget.id != editing)
        if assignment.ai_mode is None:
            others = others.join(Rubric).where(
                Rubric.is_current, GradingTarget.analysis_config["behavior"].astext == "functional_test_feedback",
            )
        if s.scalar(others.limit(1)) is not None:
            raise ConflictError("An assignment supports only one active Functional-test feedback target")

    @staticmethod
    def _context_files(s: Session, assignment_id: uuid.UUID) -> list[ContextFile]:
        return [
            ContextFile(f.id, f.filename, f.content, f.uploaded_at)
            for f in s.scalars(
                select(AssignmentContextFile)
                .where(AssignmentContextFile.assignment_id == assignment_id)
                .order_by(AssignmentContextFile.position)
            )
        ]

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
        rubric_input = RubricInput(criteria_text=criteria_text, feedback_guidance=feedback_guidance or "")
        with self._sessions.begin() as s:
            assignment_id = s.scalar(select(GradingTarget.assignment_id).where(GradingTarget.id == grading_target_id))
            if assignment_id is None:
                raise NotFoundError(f"Grading target {grading_target_id} not found")
            # Serialize activation across targets before locking an individual target.
            assignment = s.get(Assignment, assignment_id, with_for_update=True)
            target = s.get(GradingTarget, grading_target_id, with_for_update=True)
            if target is None:
                raise NotFoundError(f"Grading target {grading_target_id} not found")
            if analysis_config is None:
                analysis_config, max_score = target.analysis_config, target.max_score
            check_target_fields(target.identifier, target.kind, max_score)
            analysis_config, max_score = check_target_analysis(
                target.identifier, target.kind, assignment.language,
                analysis_config, max_score, rubric=rubric_input, ai_mode=assignment.ai_mode,
            )
            if analysis_config.get("behavior") == "functional_test_feedback":
                self._check_feedback_target(s, assignment, editing=grading_target_id)
            target.analysis_config = analysis_config
            target.max_score = max_score
            current = s.scalar(
                select(Rubric).where(Rubric.grading_target_id == grading_target_id, Rubric.is_current)
            )
            if current is not None and (current.criteria_text, current.feedback_guidance) == (
                criteria_text,
                feedback_guidance,
            ):
                return _rubric_config(current)
            last = s.scalar(select(func.max(Rubric.version)).where(Rubric.grading_target_id == grading_target_id))
            # Demoted first: the partial unique index allows one current rubric.
            s.execute(
                update(Rubric).where(Rubric.grading_target_id == grading_target_id).values(is_current=False)
            )
            rubric = Rubric(
                grading_target_id=grading_target_id,
                version=(last or 0) + 1,
                criteria_text=criteria_text,
                feedback_guidance=feedback_guidance,
                is_current=True,
            )
            s.add(rubric)
            s.flush()
            return _rubric_config(rubric)

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
        # Serialize target outcomes and replacement of the selected feedback.
        st = s.scalar(query.with_for_update() if lock else query)
        if st is None:
            # Registered after this submission's targets were created.
            st = SubmissionTarget(submission_id=submission_id, grading_target_id=gt.id, ai_needed=True)
            s.add(st)
            s.flush()
        return st


def _ref(st: SubmissionTarget, gt: GradingTarget) -> SubmissionTargetRef:
    return SubmissionTargetRef(st.id, gt.identifier, st.ai_needed, st.completed_at)


def _rubric_config(r: Rubric) -> RubricConfig:
    return RubricConfig(r.id, r.version, r.criteria_text, r.feedback_guidance)

"""Contract tests for IGradingRepository, run against both implementations.

The SQL run needs a Postgres database and is skipped without one. With
the web app's docker compose running:

    docker compose exec db createdb -U autograder autograder_test

Override the location with TEST_DATABASE_URL.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import NamedTuple
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models import (
    AiFeedback,
    Assignment,
    Base,
    Course,
    GradingTarget,
    LlmProviderConfig,
    Rubric,
    Submission,
    SubmissionTarget,
    TriggerRun,
    TriggerRunSubmission,
)
from app.repository import (
    Action,
    ConflictError,
    FeedbackResult,
    InMemoryRepository,
    NotFoundError,
    ProviderFeedback,
    SqlGradingRepository,
    SubmissionRef,
    TargetStatus,
    TargetTestOutcome,
    UnknownTargetError,
    derive_action,
    is_deduction,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://autograder:autograder@localhost:5432/autograder_test",
)

SUBMITTED = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)


class TargetState(NamedTuple):
    completed: bool
    last_error: str | None
    selection_reason: str | None
    score_spread: Decimal | None
    agreement_score: Decimal | None


# ---------------------------------------------------------------------
# Backends. Each seeds instructor config directly, so tests of the
# runtime methods do not lean on the configuration writes, and reads rows
# back so tests can check what was stored.
# ---------------------------------------------------------------------
class MemoryBackend:
    def __init__(self):
        self.repo = InMemoryRepository()

    def add_assignment(self, mode, targets):
        aid = self.repo.add_assignment(mode)
        for identifier, order in targets:
            self.repo.add_grading_target(aid, identifier, order)
        return aid

    def add_target(self, assignment_id, identifier):
        self.repo.add_grading_target(assignment_id, identifier)

    def set_submission(self, submission_id, **fields):
        for name, value in fields.items():
            setattr(self.repo.submissions[submission_id], name, value)

    def set_completed(self, submission_id, identifier):
        for st in self.repo.submission_targets.values():
            gt = self.repo.grading_targets[st.grading_target_id]
            if st.submission_id == submission_id and gt.identifier == identifier:
                st.completed_at = datetime.now(timezone.utc)

    def test_results(self, submission_id):
        ids = {st.id for st in self.repo.submission_targets.values() if st.submission_id == submission_id}
        return [(r.status, r.functional_test_feedback) for r in self.repo.test_results if r.submission_target_id in ids]

    def _submission_target(self, submission_id, identifier):
        return next(
            st for st in self.repo.submission_targets.values()
            if st.submission_id == submission_id
            and self.repo.grading_targets[st.grading_target_id].identifier == identifier
        )

    def _grading_target(self, assignment_id, identifier):
        return next(
            gt for gt in self.repo.grading_targets.values()
            if gt.assignment_id == assignment_id and gt.identifier == identifier
        )

    def add_rubric(self, assignment_id, identifier, **fields):
        return self.repo.add_rubric(self._grading_target(assignment_id, identifier).id, **fields)

    def configure_target(self, assignment_id, identifier, kind, max_score):
        gt = self._grading_target(assignment_id, identifier)
        gt.kind, gt.max_score = kind, max_score

    def add_provider(self, provider, model_name, is_enabled=True, is_primary=False):
        self.repo.add_provider(provider, model_name, is_enabled=is_enabled, is_primary=is_primary)

    def set_assignment(self, assignment_id, **fields):
        for name, value in fields.items():
            setattr(self.repo.assignments[assignment_id], name, value)

    def target_state(self, submission_id, identifier):
        st = self._submission_target(submission_id, identifier)
        return TargetState(
            st.completed_at is not None, st.last_error, st.selection_reason, st.score_spread, st.agreement_score
        )

    def feedback_rows(self, submission_id, identifier):
        st = self._submission_target(submission_id, identifier)
        return [
            (f.provider, f.is_selected, f.body, f.suggested_score)
            for f in self.repo.ai_feedback
            if f.submission_target_id == st.id
        ]

    def submission(self, submission_id):
        sub = self.repo.submissions[submission_id]
        return sub.ai_authorized_at, sub.ai_graded_submitted_at

    def course_of(self, assignment_id):
        c = self.repo.courses[self.repo.assignments[assignment_id].course_id]
        return c.id, c.name, c.term


class SqlBackend:
    def __init__(self, sessions):
        self.sessions = sessions
        self.repo = SqlGradingRepository(sessions)

    def add_assignment(self, mode, targets):
        with self.sessions.begin() as s:
            course = Course(name="CS 1")
            s.add(course)
            s.flush()
            assignment = Assignment(
                course_id=course.id,
                platform="GRADESCOPE",
                platform_assignment_id=str(uuid.uuid4()),
                grading_mode=mode,
                language="PYTHON",
            )
            s.add(assignment)
            s.flush()
            for identifier, order in targets:
                s.add(GradingTarget(assignment_id=assignment.id, identifier=identifier, kind="METHOD", display_order=order))
            return assignment.id

    def add_target(self, assignment_id, identifier):
        with self.sessions.begin() as s:
            s.add(GradingTarget(assignment_id=assignment_id, identifier=identifier, kind="METHOD"))

    def set_submission(self, submission_id, **fields):
        with self.sessions.begin() as s:
            s.execute(update(Submission).where(Submission.id == submission_id).values(**fields))

    def set_completed(self, submission_id, identifier):
        with self.sessions.begin() as s:
            s.execute(
                text(
                    "UPDATE submission_target st SET completed_at = now() FROM grading_target gt "
                    "WHERE st.grading_target_id = gt.id AND st.submission_id = :sid AND gt.identifier = :ident"
                ),
                {"sid": submission_id, "ident": identifier},
            )

    def test_results(self, submission_id):
        with self.sessions.begin() as s:
            rows = s.execute(
                text(
                    "SELECT tr.status, tr.functional_test_feedback FROM test_result tr "
                    "JOIN submission_target st ON tr.submission_target_id = st.id "
                    "WHERE st.submission_id = :sid ORDER BY tr.recorded_at"
                ),
                {"sid": submission_id},
            ).all()
            return [tuple(r) for r in rows]

    @staticmethod
    def _submission_target(s, submission_id, identifier):
        return s.scalar(
            select(SubmissionTarget)
            .join(GradingTarget, SubmissionTarget.grading_target_id == GradingTarget.id)
            .where(SubmissionTarget.submission_id == submission_id, GradingTarget.identifier == identifier)
        )

    @staticmethod
    def _grading_target(s, assignment_id, identifier):
        return s.scalar(
            select(GradingTarget).where(
                GradingTarget.assignment_id == assignment_id, GradingTarget.identifier == identifier
            )
        )

    def add_rubric(
        self, assignment_id, identifier, version=1, criteria_text="Correct and readable.",
        feedback_guidance=None, is_current=True,
    ):
        with self.sessions.begin() as s:
            gt = self._grading_target(s, assignment_id, identifier)
            if is_current:
                s.execute(update(Rubric).where(Rubric.grading_target_id == gt.id).values(is_current=False))
            rubric = Rubric(
                grading_target_id=gt.id, version=version, criteria_text=criteria_text,
                feedback_guidance=feedback_guidance, is_current=is_current,
            )
            s.add(rubric)
            s.flush()
            return rubric.id

    def configure_target(self, assignment_id, identifier, kind, max_score):
        with self.sessions.begin() as s:
            gt = self._grading_target(s, assignment_id, identifier)
            gt.kind, gt.max_score = kind, max_score

    def add_provider(self, provider, model_name, is_enabled=True, is_primary=False):
        with self.sessions.begin() as s:
            s.add(LlmProviderConfig(provider=provider, model_name=model_name, is_enabled=is_enabled, is_primary=is_primary))

    def set_assignment(self, assignment_id, **fields):
        with self.sessions.begin() as s:
            s.execute(update(Assignment).where(Assignment.id == assignment_id).values(**fields))

    def target_state(self, submission_id, identifier):
        with self.sessions.begin() as s:
            st = self._submission_target(s, submission_id, identifier)
            return TargetState(
                st.completed_at is not None, st.last_error, st.selection_reason, st.score_spread, st.agreement_score
            )

    def feedback_rows(self, submission_id, identifier):
        with self.sessions.begin() as s:
            st = self._submission_target(s, submission_id, identifier)
            rows = s.scalars(select(AiFeedback).where(AiFeedback.submission_target_id == st.id)).all()
            return [(f.provider, f.is_selected, f.body, f.suggested_score) for f in rows]

    def submission(self, submission_id):
        with self.sessions.begin() as s:
            sub = s.get(Submission, submission_id)
            return sub.ai_authorized_at, sub.ai_graded_submitted_at

    def course_of(self, assignment_id):
        with self.sessions.begin() as s:
            c = s.get(Course, s.get(Assignment, assignment_id).course_id)
            return c.id, c.name, c.term


@pytest.fixture(scope="session")
def sql_sessions():
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect():
            pass
    except OperationalError:
        pytest.skip(f"No test database at {TEST_DATABASE_URL}")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(params=["memory", "sql"])
def backend(request):
    if request.param == "memory":
        return MemoryBackend()
    sessions = request.getfixturevalue("sql_sessions")
    # Provider config and the pending-AI query are global, so every test
    # starts from empty tables.
    with sessions.begin() as s:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        s.execute(text(f"TRUNCATE {tables} CASCADE"))
    return SqlBackend(sessions)


def start(backend, mode="IMMEDIATE", targets=(("add", 1), ("sub", 2)), platform_id="sub-1"):
    """Register a submission and sync its active selection for these fixtures."""
    aid = backend.add_assignment(mode, targets)
    sid = backend.repo.upsert_submission(SubmissionRef(aid, platform_id, SUBMITTED, "student@example.edu"))
    backend.repo.sync_active_submissions(aid, [platform_id])
    return aid, sid


# ---------------------------------------------------------------------
# upsert_submission
# ---------------------------------------------------------------------
def test_upsert_returns_same_id_on_rerun(backend):
    aid, sid = start(backend)
    again = backend.repo.upsert_submission(SubmissionRef(aid, "sub-1", SUBMITTED))
    assert again == sid


def test_upsert_new_attempt_gets_new_row(backend):
    aid, sid = start(backend)
    other = backend.repo.upsert_submission(SubmissionRef(aid, "sub-2", SUBMITTED + timedelta(hours=1)))
    assert other != sid


def test_upsert_unknown_assignment(backend):
    with pytest.raises(NotFoundError):
        backend.repo.upsert_submission(SubmissionRef(uuid.uuid4(), "sub-1", SUBMITTED))


# ---------------------------------------------------------------------
# create_targets_default_needed
# ---------------------------------------------------------------------
def test_targets_created_needed_in_display_order(backend):
    _, sid = start(backend, targets=(("zeta", None), ("beta", 2), ("alpha", 1)))
    refs = backend.repo.create_targets_default_needed(sid)
    assert [r.identifier for r in refs] == ["alpha", "beta", "zeta"]
    assert all(r.ai_needed and r.completed_at is None for r in refs)


def test_targets_rerun_keeps_opt_out(backend):
    _, sid = start(backend)
    backend.repo.create_targets_default_needed(sid)
    backend.repo.set_ai_not_needed(sid, "add")
    refs = backend.repo.create_targets_default_needed(sid)
    assert {r.identifier: r.ai_needed for r in refs} == {"add": False, "sub": True}


def test_targets_unknown_submission(backend):
    with pytest.raises(NotFoundError):
        backend.repo.create_targets_default_needed(uuid.uuid4())


# ---------------------------------------------------------------------
# set_ai_not_needed
# ---------------------------------------------------------------------
def test_ai_not_needed_reports_change(backend):
    _, sid = start(backend)
    backend.repo.create_targets_default_needed(sid)
    assert backend.repo.set_ai_not_needed(sid, "add") is True
    assert backend.repo.set_ai_not_needed(sid, "add") is False


def test_ai_not_needed_unknown_target_lists_registered(backend):
    _, sid = start(backend)
    backend.repo.create_targets_default_needed(sid)
    with pytest.raises(UnknownTargetError) as err:
        backend.repo.set_ai_not_needed(sid, "Add")
    assert err.value.target == "Add"
    assert err.value.registered == ["add", "sub"]


def test_ai_not_needed_target_registered_after_init(backend):
    aid, sid = start(backend)
    backend.repo.create_targets_default_needed(sid)
    backend.add_target(aid, "mul")
    assert backend.repo.set_ai_not_needed(sid, "mul") is True


# ---------------------------------------------------------------------
# add_test_result
# ---------------------------------------------------------------------
def test_add_test_result_records_in_order(backend):
    _, sid = start(backend)
    backend.repo.create_targets_default_needed(sid)
    backend.repo.add_test_result(sid, "add", "FAIL", "add(2, 2) returned 5")
    backend.repo.add_test_result(sid, "add", "PASS", None)
    assert backend.test_results(sid) == [("FAIL", "add(2, 2) returned 5"), ("PASS", None)]


def test_add_test_result_unknown_target(backend):
    _, sid = start(backend)
    backend.repo.create_targets_default_needed(sid)
    with pytest.raises(UnknownTargetError):
        backend.repo.add_test_result(sid, "nope", "PASS", None)
    assert backend.test_results(sid) == []


def test_add_test_result_bad_status(backend):
    _, sid = start(backend)
    backend.repo.create_targets_default_needed(sid)
    with pytest.raises(ValueError):
        backend.repo.add_test_result(sid, "add", "passed", None)


# ---------------------------------------------------------------------
# decide_action: the AD-11 table, end to end
# ---------------------------------------------------------------------
def test_immediate_grades_pending_targets(backend):
    _, sid = start(backend, mode="IMMEDIATE", targets=(("add", 1), ("sub", 2), ("mul", 3)))
    backend.repo.create_targets_default_needed(sid)
    backend.repo.set_ai_not_needed(sid, "add")
    backend.set_completed(sid, "mul")
    result = backend.repo.decide_action(sid)
    assert result.action is Action.GRADE
    assert [t.identifier for t in result.targets] == ["sub"]


def test_nightly_unauthorized_defers(backend):
    _, sid = start(backend, mode="NIGHTLY")
    backend.repo.create_targets_default_needed(sid)
    result = backend.repo.decide_action(sid)
    assert result.action is Action.DEFER
    assert result.targets == ()


def test_nightly_authorized_grades(backend):
    _, sid = start(backend, mode="NIGHTLY")
    backend.repo.create_targets_default_needed(sid)
    backend.set_submission(sid, ai_authorized_at=datetime.now(timezone.utc))
    result = backend.repo.decide_action(sid)
    assert result.action is Action.GRADE
    assert [t.identifier for t in result.targets] == ["add", "sub"]


@pytest.mark.parametrize("mode", ["IMMEDIATE", "NIGHTLY"])
def test_graded_submission_reemits(backend, mode):
    _, sid = start(backend, mode=mode)
    backend.repo.create_targets_default_needed(sid)
    backend.set_submission(sid, ai_authorized_at=datetime.now(timezone.utc), ai_graded_submitted_at=SUBMITTED)
    assert backend.repo.decide_action(sid).action is Action.REEMIT


def test_decide_unknown_submission(backend):
    with pytest.raises(NotFoundError):
        backend.repo.decide_action(uuid.uuid4())


# ---------------------------------------------------------------------
# derive_action alone: the guard compares against submitted_at (AD-9)
# ---------------------------------------------------------------------
def test_guard_for_a_different_submission_time_does_not_reemit():
    earlier = SUBMITTED - timedelta(days=1)
    assert derive_action("IMMEDIATE", SUBMITTED, None, earlier) is Action.GRADE


# ---------------------------------------------------------------------
# Group 2: the grading run
# ---------------------------------------------------------------------
def result_for(rubric_id, selected=1):
    return FeedbackResult(
        rubric_id=rubric_id,
        responses=(
            ProviderFeedback("ANTHROPIC", "claude-opus-5-5", "Handle the empty list.", "v1", 0.1, None, 900, 120),
            ProviderFeedback("OPENAI", "gpt-6", "Check the empty case.", "v1", 0.3, None, 950, 140),
        ),
        selected_index=selected,
        selection_reason="Selected by the configured selector.",
        score_spread=0.2,
    )


def started(backend, mode="IMMEDIATE", targets=(("add", 1), ("sub", 2))):
    aid, sid = start(backend, mode=mode, targets=targets)
    backend.repo.create_targets_default_needed(sid)
    return aid, sid


def test_authorize_turns_defer_into_grade(backend):
    _, sid = started(backend, mode="NIGHTLY")
    assert backend.repo.decide_action(sid).action is Action.DEFER
    backend.repo.authorize_for_grading(sid, "PENDING_NIGHTLY")
    assert backend.submission(sid)[0] is not None
    assert backend.repo.decide_action(sid).action is Action.GRADE


def test_authorize_unknown_submission(backend):
    with pytest.raises(NotFoundError):
        backend.repo.authorize_for_grading(uuid.uuid4(), "PENDING_NIGHTLY")


def test_get_test_results_in_order_and_per_target(backend):
    _, sid = started(backend)
    backend.repo.add_test_result(sid, "add", "FAIL", "first")
    backend.repo.add_test_result(sid, "sub", "PASS", "other target")
    backend.repo.add_test_result(sid, "add", "ERROR", "second")
    got = backend.repo.get_test_results(sid, "add")
    assert [(r.status, r.functional_test_feedback) for r in got] == [("FAIL", "first"), ("ERROR", "second")]
    assert [r.status for r in backend.repo.get_test_results(sid, "sub")] == ["PASS"]


def test_get_test_results_unknown_target(backend):
    _, sid = started(backend)
    with pytest.raises(UnknownTargetError):
        backend.repo.get_test_results(sid, "nope")


def test_save_feedback_stores_every_provider_and_completes(backend):
    aid, sid = started(backend)
    backend.repo.save_feedback(sid, "add", result_for(backend.add_rubric(aid, "add"), selected=1))

    assert sorted(backend.feedback_rows(sid, "add")) == [
        ("ANTHROPIC", False, "Handle the empty list.", Decimal("0.1")),
        ("OPENAI", True, "Check the empty case.", Decimal("0.3")),
    ]
    assert backend.target_state(sid, "add") == TargetState(
        True, None, "Selected by the configured selector.", Decimal("0.2"), None
    )
    # A completed target leaves the GRADE list.
    assert [t.identifier for t in backend.repo.decide_action(sid).targets] == ["sub"]


def test_save_feedback_rerun_replaces_selected_feedback(backend):
    aid, sid = started(backend)
    rubric = backend.add_rubric(aid, "add")
    backend.repo.save_feedback(sid, "add", result_for(rubric, selected=0))
    backend.repo.save_feedback(sid, "add", result_for(rubric))
    rows = backend.feedback_rows(sid, "add")
    assert len(rows) == 4
    assert [row for row in rows if row[1]] == [
        ("OPENAI", True, "Check the empty case.", Decimal("0.3")),
    ]


def test_save_feedback_opted_out_conflicts(backend):
    aid, sid = started(backend)
    backend.repo.set_ai_not_needed(sid, "add")
    with pytest.raises(ConflictError):
        backend.repo.save_feedback(sid, "add", result_for(backend.add_rubric(aid, "add")))


def test_save_feedback_rejects_a_rubric_not_for_this_target(backend):
    aid, sid = started(backend)
    with pytest.raises(ValueError):
        backend.repo.save_feedback(sid, "add", result_for(backend.add_rubric(aid, "sub")))
    with pytest.raises(ValueError):
        backend.repo.save_feedback(sid, "add", result_for(uuid.uuid4()))
    assert backend.feedback_rows(sid, "add") == []
    assert backend.target_state(sid, "add").completed is False


def test_save_feedback_unknown_target(backend):
    aid, sid = started(backend)
    with pytest.raises(UnknownTargetError):
        backend.repo.save_feedback(sid, "nope", result_for(backend.add_rubric(aid, "add")))


def test_feedback_result_validates_selection():
    response = ProviderFeedback("ANTHROPIC", "claude-opus-5-5", "ok")
    with pytest.raises(ValueError):
        FeedbackResult(uuid.uuid4(), (), 0)
    with pytest.raises(ValueError):
        FeedbackResult(uuid.uuid4(), (response,), 1)


def test_mark_target_failed_completes_with_error(backend):
    _, sid = started(backend)
    backend.repo.mark_target_failed(sid, "add", "retry_exhausted: provider timed out")
    state = backend.target_state(sid, "add")
    assert state.completed is True
    assert state.last_error == "retry_exhausted: provider timed out"
    assert [t.identifier for t in backend.repo.decide_action(sid).targets] == ["sub"]


@pytest.mark.parametrize("error", ["", "   "])
def test_mark_target_failed_needs_a_message(backend, error):
    _, sid = started(backend)
    with pytest.raises(ValueError):
        backend.repo.mark_target_failed(sid, "add", error)
    assert backend.target_state(sid, "add").completed is False


def test_mark_target_failed_after_feedback_marks_submission_failed(backend):
    aid, sid = started(backend)
    backend.repo.save_feedback(sid, "add", result_for(backend.add_rubric(aid, "add")))
    backend.repo.mark_target_failed(sid, "add", "late failure")
    assert backend.target_state(sid, "add").last_error == "late failure"
    assert [sub.submission_id for sub in backend.repo.list_submissions_failed_ai()] == [sid]
    assert backend.submission(sid)[1] is None
    assert all(not row[1] for row in backend.feedback_rows(sid, "add"))


def test_complete_requires_every_needed_target_to_succeed(backend):
    aid, sid = started(backend, targets=(("add", 1), ("sub", 2), ("mul", 3)))
    backend.repo.set_ai_not_needed(sid, "mul")
    backend.repo.save_feedback(sid, "add", result_for(backend.add_rubric(aid, "add")))
    assert backend.repo.mark_ai_complete_if_all_done(sid) is False
    assert backend.submission(sid)[1] is None

    backend.repo.mark_target_failed(sid, "sub", "invalid_response")
    assert backend.repo.mark_ai_complete_if_all_done(sid) is False
    assert backend.submission(sid)[1] is None
    assert backend.repo.decide_action(sid).action is Action.GRADE
    # Idempotent.
    assert backend.repo.mark_ai_complete_if_all_done(sid) is False


def test_complete_when_everything_opted_out(backend):
    _, sid = started(backend)
    backend.repo.set_ai_not_needed(sid, "add")
    backend.repo.set_ai_not_needed(sid, "sub")
    assert backend.repo.mark_ai_complete_if_all_done(sid) is True


def test_complete_unknown_submission(backend):
    with pytest.raises(NotFoundError):
        backend.repo.mark_ai_complete_if_all_done(uuid.uuid4())


def test_nightly_lifecycle_end_to_end(backend):
    """Submission run defers, the trigger authorizes, the rerun grades, a later rerun re-emits."""
    aid, sid = start(backend, mode="NIGHTLY")
    ref = SubmissionRef(aid, "sub-1", SUBMITTED)

    # Submission run.
    backend.repo.create_targets_default_needed(sid)
    backend.repo.set_ai_not_needed(sid, "sub")
    backend.repo.add_test_result(sid, "add", "FAIL", "add(2, 2) returned 5")
    assert backend.repo.decide_action(sid).action is Action.DEFER

    # The trigger authorizes before clicking rerun.
    backend.repo.authorize_for_grading(sid, "PENDING_NIGHTLY")

    # The rerun: the suite reports in again, then the AI phase grades.
    assert backend.repo.upsert_submission(ref) == sid
    backend.repo.create_targets_default_needed(sid)
    action = backend.repo.decide_action(sid)
    assert action.action is Action.GRADE
    assert [t.identifier for t in action.targets] == ["add"]
    backend.repo.save_feedback(sid, "add", result_for(backend.add_rubric(aid, "add")))
    assert backend.repo.mark_ai_complete_if_all_done(sid) is True

    # A later rerun, e.g. an instructor debugging, spends nothing.
    assert backend.repo.decide_action(sid).action is Action.REEMIT


# ---------------------------------------------------------------------
# Group 3: the read side
# ---------------------------------------------------------------------
def test_assignment_config_targets_rubrics_and_providers(backend):
    aid = backend.add_assignment("NIGHTLY", (("sub", 2), ("add", 1), ("Calc", None)))
    backend.configure_target(aid, "Calc", "CLASS", Decimal("10"))
    backend.add_rubric(aid, "add", version=1, criteria_text="old")
    current = backend.add_rubric(aid, "add", version=2, criteria_text="Handles overflow.", feedback_guidance="Be brief.")
    backend.add_provider("OPENAI", "gpt-6")
    backend.add_provider("GOOGLE", "gemini-3", is_enabled=False)
    backend.add_provider("ANTHROPIC", "claude-opus-5-5", is_primary=True)

    config = backend.repo.get_assignment_config(aid)

    assert (config.id, config.grading_mode, config.language, config.is_active) == (aid, "NIGHTLY", "PYTHON", True)
    assert [t.identifier for t in config.targets] == ["add", "sub", "Calc"]
    add, sub, calc = config.targets
    assert add.rubric.id == current
    assert (add.rubric.version, add.rubric.criteria_text, add.rubric.feedback_guidance) == (2, "Handles overflow.", "Be brief.")
    assert sub.rubric is None
    assert (calc.kind, calc.max_score) == ("CLASS", Decimal("10"))
    assert [(p.provider, p.is_primary) for p in config.providers] == [("ANTHROPIC", True), ("OPENAI", False)]


def test_assignment_config_unknown(backend):
    with pytest.raises(NotFoundError):
        backend.repo.get_assignment_config(uuid.uuid4())


def test_selected_feedback_for_reemit(backend):
    aid, sid = started(backend, targets=(("add", 1), ("sub", 2), ("mul", 3), ("div", 4)))
    backend.configure_target(aid, "add", "METHOD", Decimal("5"))
    backend.repo.save_feedback(sid, "add", result_for(backend.add_rubric(aid, "add"), selected=0))
    backend.repo.mark_target_failed(sid, "sub", "no single source file")
    backend.repo.set_ai_not_needed(sid, "mul")

    package = backend.repo.get_selected_feedback(sid)

    assert package.submission_id == sid
    # mul was opted out, so it is not in the package at all.
    assert [t.identifier for t in package.targets] == ["add", "sub", "div"]
    add, sub, div = package.targets
    assert add.max_score == Decimal("5")
    assert (add.feedback.provider, add.feedback.body, add.feedback.suggested_score) == (
        "ANTHROPIC", "Handle the empty list.", Decimal("0.1"),
    )
    assert add.error is None
    assert (sub.feedback, sub.error) == (None, "no single source file")
    # Not attempted yet: neither set.
    assert (div.feedback, div.error) == (None, None)


def test_selected_feedback_unknown_submission(backend):
    with pytest.raises(NotFoundError):
        backend.repo.get_selected_feedback(uuid.uuid4())


def test_pending_ai_lists_only_what_the_trigger_should_rerun(backend):
    def nightly(platform_id, minutes):
        aid = backend.add_assignment("NIGHTLY", (("add", 1),))
        sid = backend.repo.upsert_submission(
            SubmissionRef(aid, platform_id, SUBMITTED + timedelta(minutes=minutes), "s@example.edu")
        )
        backend.repo.sync_active_submissions(aid, [platform_id])
        backend.repo.create_targets_default_needed(sid)
        return aid, sid

    # Wanted, and listed oldest first.
    _, later = nightly("later", 30)
    _, earlier = nightly("earlier", 10)
    # Never wanted: opted out.
    _, opted = nightly("opted-out", 0)
    backend.repo.set_ai_not_needed(opted, "add")
    # Authorized failures belong to the AI-failure retry queue.
    _, failed = nightly("failed", 0)
    backend.repo.authorize_for_grading(failed, "PENDING_NIGHTLY")
    backend.repo.mark_target_failed(failed, "add", "invalid_response")
    # Superseded attempt (AD-14).
    _, superseded = nightly("superseded", 0)
    backend.set_submission(superseded, is_active_attempt=False)
    # Assignment switched off.
    inactive_aid, _ = nightly("inactive", 0)
    backend.set_assignment(inactive_aid, is_active=False)
    # IMMEDIATE is never swept up by the nightly run.
    start(backend, mode="IMMEDIATE", platform_id="immediate")

    found = backend.repo.list_submissions_pending_ai("NIGHTLY")

    assert [s.platform_submission_id for s in found] == ["earlier", "later"]
    assert found[0].submission_id == earlier
    assert found[0].platform == "GRADESCOPE"
    assert found[0].student_identifier == "s@example.edu"


def test_pending_ai_skips_graded_submissions(backend):
    aid, sid = started(backend, mode="NIGHTLY")
    assert [s.submission_id for s in backend.repo.list_submissions_pending_ai("NIGHTLY")] == [sid]
    backend.repo.authorize_for_grading(sid, "PENDING_NIGHTLY")
    backend.repo.mark_target_failed(sid, "add", "boom")
    backend.repo.mark_target_failed(sid, "sub", "boom")
    backend.repo.mark_ai_complete_if_all_done(sid)
    assert backend.repo.list_submissions_pending_ai("NIGHTLY") == []


def test_pending_ai_rejects_unknown_mode(backend):
    with pytest.raises(ValueError):
        backend.repo.list_submissions_pending_ai("WEEKLY")


def test_nightly_retry_uses_latest_audit_and_current_eligibility(sql_sessions):
    from fastapi.testclient import TestClient
    from web.app import main

    backend = SqlBackend(sql_sessions)
    aid = backend.add_assignment("NIGHTLY", (("add", 1),))
    cases = {
        "failed_trigger": ("RERUN_FAILED",),
        "authorization_failed": ("RERUN_FAILED",),
        "never_attempted": (),
        "authorized_only": ("AUTHORIZED",),
        "later_submitted": ("RERUN_FAILED", "RERUN_SUBMITTED"),
        "later_succeeded": ("RERUN_FAILED", "RERUN_SUCCEEDED"),
        "later_authorized": ("RERUN_FAILED", "AUTHORIZED"),
        "later_failed": ("RERUN_SUBMITTED", "RERUN_FAILED"),
        "unfinished_cycle": ("RERUN_FAILED",),
        "ai_succeeded": ("RERUN_FAILED",),
        "superseded": ("RERUN_FAILED",),
    }
    ids = {}
    for name, statuses in cases.items():
        sid = backend.repo.upsert_submission(SubmissionRef(aid, name, SUBMITTED))
        ids[name] = sid
        backend.repo.create_targets_default_needed(sid)
        for index, status in enumerate(statuses):
            with sql_sessions.begin() as session:
                started = SUBMITTED + timedelta(hours=index + 1)
                run = TriggerRun(
                    trigger="SCHEDULE", started_at=started,
                    status="RUNNING" if name == "unfinished_cycle" else "FAILED",
                    finished_at=None if name == "unfinished_cycle" else started + timedelta(minutes=1),
                )
                session.add(run)
                session.flush()
                session.add(TriggerRunSubmission(
                    trigger_run_id=run.id, submission_id=sid, status=status, recorded_at=started,
                ))
    backend.repo.sync_active_submissions(aid, [name for name in cases if name != "superseded"])
    backend.repo.authorize_for_grading(ids["failed_trigger"], "PENDING_NIGHTLY")
    authorized_at, _ = backend.submission(ids["failed_trigger"])
    backend.repo.set_submission_ai_status(ids["ai_succeeded"], "SUCCEEDED")

    with patch.object(main, "grading_repository", backend.repo), \
         patch.object(main, "SessionLocal", sql_sessions), \
         patch.object(main.settings, "integration_api_token", "test-token"), \
         TestClient(main.app) as client:
        path = "/internal/nightly/submissions?mode=NIGHTLY&retry_failed=true"
        assert client.get(path).status_code == 401
        response = client.get(path, headers={"X-Internal-Token": "test-token"})
        assert response.status_code == 200
        assert {row["platform_submission_id"] for row in response.json()} == {
            "failed_trigger", "authorization_failed", "later_failed",
        }
        # A recovered trigger still goes through normal nightly authorization.
        authorized = client.post(
            f"/internal/nightly/submissions/{ids['failed_trigger']}/authorize",
            json={"trigger_source": "PENDING_NIGHTLY"}, headers={"X-Internal-Token": "test-token"},
        )
        assert authorized.status_code == 200
        assert authorized.json()["authorization_granted"] is False
        assert backend.submission(ids["failed_trigger"])[0] == authorized_at
        assert client.post(
            f"/internal/nightly/submissions/{ids['authorization_failed']}/authorize",
            json={"trigger_source": "PENDING_NIGHTLY"}, headers={"X-Internal-Token": "test-token"},
        ).json()["authorization_granted"] is True


# ---------------------------------------------------------------------
# Group 4: instructor UI reads
# ---------------------------------------------------------------------
def test_list_assignments_counts_and_order(backend):
    zeta = backend.add_assignment("IMMEDIATE", (("add", 1), ("sub", 2)))
    backend.set_assignment(zeta, name="Zeta")
    alpha = backend.add_assignment("NIGHTLY", (("add", 1),))
    backend.set_assignment(alpha, name="Alpha")
    old = backend.add_assignment("IMMEDIATE", ())
    backend.set_assignment(old, name="Aardvark", is_active=False)
    unnamed = backend.add_assignment("IMMEDIATE", ())
    for platform_id in ("s1", "s2"):
        backend.repo.upsert_submission(SubmissionRef(zeta, platform_id, SUBMITTED))

    found = backend.repo.list_assignments()

    # Active first, then by name with unnamed last.
    assert [a.id for a in found] == [alpha, zeta, unnamed, old]
    by_id = {a.id: a for a in found}
    assert (by_id[zeta].target_count, by_id[zeta].submission_count) == (2, 2)
    assert (by_id[alpha].grading_mode, by_id[alpha].target_count, by_id[alpha].submission_count) == ("NIGHTLY", 1, 0)
    assert by_id[old].is_active is False


def test_list_assignments_empty(backend):
    assert backend.repo.list_assignments() == []


def test_list_submissions_newest_first_with_target_counts(backend):
    aid = backend.add_assignment("NIGHTLY", (("add", 1), ("sub", 2), ("mul", 3)))

    def submit(platform_id, hours):
        sid = backend.repo.upsert_submission(SubmissionRef(aid, platform_id, SUBMITTED + timedelta(hours=hours)))
        backend.set_submission(sid, is_active_attempt=True)
        backend.repo.create_targets_default_needed(sid)
        return sid

    waiting = submit("waiting", 0)
    mixed = submit("mixed", 2)
    backend.repo.authorize_for_grading(mixed, "PENDING_NIGHTLY")
    backend.repo.save_feedback(mixed, "add", result_for(backend.add_rubric(aid, "add")))
    backend.repo.mark_target_failed(mixed, "sub", "boom")
    backend.repo.set_ai_not_needed(mixed, "mul")
    backend.repo.mark_ai_complete_if_all_done(mixed)
    superseded = submit("superseded", 1)
    backend.set_submission(superseded, is_active_attempt=False)

    found = backend.repo.list_submissions(aid)

    assert [s.id for s in found] == [mixed, superseded, waiting]
    m, s, w = found
    assert (m.action, m.done, m.failed, m.waiting, m.opted_out) == (Action.GRADE, 1, 1, 0, 1)
    assert (w.action, w.done, w.failed, w.waiting, w.opted_out) == (Action.DEFER, 0, 0, 3, 0)
    assert s.is_active_attempt is False and w.is_active_attempt is True
    assert w.platform_submission_id == "waiting"


def test_list_submissions_unknown_assignment(backend):
    with pytest.raises(NotFoundError):
        backend.repo.list_submissions(uuid.uuid4())


def test_submission_detail_shows_every_target_state(backend):
    aid, sid = started(backend, targets=(("sub", 2), ("add", 1), ("mul", 3), ("div", 4)))
    backend.set_assignment(aid, name="Calc")
    backend.configure_target(aid, "add", "CLASS", Decimal("5"))
    backend.add_rubric(aid, "add", version=1)
    rubric = backend.add_rubric(aid, "add", version=2)
    backend.repo.add_test_result(sid, "add", "FAIL", "first")
    backend.repo.add_test_result(sid, "add", "PASS", "second")
    backend.repo.save_feedback(sid, "add", result_for(rubric, selected=1))
    backend.repo.mark_target_failed(sid, "sub", "no single source file")
    backend.repo.set_ai_not_needed(sid, "mul")

    detail = backend.repo.get_submission_detail(sid)

    assert (detail.id, detail.assignment_id, detail.assignment_name) == (sid, aid, "Calc")
    assert (detail.grading_mode, detail.platform_submission_id) == ("IMMEDIATE", "sub-1")
    assert detail.student_identifier == "student@example.edu"
    assert detail.action is Action.GRADE
    assert [t.identifier for t in detail.targets] == ["add", "sub", "mul", "div"]
    add, sub, mul, div = detail.targets
    assert [t.status for t in detail.targets] == [
        TargetStatus.DONE, TargetStatus.FAILED, TargetStatus.OPTED_OUT, TargetStatus.WAITING,
    ]
    assert (add.kind, add.max_score) == ("CLASS", Decimal("5"))
    assert [(r.status, r.functional_test_feedback) for r in add.test_results] == [("FAIL", "first"), ("PASS", "second")]
    # Every provider's response, the selected one first.
    assert [(f.provider, f.is_selected) for f in add.feedback] == [("OPENAI", True), ("ANTHROPIC", False)]
    selected = add.feedback[0]
    assert (selected.body, selected.suggested_score, selected.rubric_version) == ("Check the empty case.", Decimal("0.3"), 2)
    assert (selected.input_tokens, selected.output_tokens, selected.prompt_version) == (950, 140, "v1")
    assert (add.selection_reason, add.score_spread) == ("Selected by the configured selector.", Decimal("0.2"))
    assert add.completed_at is not None and add.last_error is None
    assert (sub.last_error, sub.feedback) == ("no single source file", ())
    assert sub.completed_at is not None
    assert (mul.completed_at, mul.test_results, mul.feedback) == (None, (), ())
    assert div.completed_at is None


def test_submission_detail_unknown(backend):
    with pytest.raises(NotFoundError):
        backend.repo.get_submission_detail(uuid.uuid4())


# ---------------------------------------------------------------------
# record_test_run
# ---------------------------------------------------------------------
def test_record_test_run_stores_results_and_opt_outs(backend):
    _, sid = started(backend, targets=(("add", 1), ("sub", 2), ("mul", 3)))

    added = backend.repo.record_test_run(sid, "run-1", [
        TargetTestOutcome("add", "FAIL", "test_add: expected 3"),
        TargetTestOutcome("sub", "PASS", None, ai_needed=False),
        TargetTestOutcome("missing", "PASS"),
    ])

    assert added == 2
    add, sub, mul = backend.repo.get_submission_detail(sid).targets
    assert [(r.status, r.functional_test_feedback) for r in add.test_results] == [("FAIL", "test_add: expected 3")]
    assert add.status is TargetStatus.WAITING
    assert [r.status for r in sub.test_results] == ["PASS"] and sub.status is TargetStatus.OPTED_OUT
    assert mul.test_results == ()


def test_record_test_run_ignores_a_repeated_run(backend):
    _, sid = started(backend)
    outcomes = [TargetTestOutcome("add", "FAIL", "first")]

    assert backend.repo.record_test_run(sid, "run-1", outcomes) == 1
    assert backend.repo.record_test_run(sid, "run-1", [TargetTestOutcome("add", "PASS", None, ai_needed=False)]) == 0
    assert backend.repo.record_test_run(sid, "run-2", [TargetTestOutcome("add", "PASS")]) == 1

    add = backend.repo.get_submission_detail(sid).targets[0]
    # The repeat changed nothing, not even the opt-out; the new run adds the latest result.
    assert [r.status for r in add.test_results] == ["FAIL", "PASS"]
    assert add.status is TargetStatus.WAITING


def test_record_test_run_can_opt_a_target_back_in(backend):
    _, sid = started(backend)
    backend.repo.record_test_run(sid, "run-1", [TargetTestOutcome("add", "PASS", None, ai_needed=False)])
    backend.repo.record_test_run(sid, "run-2", [TargetTestOutcome("add", "FAIL", "broke", ai_needed=True)])
    assert backend.repo.get_submission_detail(sid).targets[0].status is TargetStatus.WAITING


def test_record_test_run_rejects_bad_status(backend):
    _, sid = started(backend)
    with pytest.raises(ValueError, match="status must be one of"):
        backend.repo.record_test_run(sid, "run-1", [TargetTestOutcome("add", "BROKEN")])
    assert backend.repo.get_submission_detail(sid).targets[0].test_results == ()


def test_record_test_run_unknown_submission(backend):
    with pytest.raises(NotFoundError):
        backend.repo.record_test_run(uuid.uuid4(), "run-1", [])


# ---------------------------------------------------------------------
# create_review (FR-11)
# ---------------------------------------------------------------------
def rated_submission(backend):
    aid, sid = started(backend)
    rubric = backend.add_rubric(aid, "add")
    backend.repo.save_feedback(sid, "add", result_for(rubric, selected=1))
    selected, other = backend.repo.get_submission_detail(sid).targets[0].feedback
    return sid, selected, other


def test_feedback_has_no_review_until_rated(backend):
    _, selected, other = rated_submission(backend)
    assert selected.review is None and other.review is None


def test_create_review_shows_latest_on_its_response_only(backend):
    sid, selected, other = rated_submission(backend)

    returned = backend.repo.create_review(selected.id, rating=2, comment="  Too vague.  ", reviewer="ada@example.edu")
    backend.repo.create_review(selected.id, rating=4, comment="", reviewer="ada@example.edu")

    assert returned == sid
    first, second = backend.repo.get_submission_detail(sid).targets[0].feedback
    assert first.id == selected.id
    assert (first.review.rating, first.review.comment, first.review.reviewer) == (4, None, "ada@example.edu")
    assert first.review.reviewed_at is not None
    assert second.review is None


def test_create_review_keeps_comment_trimmed(backend):
    sid, selected, _ = rated_submission(backend)
    backend.repo.create_review(selected.id, rating=1, comment="  Wrong method.  ", reviewer=None)
    review = backend.repo.get_submission_detail(sid).targets[0].feedback[0].review
    assert (review.rating, review.comment, review.reviewer) == (1, "Wrong method.", None)


@pytest.mark.parametrize("rating", [0, 6, -1])
def test_create_review_rejects_out_of_range_rating(backend, rating):
    sid, selected, _ = rated_submission(backend)
    with pytest.raises(ValueError, match="1 to 5"):
        backend.repo.create_review(selected.id, rating=rating, comment=None, reviewer=None)
    assert backend.repo.get_submission_detail(sid).targets[0].feedback[0].review is None


def test_create_review_rejects_overlong_comment(backend):
    _, selected, _ = rated_submission(backend)
    with pytest.raises(ValueError, match="at most"):
        backend.repo.create_review(selected.id, rating=3, comment="x" * 4001, reviewer=None)


def test_create_review_unknown_feedback(backend):
    with pytest.raises(NotFoundError):
        backend.repo.create_review(uuid.uuid4(), rating=3, comment=None, reviewer=None)


def by_target(backend):
    """add tested for three students, one of them superseded; sub tested for one."""
    aid = backend.add_assignment("IMMEDIATE", (("sub", 2), ("add", 1), ("mul", 3)))

    def submit(platform_id, student, hours=0):
        sid = backend.repo.upsert_submission(
            SubmissionRef(aid, platform_id, SUBMITTED + timedelta(hours=hours), student)
        )
        backend.repo.create_targets_default_needed(sid)
        return sid

    bob = submit("bob-1", "bob@example.edu")
    backend.repo.add_test_result(bob, "add", "FAIL", "first")
    backend.repo.add_test_result(bob, "add", "PASS", "second")
    backend.repo.save_feedback(bob, "add", result_for(backend.add_rubric(aid, "add")))
    backend.repo.add_test_result(bob, "sub", "ERROR", None)
    backend.repo.mark_target_failed(bob, "sub", "boom")
    alice = submit("alice-1", "alice@example.edu")
    backend.repo.add_test_result(alice, "add", "FAIL", None)
    old = submit("alice-0", "alice@example.edu", hours=-1)
    backend.set_submission(old, is_active_attempt=False)
    backend.repo.add_test_result(old, "add", "PASS", None)
    nobody = submit("anon-1", None)
    backend.repo.add_test_result(nobody, "add", "SKIP", None)
    backend.repo.set_ai_not_needed(nobody, "add")
    targets = {t.identifier: t.id for t in backend.repo.get_assignment_config(aid).targets}
    return aid, targets, bob, alice


def test_target_summaries_count_active_tested_attempts(backend):
    aid, targets, _, _ = by_target(backend)

    found = backend.repo.list_target_summaries(aid)

    assert [t.identifier for t in found] == ["add", "sub", "mul"]
    add, sub, mul = found
    assert add.id == targets["add"]
    # The superseded attempt is left out; each untested target row is too.
    assert (add.done, add.failed, add.waiting, add.opted_out, add.submission_count) == (1, 0, 1, 1, 3)
    assert (sub.done, sub.failed, sub.waiting, sub.opted_out) == (0, 1, 0, 0)
    assert mul.submission_count == 0


def test_target_summaries_unknown_assignment(backend):
    with pytest.raises(NotFoundError):
        backend.repo.list_target_summaries(uuid.uuid4())


def test_target_submissions_by_student_with_latest_test(backend):
    aid, targets, bob, alice = by_target(backend)

    found = backend.repo.get_target_submissions(aid, targets["add"])

    assert (found.assignment_id, found.id, found.identifier, found.kind) == (aid, targets["add"], "add", "METHOD")
    assert [s.platform_submission_id for s in found.submissions] == ["alice-1", "bob-1", "anon-1"]
    a, b, anon = found.submissions
    assert (a.submission_id, a.status, a.latest_test.status, a.suggested_score) == (
        alice, TargetStatus.WAITING, "FAIL", None,
    )
    # bob's later test result wins, and the selected response's score is shown.
    assert (b.submission_id, b.status, b.latest_test.functional_test_feedback) == (bob, TargetStatus.DONE, "second")
    assert b.suggested_score == Decimal("0.3")
    assert (anon.student_identifier, anon.status) == (None, TargetStatus.OPTED_OUT)
    assert backend.repo.get_target_submissions(aid, targets["mul"]).submissions == ()


def test_target_submissions_target_must_belong_to_the_assignment(backend):
    aid, targets, _, _ = by_target(backend)
    other = backend.add_assignment("IMMEDIATE", (("add", 1),))
    for assignment_id, target_id in ((other, targets["add"]), (aid, uuid.uuid4()), (uuid.uuid4(), targets["add"])):
        with pytest.raises(NotFoundError):
            backend.repo.get_target_submissions(assignment_id, target_id)


def scored(rubric_id, score):
    return FeedbackResult(
        rubric_id=rubric_id,
        responses=(ProviderFeedback("GOOGLE", "gemini-2.5-flash", "Fine.", "v1", score),),
        selected_index=0,
    )


def test_results_flag_failing_tests_and_low_scores(backend):
    aid = backend.add_assignment("IMMEDIATE", (("sub", 2), ("add", 1)))
    backend.configure_target(aid, "add", "METHOD", Decimal("1"))
    rubric = backend.add_rubric(aid, "add")

    def submit(platform_id, student, hours=0):
        sid = backend.repo.upsert_submission(
            SubmissionRef(aid, platform_id, SUBMITTED + timedelta(hours=hours), student)
        )
        backend.repo.create_targets_default_needed(sid)
        return sid

    bob = submit("bob-1", "bob@example.edu")
    backend.repo.add_test_result(bob, "add", "FAIL", None)
    backend.repo.add_test_result(bob, "add", "PASS", None)
    backend.repo.save_feedback(bob, "add", scored(rubric, 0.5))
    backend.repo.add_test_result(bob, "sub", "ERROR", None)
    backend.repo.mark_target_failed(bob, "sub", "boom")
    carol = submit("carol-1", "carol@example.edu")
    backend.repo.add_test_result(carol, "add", "PASS", None)
    backend.repo.save_feedback(carol, "add", scored(rubric, 1))
    backend.repo.add_test_result(carol, "sub", "SKIP", None)
    alice = submit("alice-1", "alice@example.edu")
    backend.repo.add_test_result(alice, "add", "FAIL", None)
    old = submit("alice-0", "alice@example.edu", hours=-1)
    backend.set_submission(old, is_active_attempt=False)
    submit("anon-1", None)
    # Registered after everyone submitted, so nobody has a row for it.
    backend.add_target(aid, "mul")

    found = backend.repo.list_results(aid)

    assert (found.assignment_id, found.grading_mode) == (aid, "IMMEDIATE")
    assert [c.identifier for c in found.columns] == ["add", "sub", "mul"]
    assert found.columns[0].max_score == Decimal("1")
    # Active attempts only, by student, unknown last.
    assert [r.platform_submission_id for r in found.rows] == ["alice-1", "bob-1", "carol-1", "anon-1"]
    a, b, c, anon = found.rows

    assert a.submission_id == alice
    assert (a.cells[0].test_status, a.cells[0].status, a.cells[0].deduction) == ("FAIL", TargetStatus.WAITING, True)
    assert a.cells[2] is None

    # The latest test passed, but the AI score is below max.
    assert (b.cells[0].test_status, b.cells[0].suggested_score, b.cells[0].deduction) == ("PASS", Decimal("0.5"), True)
    # ERROR counts; sub has no max score, so only the test can flag it.
    assert (b.cells[1].test_status, b.cells[1].status, b.cells[1].deduction) == ("ERROR", TargetStatus.FAILED, True)
    assert b.deduction_count == 2
    # The selected feedback text and the failure reason, for the cell dropdowns.
    assert (b.cells[0].feedback, b.cells[0].last_error) == ("Fine.", None)
    assert (b.cells[1].feedback, b.cells[1].last_error) == (None, "boom")
    assert (a.cells[0].feedback, a.cells[0].last_error) == (None, None)

    assert c.submission_id == carol
    assert (c.cells[0].suggested_score, c.cells[0].deduction) == (Decimal("1"), False)
    assert (c.cells[1].test_status, c.cells[1].deduction) == ("SKIP", False)
    assert not c.has_deduction

    assert (anon.student_identifier, anon.cells[0].test_status, anon.has_deduction) == (None, None, False)


def test_results_unknown_assignment(backend):
    with pytest.raises(NotFoundError):
        backend.repo.list_results(uuid.uuid4())


@pytest.mark.parametrize("test_status, score, max_score, expected", [
    ("FAIL", None, None, True),
    ("ERROR", Decimal("5"), Decimal("5"), True),
    ("SKIP", None, Decimal("5"), False),
    ("PASS", Decimal("4.5"), Decimal("5"), True),
    ("PASS", Decimal("5"), Decimal("5"), False),
    (None, Decimal("2"), None, False),
    (None, None, None, False),
])
def test_is_deduction(test_status, score, max_score, expected):
    assert is_deduction(test_status, score, max_score) is expected


def test_assignment_context_notes_and_files_in_config(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    assert backend.repo.get_assignment_config(aid).context_notes is None
    assert backend.repo.get_assignment_config(aid).context_files == ()

    backend.repo.set_context_notes(aid, "  Use recursion.  ")
    first, second = backend.repo.add_context_files(aid, [("spec.md", "Add."), ("starter.py", "def add(): ...")])

    config = backend.repo.get_assignment_config(aid)
    assert config.context_notes == "Use recursion."
    assert [(f.id, f.filename, f.content) for f in config.context_files] == [
        (first, "spec.md", "Add."), (second, "starter.py", "def add(): ..."),
    ]

    backend.repo.delete_context_file(aid, first)
    backend.repo.set_context_notes(aid, "   ")
    config = backend.repo.get_assignment_config(aid)
    assert config.context_notes is None
    assert [f.id for f in config.context_files] == [second]


def test_assignment_context_limits_are_all_or_none(backend):
    from app.assignment_context import MAX_TOTAL_CHARS

    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    backend.repo.add_context_files(aid, [("a.txt", "x" * (MAX_TOTAL_CHARS - 5))])

    with pytest.raises(ValueError):
        backend.repo.add_context_files(aid, [("b.txt", "ok"), ("c.txt", "too much")])
    with pytest.raises(ValueError):
        backend.repo.set_context_notes(aid, "123456")

    config = backend.repo.get_assignment_config(aid)
    assert [f.filename for f in config.context_files] == ["a.txt"]
    assert config.context_notes is None


def test_assignment_context_belongs_to_one_assignment(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    other = backend.add_assignment("IMMEDIATE", (("add", 1),))
    (file_id,) = backend.repo.add_context_files(aid, [("spec.md", "Add.")])

    assert backend.repo.get_assignment_config(other).context_files == ()
    for assignment_id, fid in ((other, file_id), (aid, uuid.uuid4())):
        with pytest.raises(NotFoundError):
            backend.repo.delete_context_file(assignment_id, fid)
    for call in (lambda: backend.repo.set_context_notes(uuid.uuid4(), "x"),
                 lambda: backend.repo.add_context_files(uuid.uuid4(), [("a", "b")])):
        with pytest.raises(NotFoundError):
            call()


# ---------------------------------------------------------------------
# Group 4: instructor configuration writes
# ---------------------------------------------------------------------
def new_assignment(backend, platform_id="12345", **fields):
    values = dict(
        course_name="CS 0401", term="Fall 2026", platform_assignment_id=platform_id,
        name="Project 1", grading_mode="NIGHTLY", language="java",
    )
    values.update(fields)
    return backend.repo.create_assignment(**values)


def test_create_assignment(backend):
    aid = new_assignment(backend, name="  Project 1  ")

    config = backend.repo.get_assignment_config(aid)

    assert (config.platform, config.platform_assignment_id, config.name) == ("GRADESCOPE", "12345", "Project 1")
    # Language is stored upper-cased, as the AI phase's mapping is keyed.
    assert (config.grading_mode, config.language, config.is_active, config.targets) == ("NIGHTLY", "JAVA", True, ())
    assert [a.id for a in backend.repo.list_assignments()] == [aid]


def test_create_assignment_reuses_a_course_by_name_and_term(backend):
    first = new_assignment(backend, "1")
    same = new_assignment(backend, "2")
    other_term = new_assignment(backend, "3", term="Spring 2027")
    no_term = new_assignment(backend, "4", term="  ")
    no_term_again = new_assignment(backend, "5", term=None)

    courses = {aid: backend.course_of(aid) for aid in (first, same, other_term, no_term, no_term_again)}

    assert courses[first] == courses[same]
    assert courses[first] != courses[other_term]
    assert courses[no_term] == courses[no_term_again]
    assert courses[no_term][1:] == ("CS 0401", None)


def test_create_assignment_duplicate_platform_id_conflicts(backend):
    new_assignment(backend)
    with pytest.raises(ConflictError):
        new_assignment(backend, " 12345 ", name="Another")


@pytest.mark.parametrize(
    "fields",
    [
        {"course_name": " "},
        {"platform_assignment_id": ""},
        {"grading_mode": "WEEKLY"},
        {"language": ""},
    ],
)
def test_create_assignment_rejects_bad_fields(backend, fields):
    with pytest.raises(ValueError):
        new_assignment(backend, **fields)
    assert backend.repo.list_assignments() == []


def test_create_target_appends_in_display_order(backend):
    aid = backend.add_assignment("IMMEDIATE", (("first", 4), ("loose", None)))

    tid = backend.repo.create_target(aid, identifier=" edu.pitt.Calc#add ", kind="METHOD", max_score=Decimal("2.5"))
    backend.repo.create_target(aid, identifier="Calc", kind="CLASS", max_score=None)

    targets = backend.repo.get_assignment_config(aid).targets
    assert [t.identifier for t in targets] == ["first", "edu.pitt.Calc#add", "Calc", "loose"]
    added = targets[1]
    assert (added.id, added.kind, added.max_score, added.rubric) == (tid, "METHOD", Decimal("2.5"), None)


def test_created_target_is_registered_for_the_contract(backend):
    aid = backend.add_assignment("IMMEDIATE", ())
    backend.repo.create_target(aid, identifier="add", kind="METHOD", max_score=None)
    sid = backend.repo.upsert_submission(SubmissionRef(aid, "sub-1", SUBMITTED))

    refs = backend.repo.create_targets_default_needed(sid)

    assert [r.identifier for r in refs] == ["add"]


def test_create_target_duplicate_conflicts(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    with pytest.raises(ConflictError):
        backend.repo.create_target(aid, identifier="add ", kind="CLASS", max_score=None)


def test_create_target_unknown_assignment(backend):
    with pytest.raises(NotFoundError):
        backend.repo.create_target(uuid.uuid4(), identifier="add", kind="METHOD", max_score=None)


@pytest.mark.parametrize(
    "identifier, kind, max_score",
    [(" ", "METHOD", None), ("add", "FUNCTION", None), ("add", "METHOD", Decimal("-1"))],
)
def test_create_target_rejects_bad_fields(backend, identifier, kind, max_score):
    aid = backend.add_assignment("IMMEDIATE", ())
    with pytest.raises(ValueError):
        backend.repo.create_target(aid, identifier=identifier, kind=kind, max_score=max_score)
    assert backend.repo.get_assignment_config(aid).targets == ()


def test_delete_target_removes_it_and_its_rubrics(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1), ("divide", 2)))
    tid = backend.repo.get_assignment_config(aid).targets[0].id
    backend.repo.upsert_rubric(
        tid, criteria_text="Returns the sum.", feedback_guidance=None,
        analysis_config={"source_globs": ["calculator.py"]},
    )
    backend.repo.upsert_rubric(tid, criteria_text="Returns the sum of two ints.", feedback_guidance=None)

    backend.repo.delete_target(aid, tid)

    assert [t.identifier for t in backend.repo.get_assignment_config(aid).targets] == ["divide"]
    # The identifier is free to register again.
    backend.repo.create_target(aid, identifier="add", kind="METHOD", max_score=None)


def test_delete_target_with_submissions_conflicts(backend):
    aid, _ = started(backend, targets=(("add", 1),))
    tid = backend.repo.get_assignment_config(aid).targets[0].id

    with pytest.raises(ConflictError):
        backend.repo.delete_target(aid, tid)
    assert [t.identifier for t in backend.repo.get_assignment_config(aid).targets] == ["add"]


def test_delete_target_must_belong_to_the_assignment(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    other = backend.add_assignment("IMMEDIATE", ())
    tid = backend.repo.get_assignment_config(aid).targets[0].id

    with pytest.raises(NotFoundError):
        backend.repo.delete_target(other, tid)
    with pytest.raises(NotFoundError):
        backend.repo.delete_target(aid, uuid.uuid4())
    assert len(backend.repo.get_assignment_config(aid).targets) == 1


def test_upsert_rubric_versions_rather_than_overwrites(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    tid = backend.repo.get_assignment_config(aid).targets[0].id

    first = backend.repo.upsert_rubric(
        tid, criteria_text=" Returns the sum. ", feedback_guidance="  ",
        analysis_config={"source_globs": ["calculator.py"]},
    )
    second = backend.repo.upsert_rubric(tid, criteria_text="Returns the sum.", feedback_guidance="Be brief.")

    assert (first.version, first.criteria_text, first.feedback_guidance) == (1, "Returns the sum.", None)
    assert (second.version, second.feedback_guidance) == (2, "Be brief.")
    assert second.id != first.id
    assert backend.repo.get_assignment_config(aid).targets[0].rubric == second


def test_upsert_rubric_keeps_feedback_pointing_at_its_version(backend):
    aid, sid = started(backend, targets=(("add", 1),))
    tid = backend.repo.get_assignment_config(aid).targets[0].id
    v1 = backend.repo.upsert_rubric(
        tid, criteria_text="old", feedback_guidance=None,
        analysis_config={"source_globs": ["calculator.py"]},
    )
    backend.repo.save_feedback(sid, "add", result_for(v1.id, selected=0))

    backend.repo.upsert_rubric(tid, criteria_text="new", feedback_guidance=None)

    feedback = backend.repo.get_submission_detail(sid).targets[0].feedback
    assert {f.rubric_version for f in feedback} == {1}


def test_upsert_rubric_unchanged_text_adds_no_version(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    tid = backend.repo.get_assignment_config(aid).targets[0].id
    first = backend.repo.upsert_rubric(
        tid, criteria_text="Returns the sum.", feedback_guidance=None,
        analysis_config={"source_globs": ["calculator.py"]},
    )

    again = backend.repo.upsert_rubric(tid, criteria_text="Returns the sum.\n", feedback_guidance="")

    assert again == first


def test_upsert_rubric_continues_after_seeded_versions(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    backend.add_rubric(aid, "add", version=3, criteria_text="seeded")
    tid = backend.repo.get_assignment_config(aid).targets[0].id

    assert backend.repo.upsert_rubric(
        tid, criteria_text="edited", feedback_guidance=None,
        analysis_config={"source_globs": ["calculator.py"]},
    ).version == 4


def test_upsert_rubric_needs_criteria(backend):
    aid = backend.add_assignment("IMMEDIATE", (("add", 1),))
    tid = backend.repo.get_assignment_config(aid).targets[0].id
    with pytest.raises(ValueError):
        backend.repo.upsert_rubric(tid, criteria_text="  ", feedback_guidance="Be brief.")
    assert backend.repo.get_assignment_config(aid).targets[0].rubric is None


def test_upsert_rubric_unknown_target(backend):
    with pytest.raises(NotFoundError):
        backend.repo.upsert_rubric(uuid.uuid4(), criteria_text="x", feedback_guidance=None)

"""Contract tests for IGradingRepository, run against both implementations.

The SQL run needs a Postgres database and is skipped without one. With
the web app's docker compose running:

    docker compose exec db createdb -U autograder autograder_test

Override the location with TEST_DATABASE_URL.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models import Assignment, Base, Course, GradingTarget, Submission
from app.repository import (
    Action,
    InMemoryRepository,
    NotFoundError,
    SqlGradingRepository,
    SubmissionRef,
    UnknownTargetError,
    derive_action,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://autograder:autograder@localhost:5432/autograder_test",
)

SUBMITTED = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------
# Backends. Each seeds instructor config and pokes the columns that the
# not-yet-written methods (authorize_for_grading, mark_ai_complete...) set.
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
    return SqlBackend(request.getfixturevalue("sql_sessions"))


def start(backend, mode="IMMEDIATE", targets=(("add", 1), ("sub", 2)), platform_id="sub-1"):
    """The reporting library's first call: upsert, then create targets."""
    aid = backend.add_assignment(mode, targets)
    sid = backend.repo.upsert_submission(SubmissionRef(aid, platform_id, SUBMITTED, "student@example.edu"))
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

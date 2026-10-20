"""Tests for the Gradescope CSV importer.

register_assignment writes SQL directly, so its tests need the Postgres
test database described in test_repository and are skipped without one.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.import_gradescope import import_submissions, read_submissions, register_assignment
from app.models import Assignment, Base, Course
from app.repository import InMemoryRepository
from test_repository import sql_sessions  # noqa: F401 - pytest fixture

# The header and rows as Gradescope's Download Grades writes them.
CSV = """\
Name,SID,Email,Total Score,Max Points,Status,Submission ID,Submission Time,Lateness (H:M:S),View Count,Submission Count,1: Autograder (110.0 pts)
Student 1,"",student1@example.edu,110.0,110.0,Graded,439103359,2026-10-20 07:22:13 -0400,00:00:00,0,1,110.0
Student 2,"",student2@example.edu,85.0,110.0,Graded,439103420,2026-10-20 07:24:39 -0400,00:00:00,0,1,85.0
Student 3,"",student3@example.edu,,110.0,Missing,,,,,,
"""


@pytest.fixture
def csv_path(tmp_path):
    path = tmp_path / "Exercise_4_scores.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def test_reads_submitted_rows_and_counts_missing(csv_path):
    rows, skipped = read_submissions(csv_path)

    assert skipped == 1
    assert rows == [
        ("439103359", datetime(2026, 10, 20, 7, 22, 13, tzinfo=timezone(timedelta(hours=-4))),
         "student1@example.edu"),
        ("439103420", datetime(2026, 10, 20, 7, 24, 39, tzinfo=timezone(timedelta(hours=-4))),
         "student2@example.edu"),
    ]


def test_bad_time_is_rejected(csv_path):
    csv_path.write_text(CSV.replace("2026-10-20 07:24:39 -0400", "yesterday"), encoding="utf-8")

    with pytest.raises(ValueError):
        read_submissions(csv_path)


def test_reimport_does_not_duplicate_submissions(csv_path):
    repo = InMemoryRepository()
    assignment_id = repo.add_assignment("IMMEDIATE", platform_assignment_id="8904668")
    rows, _ = read_submissions(csv_path)

    import_submissions(repo, assignment_id, rows)
    import_submissions(repo, assignment_id, rows)

    subs = repo.list_submissions(assignment_id)
    assert sorted(s.platform_submission_id for s in subs) == ["439103359", "439103420"]


@pytest.fixture
def sessions(sql_sessions):  # noqa: F811
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with sql_sessions.begin() as s:
        s.execute(text(f"TRUNCATE {tables} CASCADE"))
    return sql_sessions


def register(sessions, platform_assignment_id="8904668"):
    return register_assignment(
        sessions,
        platform_assignment_id=platform_assignment_id,
        course_name="CS 1632",
        term="Fall 2026",
        name="Exercise 4",
        grading_mode="IMMEDIATE",
        language="typescript",
    )


def test_register_creates_then_reuses_assignment(sessions):
    first, created = register(sessions)
    again, created_again = register(sessions)

    assert (created, created_again) == (True, False)
    assert again == first
    with sessions() as s:
        a = s.get(Assignment, first)
        assert (a.platform_assignment_id, a.name, a.language) == ("8904668", "Exercise 4", "TYPESCRIPT")


def test_register_shares_course_between_assignments(sessions):
    register(sessions, "1")
    register(sessions, "2")

    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(Course)) == 1


def test_register_rejects_unknown_mode(sessions):
    with pytest.raises(ValueError):
        register_assignment(
            sessions, platform_assignment_id="1", course_name="C", term=None,
            name=None, grading_mode="WEEKLY", language="JAVA",
        )

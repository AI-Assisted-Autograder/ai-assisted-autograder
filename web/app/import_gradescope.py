"""Import an assignment's submissions from a Gradescope "Download Grades" CSV.

    docker compose exec web python -m app.import_gradescope scores.csv \\
        --assignment-id 8904668 --course "CS 1632" --name "Exercise 4" --language TYPESCRIPT

The assignment id is the number after /assignments/ in the Gradescope URL.
The assignment is registered on first import and reused after that, so the
same CSV can be imported again: submissions already present are left as
they are. Students with no submission (blank Submission ID) are skipped.

Only the submission rows are imported. Targets and rubrics are configured
separately, and scores and test results come from the autograder.
"""

import argparse
import csv
import sys
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import GRADING_MODES, Assignment, Course
from app.repository import IGradingRepository, SubmissionRef

# Gradescope writes times like "2026-10-20 07:22:13 -0400".
TIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"

# (platform_submission_id, submitted_at, student_identifier)
SubmissionRow = tuple[str, datetime, str | None]


def read_submissions(csv_path: Path) -> tuple[list[SubmissionRow], int]:
    """Return a row per submission, and how many students had none."""
    rows, skipped = [], 0
    # utf-8-sig: the file may start with a byte order mark if it was saved from Excel.
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            submission_id = (row.get("Submission ID") or "").strip()
            if not submission_id:
                skipped += 1
                continue
            submitted_at = datetime.strptime(row["Submission Time"].strip(), TIME_FORMAT)
            # Email is the one identifier every row has; SID is often blank.
            student = (row.get("Email") or "").strip() or (row.get("Name") or "").strip() or None
            rows.append((submission_id, submitted_at, student))
    return rows, skipped


def register_assignment(
    sessions: sessionmaker[Session],
    *,
    platform_assignment_id: str,
    course_name: str,
    term: str | None,
    name: str | None,
    grading_mode: str,
    language: str,
) -> tuple[uuid.UUID, bool]:
    """Return the assignment's id, and whether it was created now.

    Inserted directly, as app.seed does, since main has no instructor UI
    for creating assignments yet. The course is matched by name and term.
    """
    if grading_mode not in GRADING_MODES:
        raise ValueError(f"grading mode must be one of {', '.join(GRADING_MODES)}, not {grading_mode!r}")
    with sessions.begin() as s:
        existing = s.scalar(
            select(Assignment.id).where(
                Assignment.platform == "GRADESCOPE",
                Assignment.platform_assignment_id == platform_assignment_id,
            )
        )
        if existing is not None:
            return existing, False
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
        assignment = Assignment(
            course_id=course_id,
            platform="GRADESCOPE",
            platform_assignment_id=platform_assignment_id,
            name=name,
            grading_mode=grading_mode,
            language=language.upper(),
        )
        s.add(assignment)
        s.flush()
        return assignment.id, True


def import_submissions(repo: IGradingRepository, assignment_id: uuid.UUID, rows: list[SubmissionRow]) -> None:
    for submission_id, submitted_at, student in rows:
        repo.upsert_submission(SubmissionRef(
            assignment_id=assignment_id,
            platform_submission_id=submission_id,
            submitted_at=submitted_at,
            student_identifier=student,
        ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path, help="the CSV from Gradescope's Download Grades")
    parser.add_argument("--assignment-id", required=True,
                        help="Gradescope assignment id, from the assignment's URL")
    parser.add_argument("--course", required=True, help="course name, used only if the assignment is new")
    parser.add_argument("--term", help="course term, e.g. 'Fall 2026'")
    parser.add_argument("--name", help="assignment name; defaults to the CSV's file name")
    parser.add_argument("--mode", default="IMMEDIATE", choices=GRADING_MODES)
    parser.add_argument("--language", required=True, help="e.g. JAVA, PYTHON, TYPESCRIPT")
    args = parser.parse_args(argv)

    # Imported here so --help works without a database configured.
    from app.db import SessionLocal
    from app.repository import SqlGradingRepository

    # Read the whole file first, so a malformed row stops the import before anything is written.
    rows, skipped = read_submissions(args.csv)
    assignment_id, created = register_assignment(
        SessionLocal,
        platform_assignment_id=args.assignment_id,
        course_name=args.course,
        term=args.term,
        name=args.name or args.csv.stem.removesuffix("_scores").replace("_", " "),
        grading_mode=args.mode,
        language=args.language,
    )
    import_submissions(SqlGradingRepository(SessionLocal), assignment_id, rows)

    print(f"{'Registered' if created else 'Found'} assignment {args.assignment_id} ({assignment_id})")
    print(f"Imported {len(rows)} submissions (any already present were left unchanged); "
          f"skipped {skipped} students with no submission.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

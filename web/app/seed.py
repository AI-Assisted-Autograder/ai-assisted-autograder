"""Fill the database with sample data to develop and test against.

    docker compose exec web python -m app.seed           # add sample data
    docker compose exec web python -m app.seed --reset   # wipe every table first

Instructor configuration (course, assignments, targets, rubrics, providers)
is inserted directly, since the instructor UI does not exist yet. Everything
a submission produces goes through SqlGradingRepository, so the rows are in
the states real grading runs leave them in.
"""

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import (
    Assignment,
    Base,
    Course,
    GradingTarget,
    LlmProviderConfig,
    Rubric,
)
from app.repository import (
    FeedbackResult,
    ProviderFeedback,
    SqlGradingRepository,
    SubmissionRef,
)

COURSE_NAME = "CS 0401 Sample Course"

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def reset() -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with SessionLocal.begin() as s:
        s.execute(text(f"TRUNCATE {tables} CASCADE"))


def already_seeded() -> bool:
    with SessionLocal() as s:
        return s.scalar(select(Course.id).where(Course.name == COURSE_NAME)) is not None


def add_config() -> dict[str, uuid.UUID]:
    """Course, two assignments, their targets and rubrics, and providers.

    Returns the assignment ids and rubric ids by name.
    """
    ids: dict[str, uuid.UUID] = {}
    with SessionLocal.begin() as s:
        course = Course(name=COURSE_NAME, term="Fall 2026")
        s.add(course)
        s.flush()

        assignments = {
            "calc": Assignment(
                course_id=course.id,
                platform="GRADESCOPE",
                platform_assignment_id="sample-calc",
                grading_mode="IMMEDIATE",
                language="PYTHON",
                name="Project 1: Calculator",
            ),
            "list": Assignment(
                course_id=course.id,
                platform="GRADESCOPE",
                platform_assignment_id="sample-linked-list",
                grading_mode="NIGHTLY",
                language="JAVA",
                name="Project 2: Linked List",
            ),
            "old": Assignment(
                course_id=course.id,
                platform="GRADESCOPE",
                platform_assignment_id="sample-archived",
                grading_mode="IMMEDIATE",
                language="PYTHON",
                name="Project 0: Hello World (inactive)",
                is_active=False,
            ),
        }
        s.add_all(assignments.values())
        s.flush()
        for key, a in assignments.items():
            ids[key] = a.id

        targets = [
            # (assignment, identifier, kind, max_score, criteria, guidance)
            ("calc", "add", "METHOD", 5,
             "Returns the sum of two numbers. Handles negatives and floats.",
             "Point out missed edge cases; do not give the answer."),
            ("calc", "divide", "METHOD", 5,
             "Returns a / b. Raises ValueError when b is zero.",
             "Mention the zero case explicitly if it is mishandled."),
            ("calc", "Calculator", "CLASS", 10,
             "Clear method names, no duplicated logic, docstrings on public methods.",
             "Focus on readability; keep feedback under 120 words."),
            ("list", "LinkedList.add", "METHOD", 10,
             "Appends to the tail in O(1) using a tail pointer.",
             "Comment on complexity if the student walks the list."),
            ("list", "LinkedList.remove", "METHOD", 10,
             "Removes the first match, updating head and tail correctly.",
             "Check the head, tail and single-element cases."),
            ("old", "main", "METHOD", 1, "Prints Hello, World!", None),
        ]
        order: dict[str, int] = {}
        for assignment, identifier, kind, max_score, criteria, guidance in targets:
            order[assignment] = order.get(assignment, 0) + 1
            gt = GradingTarget(
                assignment_id=ids[assignment],
                identifier=identifier,
                kind=kind,
                max_score=max_score,
                display_order=order[assignment],
            )
            s.add(gt)
            s.flush()
            if identifier == "add" and assignment == "calc":
                # An older rubric, to show versioning: only the current one is used.
                s.add(Rubric(grading_target_id=gt.id, version=1,
                             criteria_text="Returns the sum.", is_current=False))
                version = 2
            else:
                version = 1
            rubric = Rubric(
                grading_target_id=gt.id,
                version=version,
                criteria_text=criteria,
                feedback_guidance=guidance,
                is_current=True,
            )
            s.add(rubric)
            s.flush()
            ids[f"rubric:{assignment}:{identifier}"] = rubric.id

        s.add_all([
            LlmProviderConfig(provider="GOOGLE", model_name="gemini-2.5-flash", is_primary=True),
            LlmProviderConfig(provider="ANTHROPIC", model_name="claude-sonnet-5-5"),
            LlmProviderConfig(provider="OPENAI", model_name="gpt-5", is_enabled=False),
        ])
    return ids


def feedback(rubric_id: uuid.UUID, body: str, score: float, other_score: float) -> FeedbackResult:
    """Two providers' responses, the primary (Gemini) selected."""
    return FeedbackResult(
        rubric_id=rubric_id,
        responses=(
            ProviderFeedback(
                provider="GOOGLE", model_name="gemini-2.5-flash", body=body,
                prompt_version="v1", suggested_score=score, confidence=0.85,
                input_tokens=1200, output_tokens=180,
            ),
            ProviderFeedback(
                provider="ANTHROPIC", model_name="claude-sonnet-5-5",
                body=f"(Alternative) {body}", prompt_version="v1",
                suggested_score=other_score, confidence=0.8,
                input_tokens=1250, output_tokens=210,
            ),
        ),
        selected_index=0,
        selection_reason="primary provider",
        score_spread=abs(score - other_score),
        agreement_score=0.9,
    )


def report(repo, assignment_id, platform_id, student, hours_ago, tests):
    """Phase 1 of a run: register the submission, its targets and test results."""
    sid = repo.upsert_submission(SubmissionRef(
        assignment_id=assignment_id,
        platform_submission_id=platform_id,
        submitted_at=ago(hours_ago),
        student_identifier=student,
    ))
    repo.create_targets_default_needed(sid)
    for target, status, message in tests:
        repo.add_test_result(sid, target, status, message)
    return sid


def add_submissions(ids: dict[str, uuid.UUID]) -> list[tuple[str, str]]:
    repo = SqlGradingRepository(SessionLocal)
    calc, lst = ids["calc"], ids["list"]
    r = lambda a, t: ids[f"rubric:{a}:{t}"]  # noqa: E731
    made: list[tuple[str, str]] = []

    # --- IMMEDIATE: Calculator ---------------------------------------
    # Fully graded: decide_action returns REEMIT.
    sid = report(repo, calc, "sample-calc-sub-1", "alice@example.edu", 30, [
        ("add", "PASS", None),
        ("divide", "FAIL", "test_divide_by_zero: expected ValueError, got ZeroDivisionError"),
        ("Calculator", "PASS", None),
    ])
    repo.save_feedback(sid, "add", feedback(
        r("calc", "add"), "Correct for all inputs, including floats.", 5, 5))
    repo.save_feedback(sid, "divide", feedback(
        r("calc", "divide"),
        "Division works, but dividing by zero lets Python's ZeroDivisionError escape. "
        "The spec asks for a ValueError: check b before dividing.", 3, 2.5))
    repo.save_feedback(sid, "Calculator", feedback(
        r("calc", "Calculator"), "Readable and well named. Add docstrings to the public methods.", 8, 9))
    repo.mark_ai_complete_if_all_done(sid)
    made.append(("sample-calc-sub-1", "graded, REEMIT"))

    # One target opted out, one failed, one graded: complete.
    sid = report(repo, calc, "sample-calc-sub-2", "bob@example.edu", 20, [
        ("add", "PASS", None),
        ("divide", "PASS", None),
        ("Calculator", "ERROR", "ImportError: No module named 'calculator'"),
    ])
    repo.set_ai_not_needed(sid, "Calculator")
    repo.save_feedback(sid, "add", feedback(r("calc", "add"), "Correct and concise.", 5, 5))
    repo.mark_target_failed(sid, "divide", "GOOGLE: 429 Resource exhausted; ANTHROPIC: timeout after 60s")
    repo.mark_ai_complete_if_all_done(sid)
    made.append(("sample-calc-sub-2", "graded with one opt-out and one failure, REEMIT"))

    # Reported, not graded yet: GRADE.
    report(repo, calc, "sample-calc-sub-3", "carol@example.edu", 0.5, [
        ("add", "FAIL", "test_add_negative: expected -1, got 1"),
        ("divide", "PASS", None),
        ("Calculator", "PASS", None),
    ])
    made.append(("sample-calc-sub-3", "pending, GRADE"))

    # --- NIGHTLY: Linked List ----------------------------------------
    # Waiting for tonight's run: DEFER, and listed by list_submissions_pending_ai.
    report(repo, lst, "sample-list-sub-1", "alice@example.edu", 6, [
        ("LinkedList.add", "PASS", None),
        ("LinkedList.remove", "FAIL", "testRemoveTail: tail still points at removed node"),
    ])
    made.append(("sample-list-sub-1", "pending, DEFER (nightly queue)"))

    # Authorized by the trigger, rerun not finished yet: GRADE.
    authorized_sid = report(repo, lst, "sample-list-sub-2", "dave@example.edu", 4, [
        ("LinkedList.add", "PASS", None),
        ("LinkedList.remove", "PASS", None),
    ])
    made.append(("sample-list-sub-2", "authorized, GRADE (nightly queue)"))

    # Graded last night: REEMIT.
    sid = report(repo, lst, "sample-list-sub-3", "erin@example.edu", 28, [
        ("LinkedList.add", "PASS", None),
        ("LinkedList.remove", "PASS", None),
    ])
    repo.sync_active_submissions(calc, [f"sample-calc-sub-{index}" for index in (1, 2, 3)])
    repo.sync_active_submissions(lst, [f"sample-list-sub-{index}" for index in (1, 2, 3)])
    repo.authorize_for_grading(authorized_sid, "PENDING_NIGHTLY")
    repo.authorize_for_grading(sid, "PENDING_NIGHTLY")
    repo.save_feedback(sid, "LinkedList.add", feedback(
        r("list", "LinkedList.add"), "O(1) append using the tail pointer. Nicely done.", 10, 10))
    repo.save_feedback(sid, "LinkedList.remove", feedback(
        r("list", "LinkedList.remove"),
        "Handles head and tail. The single-element case works but is easy to miss: "
        "a comment would help readers.", 9, 8))
    repo.mark_ai_complete_if_all_done(sid)
    made.append(("sample-list-sub-3", "graded, REEMIT"))

    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true",
                        help="delete ALL rows in every table before seeding")
    args = parser.parse_args(argv)

    if args.reset:
        reset()
        print("Emptied every table.")
    elif already_seeded():
        print("Sample data is already loaded. Use --reset to wipe and reload it.")
        return 0

    ids = add_config()
    made = add_submissions(ids)

    print("Loaded sample data:")
    print("  1 course, 3 assignments (IMMEDIATE, NIGHTLY, inactive), 6 targets, 3 providers")
    for platform_id, state in made:
        print(f"  {platform_id:<20} {state}")
    print("Assignment ids:")
    for key in ("calc", "list", "old"):
        print(f"  {key:<5} {ids[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

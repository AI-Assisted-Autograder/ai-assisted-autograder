"""Instructor UI pages, rendered against an InMemoryRepository."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.auth import InstructorAccount, require_instructor
from app.main import app
from app.repository import FeedbackResult, InMemoryRepository, ProviderFeedback, SubmissionRef
from app.ui import get_repository

SUBMITTED = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
SIGNED_IN = InstructorAccount(uuid.uuid4(), "ada@example.edu", "Ada", "unused", True)


@pytest.fixture
def repo():
    repo = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repo
    # Sign-in has its own tests (test_auth.py); here every request is signed in.
    app.dependency_overrides[require_instructor] = lambda: SIGNED_IN
    yield repo
    app.dependency_overrides.clear()


@pytest.fixture
def client(repo):
    return TestClient(app)


def graded_submission(repo):
    """An assignment with one submission: one target graded, one failed, one opted out."""
    aid = repo.add_assignment("IMMEDIATE", platform_assignment_id="calc", name="Project 1: Calculator")
    add = repo.add_grading_target(aid, "add", 1, max_score=Decimal("5"))
    repo.add_grading_target(aid, "divide", 2)
    repo.add_grading_target(aid, "Calculator", 3, kind="CLASS")
    rubric = repo.add_rubric(add, criteria_text="Returns the sum.", feedback_guidance="Be brief.")
    repo.add_provider("GOOGLE", "gemini-2.5-flash", is_primary=True)

    sid = repo.upsert_submission(SubmissionRef(aid, "sub-1", SUBMITTED, "alice@example.edu"))
    repo.create_targets_default_needed(sid)
    repo.add_test_result(sid, "add", "FAIL", "test_add_negative: expected -1")
    repo.save_feedback(sid, "add", FeedbackResult(
        rubric_id=rubric,
        responses=(
            ProviderFeedback("GOOGLE", "gemini-2.5-flash", "Handle <negative> numbers.", suggested_score=3),
            ProviderFeedback("ANTHROPIC", "claude-sonnet-5-5", "Check the sign.", suggested_score=2.5),
        ),
        selected_index=0,
    ))
    repo.mark_target_failed(sid, "divide", "429 Resource exhausted")
    repo.set_ai_not_needed(sid, "Calculator")
    repo.mark_ai_complete_if_all_done(sid)
    return aid, sid


def test_assignments_empty_suggests_seeding(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "app.seed" in response.text


def test_assignments_lists_and_links(client, repo):
    aid, _ = graded_submission(repo)
    old = repo.add_assignment("NIGHTLY", platform_assignment_id="old", name="Archived", is_active=False)

    response = client.get("/")

    assert response.status_code == 200
    assert f'href="/assignments/{aid}"' in response.text
    assert "Project 1: Calculator" in response.text
    assert f'href="/assignments/{old}"' in response.text
    assert "Inactive" in response.text


def test_assignment_page_shows_submissions_targets_and_rubric(client, repo):
    aid, sid = graded_submission(repo)

    response = client.get(f"/assignments/{aid}")

    assert response.status_code == 200
    text = response.text
    assert f'href="/submissions/{sid}"' in text
    assert "alice@example.edu" in text
    assert "Not graded yet" in text
    assert '<th>AI result</th>' in text
    assert '<span class="badge failed">AI processing failed</span>' in text
    assert "Returns the sum." in text and "Be brief." in text
    # divide has no rubric.
    assert "No rubric yet" in text
    assert "LLM providers" not in text
    assert "gemini-2.5-flash" not in text


def test_assignment_page_collapses_long_rubric_text(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    tid = repo.add_grading_target(aid, "add", 1)
    long_criteria = "Check every step. " * 40
    repo.add_rubric(tid, criteria_text=long_criteria, feedback_guidance="Be brief.")

    text = client.get(f"/assignments/{aid}").text

    assert '<details class="long-text">' in text
    assert f"Show all ({len(long_criteria)} characters)" in text
    assert long_criteria in text
    # Short text is still shown in full, not collapsed.
    assert '<p class="prose">Be brief.</p>' in text


def test_delete_target(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    tid = repo.add_grading_target(aid, "add", 1)
    repo.add_grading_target(aid, "divide", 2)
    sid = repo.upsert_submission(SubmissionRef(aid, "sub-1", SUBMITTED, None))
    repo.create_targets_default_needed(sid)

    assert f'action="/assignments/{aid}/targets/{tid}/delete"' in client.get(f"/assignments/{aid}").text
    response = client.post(f"/assignments/{aid}/targets/{tid}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/assignments/{aid}#targets"
    assert [t.identifier for t in repo.get_assignment_config(aid).targets] == ["divide"]
    assert [t.identifier for t in repo.get_submission_detail(sid).targets] == ["divide"]


def test_delete_target_with_grading_history_is_409(client, repo):
    aid, _ = graded_submission(repo)
    tid = repo.get_assignment_config(aid).targets[0].id

    response = client.post(f"/assignments/{aid}/targets/{tid}/delete")

    assert response.status_code == 409
    assert "has grading history" in response.text
    assert len(repo.get_assignment_config(aid).targets) == 3


def test_delete_target_under_another_assignment_is_404(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    tid = repo.add_grading_target(aid, "add", 1)
    other = repo.add_assignment("IMMEDIATE", platform_assignment_id="other", name="Other")

    assert client.post(f"/assignments/{other}/targets/{tid}/delete").status_code == 404
    assert len(repo.get_assignment_config(aid).targets) == 1


def test_submission_page_shows_each_target_state(client, repo):
    _, sid = graded_submission(repo)

    response = client.get(f"/submissions/{sid}")

    assert response.status_code == 200
    text = response.text
    assert "test_add_negative: expected -1" in text
    assert "Sent to student" in text
    # Both providers, side by side, with LLM output escaped.
    assert "Handle &lt;negative&gt; numbers." in text
    assert "Check the sign." in text
    assert "3 / 5" in text
    assert "429 Resource exhausted" in text
    assert "opted this target out" in text


def test_nightly_submission_waiting_for_its_run(client, repo):
    aid = repo.add_assignment("NIGHTLY", name="Linked List")
    repo.add_grading_target(aid, "LinkedList.add", 1)
    sid = repo.upsert_submission(SubmissionRef(aid, "sub-1", SUBMITTED, "dave@example.edu"))
    repo.create_targets_default_needed(sid)

    response = client.get(f"/submissions/{sid}")

    assert response.status_code == 200
    assert "Waiting for nightly run" in response.text
    assert "The next nightly run will grade it." in response.text


def first_feedback_id(repo, sid):
    return repo.get_submission_detail(sid).targets[0].feedback[0].id


def test_submission_page_offers_rating_form(client, repo):
    _, sid = graded_submission(repo)
    fid = first_feedback_id(repo, sid)

    text = client.get(f"/submissions/{sid}").text

    assert f'action="/feedback/{fid}/review"' in text
    assert f'id="feedback-{fid}"' in text
    assert text.count("Rate this feedback") == 2


def test_rate_feedback_records_reviewer_and_redirects(client, repo):
    _, sid = graded_submission(repo)
    fid = first_feedback_id(repo, sid)

    response = client.post(f"/feedback/{fid}/review", data={
        "rating": "4", "comment": "Good, but <too> long.", "submission_id": str(sid),
    }, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/submissions/{sid}#feedback-{fid}"
    review = repo.get_submission_detail(sid).targets[0].feedback[0].review
    assert (review.rating, review.comment, review.reviewer) == (4, "Good, but <too> long.", "ada@example.edu")
    text = client.get(f"/submissions/{sid}").text
    assert "Rated 4 out of 5" in text
    assert "Good, but &lt;too&gt; long." in text
    assert "Change rating" in text and "Rate this feedback" in text
    assert 'value="4" required checked' in text


@pytest.mark.parametrize("rating", ["", "0", "6", "abc", "2.5"])
def test_rate_feedback_rejects_bad_rating(client, repo, rating):
    _, sid = graded_submission(repo)
    fid = first_feedback_id(repo, sid)

    response = client.post(f"/feedback/{fid}/review", data={"rating": rating, "submission_id": str(sid)})

    assert response.status_code == 400
    assert "Rating must be a whole number from 1 to 5" in response.text
    assert repo.get_submission_detail(sid).targets[0].feedback[0].review is None


def test_rate_unknown_feedback(client, repo):
    response = client.post(f"/feedback/{uuid.uuid4()}/review", data={"rating": "3"})
    assert response.status_code == 404


def test_assignment_by_target_view_links_each_target(client, repo):
    aid, _ = graded_submission(repo)
    targets = {t.identifier: t.id for t in repo.get_assignment_config(aid).targets}

    by_student = client.get(f"/assignments/{aid}").text
    response = client.get(f"/assignments/{aid}?view=targets")

    assert 'href="/assignments/{}/targets/{}"'.format(aid, targets["add"]) not in by_student
    assert response.status_code == 200
    text = response.text
    for target_id in targets.values():
        assert f'href="/assignments/{aid}/targets/{target_id}"' in text
    # Only add has a test result, so only it counts alice.
    assert text.count("1 feedback") == 1
    assert "alice@example.edu" not in text


def test_target_page_lists_students_with_test_results(client, repo):
    aid, sid = graded_submission(repo)
    targets = {t.identifier: t.id for t in repo.get_assignment_config(aid).targets}

    response = client.get(f"/assignments/{aid}/targets/{targets['add']}")

    assert response.status_code == 200
    text = response.text
    assert f'href="/submissions/{sid}"' in text and "alice@example.edu" in text
    assert "FAIL" in text and "Feedback given" in text
    assert "3 / 5" in text
    assert f'href="/assignments/{aid}?view=targets"' in text

    untested = client.get(f"/assignments/{aid}/targets/{targets['divide']}").text
    assert "alice@example.edu" not in untested
    assert "No test results recorded" in untested


def test_target_page_target_must_belong_to_the_assignment(client, repo):
    aid, _ = graded_submission(repo)
    other = repo.add_assignment("IMMEDIATE", platform_assignment_id="other")
    target_id = repo.get_assignment_config(aid).targets[0].id

    assert client.get(f"/assignments/{other}/targets/{target_id}").status_code == 404
    assert client.get(f"/assignments/{aid}/targets/{uuid.uuid4()}").status_code == 404


@pytest.mark.parametrize("path", ["/assignments/{}", "/submissions/{}"])
def test_unknown_ids_are_404(client, path):
    response = client.get(path.format(uuid.uuid4()))
    assert response.status_code == 404
    assert "does not exist" in response.text


# ---------------------------------------------------------------------
# Instructor configuration forms
# ---------------------------------------------------------------------
ASSIGNMENT_FORM = {
    "course_name": "CS 0401",
    "term": "Fall 2026",
    "platform_assignment_id": "12345",
    "name": "Project 1: Calculator",
    "ai_mode": "GRADING",
    "grading_mode": "IMMEDIATE",
    "language": "PYTHON",
}


def test_new_assignment_form_renders(client):
    response = client.get("/assignments/new")
    assert response.status_code == 200
    assert 'action="/assignments"' in response.text
    assert 'value="NIGHTLY"' in response.text and 'value="JAVA"' in response.text


def test_create_assignment_redirects_to_configuration(client, repo):
    response = client.post("/assignments", data=ASSIGNMENT_FORM, follow_redirects=False)

    assert response.status_code == 303
    (aid,) = repo.assignments
    assert response.headers["location"] == f"/assignments/{aid}/targets/new"
    assert client.get(response.headers["location"]).status_code == 200


def test_create_assignment_error_keeps_what_was_typed(client, repo):
    client.post("/assignments", data=ASSIGNMENT_FORM)

    response = client.post("/assignments", data={**ASSIGNMENT_FORM, "name": "Typed <again>"})

    assert response.status_code == 409
    assert "already registered" in response.text
    assert 'value="Typed &lt;again&gt;"' in response.text
    assert len(repo.assignments) == 1


def test_create_assignment_missing_field_is_400(client, repo):
    response = client.post("/assignments", data={**ASSIGNMENT_FORM, "course_name": ""})
    assert response.status_code == 400
    assert "Course is required" in response.text
    assert repo.assignments == {}


def test_add_target_redirects_to_configuration(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")

    response = client.post(
        f"/assignments/{aid}/targets",
        data={"identifier": "add", "kind": "METHOD", "behavior": "ai_grading"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    (target,) = repo.get_assignment_config(aid).targets
    assert response.headers["location"] == f"/assignments/{aid}/targets/{target.id}/rubric"
    assert (target.identifier, target.kind, target.max_score) == ("add", "METHOD", None)
    assert target.analysis_config["behavior"] == "ai_grading"


@pytest.mark.parametrize(
    "data, status, message",
    [
        ({"identifier": "existing", "kind": "METHOD", "behavior": "ai_grading"}, 409, "already registered"),
    ],
)
def test_add_target_errors_reshow_the_page(client, repo, data, status, message):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    repo.add_grading_target(aid, "existing", 1)

    response = client.post(f"/assignments/{aid}/targets", data=data)

    assert response.status_code == status
    assert message in response.text
    assert f'value="{data["identifier"]}"' in response.text
    assert len(repo.get_assignment_config(aid).targets) == 1


def test_add_target_unknown_assignment(client):
    response = client.post(f"/assignments/{uuid.uuid4()}/targets", data={"identifier": "add", "kind": "METHOD"})
    assert response.status_code == 404


def test_rubric_form_prefills_current_rubric(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    tid = repo.add_grading_target(aid, "add", 1)
    repo.add_rubric(tid, version=2, criteria_text="Returns the sum.", feedback_guidance="Be brief.")

    response = client.get(f"/assignments/{aid}/targets/{tid}/rubric")

    assert response.status_code == 200
    assert "Returns the sum.</textarea>" in response.text
    assert "Be brief.</textarea>" in response.text
    assert "Common background version 2" in response.text


def test_save_rubric_creates_a_version(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    tid = repo.add_grading_target(aid, "add", 1, analysis_config={"source_globs": ["calculator.py"]})

    response = client.post(
        f"/assignments/{aid}/targets/{tid}/rubric",
        data={
            "criteria_text": "Calculator assignment.", "feedback_guidance": "",
            "behavior": "ai_grading", "source_globs": "calculator.py",
            "item_id": "sum", "item_requirement": "Returns the sum.", "item_points": "5",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    target = repo.get_assignment_config(aid).targets[0]
    rubric = target.rubric
    assert (rubric.version, rubric.criteria_text, rubric.feedback_guidance) == (1, "Calculator assignment.", None)
    assert target.max_score == Decimal("5")
    assert target.analysis_config["grading_items"] == (
        {"id": "sum", "requirement": "Returns the sum.", "points": 5.0},
    )


@pytest.mark.parametrize("points", ["five", "NaN"])
def test_save_rubric_invalid_item_points_reshows_the_form(client, repo, points):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    tid = repo.add_grading_target(aid, "add", 1)

    response = client.post(
        f"/assignments/{aid}/targets/{tid}/rubric",
        data={
            "criteria_text": "Calculator assignment.",
            "behavior": "ai_grading", "source_globs": "calculator.py",
            "item_id": "sum", "item_requirement": "Returns the sum.", "item_points": points,
        },
    )

    assert response.status_code == 400
    assert "Item points must be a number" in response.text
    assert f'value="{points}"' in response.text
    assert "Returns the sum.</textarea>" in response.text
    target = repo.get_assignment_config(aid).targets[0]
    assert target.rubric is None
    assert target.max_score is None
    assert target.analysis_config == {}


def test_save_rubric_without_criteria_is_400(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    tid = repo.add_grading_target(aid, "add", 1)

    response = client.post(
        f"/assignments/{aid}/targets/{tid}/rubric", data={"criteria_text": " ", "feedback_guidance": "Kept"}
    )

    assert response.status_code == 400
    assert "Criteria is required" in response.text
    assert "Kept</textarea>" in response.text
    assert repo.get_assignment_config(aid).targets[0].rubric is None


def test_rubric_target_must_belong_to_the_assignment(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    other = repo.add_assignment("IMMEDIATE", name="Other")
    tid = repo.add_grading_target(other, "add", 1)

    assert client.get(f"/assignments/{aid}/targets/{tid}/rubric").status_code == 404
    response = client.post(f"/assignments/{aid}/targets/{tid}/rubric", data={"criteria_text": "x"})
    assert response.status_code == 404
    assert repo.get_assignment_config(other).targets[0].rubric is None


def test_results_dashboard_flags_deductions(client, repo):
    aid, sid = graded_submission(repo)
    clean = repo.upsert_submission(SubmissionRef(aid, "sub-2", SUBMITTED, "bob@example.edu"))
    repo.create_targets_default_needed(clean)
    repo.add_test_result(clean, "add", "PASS", None)

    response = client.get(f"/assignments/{aid}/results")

    assert response.status_code == 200
    text = response.text
    assert f'href="/submissions/{sid}"' in text
    assert f'href="/submissions/{clean}"' in text
    # alice's add failed its test and scored 3 of 5.
    assert '<td class="cell deduction">' in text
    assert "3 / 5" in text
    assert '<span class="badge failed">AI failed</span>' in text
    assert "Opted out" in text
    # Each cell with feedback has a dropdown showing the text sent to the student, escaped.
    assert text.count('<details class="cell-feedback">') == 2
    assert "<summary>Feedback</summary>" in text
    assert "Handle &lt;negative&gt; numbers." in text
    # The failed target shows why instead.
    assert "<summary>Why it failed</summary>" in text and "429 Resource exhausted" in text


def test_results_dashboard_filters_to_deductions(client, repo):
    aid, sid = graded_submission(repo)
    clean = repo.upsert_submission(SubmissionRef(aid, "sub-2", SUBMITTED, "bob@example.edu"))
    repo.create_targets_default_needed(clean)
    repo.add_test_result(clean, "add", "PASS", None)

    text = client.get(f"/assignments/{aid}/results?filter=has_deduction").text

    assert f'href="/submissions/{sid}"' in text
    assert f'href="/submissions/{clean}"' not in text
    assert 'filter=has_deduction" aria-current="page"' in text


def test_results_dashboard_empty_and_unknown(client, repo):
    aid = repo.add_assignment("NIGHTLY", platform_assignment_id="x", name="Empty")
    repo.add_grading_target(aid, "add", 1)

    assert "No submissions yet." in client.get(f"/assignments/{aid}/results").text
    assert client.get(f"/assignments/{uuid.uuid4()}/results").status_code == 404


def test_assignment_page_links_to_results(client, repo):
    aid, _ = graded_submission(repo)
    assert f'href="/assignments/{aid}/results"' in client.get(f"/assignments/{aid}").text


def test_create_assignment_with_context_notes_and_files(client, repo):
    response = client.post("/assignments", data={
        **ASSIGNMENT_FORM, "platform_assignment_id": "777", "context_notes": "Students have not learned exceptions yet.",
    }, files=[
        ("context_files", ("spec.md", b"Implement add.", "text/markdown")),
        ("context_files", ("starter.py", b"def add(a, b): ...", "text/x-python")),
    ], follow_redirects=False)

    assert response.status_code == 303
    (aid,) = repo.assignments
    config = repo.get_assignment_config(aid)
    assert config.context_notes == "Students have not learned exceptions yet."
    assert [f.filename for f in config.context_files] == ["spec.md", "starter.py"]
    page = client.get(f"/assignments/{aid}").text
    assert "spec.md" in page
    assert "Students have not learned exceptions yet." in page


def test_create_assignment_with_no_files_chosen(client, repo):
    # A browser sends an empty, nameless part for an untouched file input.
    response = client.post("/assignments", data={
        **ASSIGNMENT_FORM, "platform_assignment_id": "778",
    }, files=[("context_files", ("", b"", "application/octet-stream"))], follow_redirects=False)

    assert response.status_code == 303
    (aid,) = repo.assignments
    assert repo.get_assignment_config(aid).context_files == ()


def test_create_assignment_with_unreadable_file_creates_nothing(client, repo):
    response = client.post("/assignments", data={
        **ASSIGNMENT_FORM, "platform_assignment_id": "779", "context_notes": "keep me",
    }, files=[("context_files", ("report.docx", b"PK\x03\x04\xff\xfe", "application/octet-stream"))])

    assert response.status_code == 400
    assert "Save Word documents as PDF first" in response.text
    assert "keep me" in response.text
    assert repo.list_assignments() == []


def test_assignment_context_upload_notes_and_remove(client, repo):
    aid, _ = graded_submission(repo)

    response = client.post(f"/assignments/{aid}/context/files",
                           files=[("context_files", ("handout.txt", b"Add two numbers.", "text/plain"))],
                           follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, f"/assignments/{aid}#context")
    (f,) = repo.get_assignment_config(aid).context_files
    assert (f.filename, f.content) == ("handout.txt", "Add two numbers.")

    client.post(f"/assignments/{aid}/context/notes", data={"context_notes": "Be kind."})
    assert repo.get_assignment_config(aid).context_notes == "Be kind."

    page = client.get(f"/assignments/{aid}").text
    assert f'action="/assignments/{aid}/context/files/{f.id}/delete"' in page

    client.post(f"/assignments/{aid}/context/files/{f.id}/delete")
    assert repo.get_assignment_config(aid).context_files == ()
    assert client.post(f"/assignments/{aid}/context/files/{f.id}/delete").status_code == 404


def test_assignment_context_errors_reshow_the_page(client, repo):
    from app.assignment_context import MAX_TOTAL_CHARS

    aid, _ = graded_submission(repo)

    response = client.post(f"/assignments/{aid}/context/files",
                           files=[("context_files", ("", b"", "application/octet-stream"))])
    assert response.status_code == 400
    assert "Choose at least one file" in response.text

    response = client.post(f"/assignments/{aid}/context/notes", data={"context_notes": "x" * (MAX_TOTAL_CHARS + 1)})
    assert response.status_code == 400
    assert "over the" in response.text
    assert repo.get_assignment_config(aid).context_notes is None

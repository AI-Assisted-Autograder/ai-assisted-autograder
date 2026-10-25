"""Instructor UI pages, rendered against an InMemoryRepository."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repository import FeedbackResult, InMemoryRepository, ProviderFeedback, SubmissionRef
from app.ui import get_repository

SUBMITTED = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    repo = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repo
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
    assert "Graded" in text
    assert "1 feedback" in text and "1 failed" in text and "1 opted out" in text
    assert "Returns the sum." in text and "Be brief." in text
    # divide has no rubric.
    assert "No rubric yet" in text
    assert "gemini-2.5-flash" in text


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
    "grading_mode": "IMMEDIATE",
    "language": "PYTHON",
}


def test_new_assignment_form_renders(client):
    response = client.get("/assignments/new")
    assert response.status_code == 200
    assert 'action="/assignments"' in response.text
    assert 'value="NIGHTLY"' in response.text and 'value="JAVA"' in response.text


def test_create_assignment_redirects_to_it(client, repo):
    response = client.post("/assignments", data=ASSIGNMENT_FORM, follow_redirects=False)

    assert response.status_code == 303
    (aid,) = repo.assignments
    assert response.headers["location"] == f"/assignments/{aid}"
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


def test_add_target_redirects_back(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")

    response = client.post(
        f"/assignments/{aid}/targets",
        data={"identifier": "add", "kind": "METHOD", "max_score": "5"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/assignments/{aid}#targets"
    (target,) = repo.get_assignment_config(aid).targets
    assert (target.identifier, target.kind, target.max_score) == ("add", "METHOD", Decimal("5"))


@pytest.mark.parametrize(
    "data, status, message",
    [
        ({"identifier": "add", "kind": "METHOD", "max_score": "five"}, 400, "Max score must be a number"),
        ({"identifier": "add", "kind": "METHOD", "max_score": "NaN"}, 400, "Max score must be a number"),
        ({"identifier": "existing", "kind": "METHOD", "max_score": ""}, 409, "already registered"),
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
    assert "creates version 3" in response.text


def test_save_rubric_creates_a_version(client, repo):
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    tid = repo.add_grading_target(aid, "add", 1)

    response = client.post(
        f"/assignments/{aid}/targets/{tid}/rubric",
        data={"criteria_text": "Returns the sum.", "feedback_guidance": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    rubric = repo.get_assignment_config(aid).targets[0].rubric
    assert (rubric.version, rubric.criteria_text, rubric.feedback_guidance) == (1, "Returns the sum.", None)


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

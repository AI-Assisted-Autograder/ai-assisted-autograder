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

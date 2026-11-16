"""Mapping one run's Gradescope rows to targets, and POST /v1/submissions/{id}/test-results."""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.feedback as feedback
from app.feedback_store import StoreError
from app.repository import InMemoryRepository, TargetConfig, TargetStatus
from app.run_results import MESSAGE_LIMIT, map_outcomes, target_outcome

RESULTS = "results/results.json"


def target(identifier="add", **analysis):
    return TargetConfig(uuid.uuid4(), identifier, "METHOD", None, None, analysis)


def source(*include, path=RESULTS, fmt="gradescope"):
    return {"format": fmt, "path": path, "label": "Tests", "include": list(include)}


def row(name, status="passed", output="", **extra):
    return {"name": name, "status": status, "output": output, **extra}


# ---------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------
def test_include_patterns_pick_the_targets_rows():
    tests = [row("add: positive"), row("add: negative", "failed", "expected -1"), row("sub: basic", "failed")]

    add = target_outcome(target("add", evidence_sources=[source(r"^add:")]), tests)
    sub = target_outcome(target("sub", evidence_sources=[source(r"^sub:")]), tests)

    assert (add.status, add.message) == ("FAIL", "add: negative: expected -1")
    assert (sub.status, sub.message) == ("FAIL", "sub: basic")
    # ai_grading and legacy targets are never opted in or out here.
    assert add.ai_needed is None


def test_a_source_without_include_takes_every_row():
    out = target_outcome(target(evidence_sources=[source()]), [row("a"), row("b")])
    assert (out.status, out.message) == ("PASS", None)


def test_sources_for_other_files_map_nothing():
    t = target(evidence_sources=[source(path="results/other.json")])
    assert target_outcome(t, [row("a", "failed")]).status is None
    assert map_outcomes([t], [row("a", "failed")]) == []


def test_expected_tests_select_by_exact_name_and_flag_missing_ones():
    t = target(expected_tests=["test_add", "test_add_negative"])

    assert target_outcome(t, [row("test_add"), row("test_add_negative")]).status == "PASS"
    missing = target_outcome(t, [row("test_add"), row("test_add_negative_extra")])
    assert (missing.status, missing.message) == ("ERROR", "Missing from the run: test_add_negative")


def test_failure_beats_missing_and_unknown_rows_are_errors():
    t = target(expected_tests=["a", "b", "c"])
    assert target_outcome(t, [row("a", "failed"), row("b", "weird")]).status == "FAIL"
    assert target_outcome(t, [row("a"), row("b", "weird"), row("c")]).status == "ERROR"


def test_score_decides_when_a_row_has_no_status():
    t = target(evidence_sources=[source()])
    assert target_outcome(t, [{"name": "a", "score": 2, "max_score": 2}]).status == "PASS"
    assert target_outcome(t, [{"name": "a", "score": 1.5, "max_score": 2}]).status == "FAIL"
    assert target_outcome(t, [{"name": "a"}]).status == "ERROR"


def test_functional_target_opts_out_without_a_visible_failure():
    t = target(behavior="functional_test_feedback", evidence_sources=[source()])

    passing = target_outcome(t, [row("a"), row("b")])
    hidden_fail = target_outcome(t, [row("a", "failed", visibility="hidden")])
    hidden_by_default = target_outcome(t, [row("a", "failed")], visibility="after_published")
    visible_fail = target_outcome(t, [row("a", "failed", visibility="visible")], visibility="hidden")

    assert (passing.status, passing.ai_needed) == ("PASS", False)
    # Hidden failures still count as failures for the instructor, but get no AI feedback.
    assert (hidden_fail.status, hidden_fail.ai_needed) == ("FAIL", False)
    assert hidden_by_default.ai_needed is False
    assert (visible_fail.status, visible_fail.ai_needed) == ("FAIL", True)


def test_long_messages_are_truncated():
    out = target_outcome(target(evidence_sources=[source()]), [row(f"t{i}", "failed", "x" * 900) for i in range(10)])
    assert len(out.message) == MESSAGE_LIMIT and out.message.endswith("[truncated]")


def test_targets_with_nothing_to_record_are_left_out():
    found = map_outcomes([target("a"), target("b", expected_tests=["t"])], [row("t")])
    assert [(o.target, o.status) for o in found] == [("b", "PASS")]


# ---------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------
TOKEN = "assignment-token-0123456789"
SUBMITTED = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)


class FakeStore:
    """Stands in for JsonStore: one token for one assignment."""

    assignment_id = None

    def __init__(self, path):
        self.config = SimpleNamespace(max_concurrency=1, timeout_seconds=5, max_body_bytes=65_536)

    def authenticate(self, raw_token):
        if raw_token != TOKEN:
            raise StoreError("unauthorized", 401)
        return str(FakeStore.assignment_id)

    def allow_pitt_models(self):
        pass

    def close(self):
        pass


@pytest.fixture
def api(monkeypatch, tmp_path):
    repo = InMemoryRepository()
    aid = repo.add_assignment("IMMEDIATE", name="Calc")
    repo.add_grading_target(aid, "add", 1, max_score=Decimal("5"),
                            analysis_config={"behavior": "ai_grading", "evidence_sources": [source(r"^add")]})
    repo.add_grading_target(aid, "feedback", 2,
                            analysis_config={"behavior": "functional_test_feedback", "evidence_sources": [source()]})
    FakeStore.assignment_id = aid
    monkeypatch.setattr(feedback, "JsonStore", FakeStore)
    app = FastAPI()
    app.include_router(feedback.create_feedback_router(
        tmp_path / "state.json", assignment_loader=lambda _: None, grading_repository=repo,
    ))
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, repo=repo, aid=aid)


def body(run_id="run-1", tests=None, psid="312345678"):
    return {
        "submission": {"platform_submission_id": psid, "submitted_at": SUBMITTED.isoformat(),
                       "student_identifier": "alice@example.edu"},
        "run_id": run_id,
        "visibility": "visible",
        "tests": tests if tests is not None else [
            {"name": "add: negative", "status": "failed", "output": "expected -1", "score": 0, "max_score": 1,
             "tags": ["ignored"]},
            {"name": "add: positive", "status": "passed", "score": 1, "max_score": 1},
        ],
    }


def post(api, payload, token=TOKEN, psid="312345678"):
    return api.client.post(f"/v1/submissions/{psid}/test-results", content=json.dumps(payload),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})


def detail(api):
    (summary,) = api.repo.list_submissions(api.aid)
    return api.repo.get_submission_detail(summary.id)


def test_endpoint_records_a_run(api):
    response = post(api, body())

    assert response.status_code == 200
    assert response.json() == {"status": "recorded", "submission_id": "312345678", "recorded": 2, "targets": 2}
    add, fb = detail(api).targets
    assert [(r.status, r.functional_test_feedback) for r in add.test_results] == [("FAIL", "add: negative: expected -1")]
    assert add.status is TargetStatus.WAITING
    # The functional target saw a visible failure, so it still needs AI feedback.
    assert [r.status for r in fb.test_results] == ["FAIL"] and fb.status is TargetStatus.WAITING
    assert detail(api).student_identifier == "alice@example.edu"


def test_endpoint_repeat_adds_nothing_and_passing_run_opts_out(api):
    post(api, body())
    again = post(api, body())
    passing = post(api, body("run-2", [{"name": "add: one", "status": "passed"}]))

    assert again.json()["recorded"] == 0
    assert passing.json()["recorded"] == 2
    add, fb = detail(api).targets
    assert [r.status for r in add.test_results] == ["FAIL", "PASS"]
    assert fb.status is TargetStatus.OPTED_OUT


@pytest.mark.parametrize("token", ["wrong-token-0123456789", ""])
def test_endpoint_requires_the_assignment_token(api, token):
    response = post(api, body(), token=token)
    assert response.status_code == 401
    assert api.repo.list_submissions(api.aid) == []


def test_endpoint_rejects_mismatched_submission_and_bad_body(api):
    assert post(api, body(psid="1"), psid="2").status_code == 422
    assert post(api, {**body(), "unexpected": 1}).status_code == 422
    assert post(api, {**body(), "run_id": "has spaces"}).status_code == 422
    assert api.repo.list_submissions(api.aid) == []


def test_endpoint_refuses_an_inactive_assignment(api):
    api.repo.assignments[api.aid].is_active = False
    assert post(api, body()).status_code == 403

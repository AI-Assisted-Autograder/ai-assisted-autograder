import json

import pytest

from ai_grading.ResultsPackager import (
    FeedbackItem,
    FeedbackPackage,
    ResultsFormatError,
    ResultsPackager,
)


def base_results():
    return {
        "tests": [{"name": "test_add", "score": 1, "max_score": 1, "output": "passed"}],
        "extra": {"kept": True},
    }


def test_splice_preserves_functional_results_and_adds_zero_point_feedback():
    original = base_results()
    updated = ResultsPackager().splice(
        original,
        FeedbackPackage((FeedbackItem("add", "Use a clearer variable name.", 0.5),)),
    )

    assert updated["tests"][0] == original["tests"][0]
    assert updated["extra"] == {"kept": True}
    assert updated["tests"][1] == {
        "name": "AI Feedback: add",
        "score": 0,
        "max_score": 0,
        "output": "Suggested score: 0.5\n\nUse a clearer variable name.",
    }
    assert original["tests"] == [base_results()["tests"][0]]


def test_deferred_message_promises_the_active_submission_next_run():
    result = ResultsPackager().append_deferred(base_results())

    assert result["tests"][-1]["name"] == "AI Feedback Deferred"
    assert "active submission" in result["tests"][-1]["output"]
    assert result["tests"][-1]["score"] == 0


def test_error_message_is_visible_and_does_not_change_test_score():
    result = ResultsPackager().append_error(base_results(), "source file could not be resolved")

    assert result["tests"][-1]["name"] == "AI Feedback Unavailable"
    assert result["tests"][-1]["output"] == "source file could not be resolved"
    assert result["tests"][0]["score"] == 1


def test_parse_and_write_round_trip(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(json.dumps(base_results()), encoding="utf-8")

    packager = ResultsPackager()
    parsed = packager.parse(path)
    packager.write(path, packager.append_error(parsed, "provider unavailable"))

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["tests"][-1]["output"] == "provider unavailable"


@pytest.mark.parametrize("invalid", [{}, {"tests": {}}, [], None])
def test_rejects_invalid_results_shape(invalid):
    with pytest.raises(ResultsFormatError):
        ResultsPackager().splice(invalid, FeedbackPackage())

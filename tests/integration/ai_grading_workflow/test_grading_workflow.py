"""Cross-component grading workflow: source resolution -> prompt -> provider -> result file.

These are integration tests: each scenario passes real objects through several
production components. Only the external model provider is deterministic.
"""
import json

from ai_grading.FileResolver import FileResolver
from ai_grading.ILLMProvider.FakeProvider import FakeProvider
from ai_grading.AIGradingComponent import AIGradingComponent, RetryPolicy
from ai_grading.PromptBuilder import PromptBuilder
from ai_grading.ResultsPackager import FeedbackItem, FeedbackPackage, ResultsPackager
from ai_grading.models import (
    FeedbackRequest, GradingTarget, Rubric, TargetKind,
)


def test_submission_source_is_resolved_prompted_graded_and_spliced(tmp_path):
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "calculator.py").write_text(
        "def add(a, b):\\n    return a - b\\n", encoding="utf-8"
    )
    (submission / "README.md").write_text("Student project", encoding="utf-8")

    resolved = FileResolver().resolve("calculator.py", TargetKind.FILE, "python", submission)
    request = FeedbackRequest(
        target=GradingTarget(identifier="calculator.py", kind=TargetKind.FILE, max_score=10),
        rubric=Rubric(criteria_text="The add function must return the sum of its inputs."),
        resolved=resolved,
        functional_test_feedback=("FAIL: add(2, 3) returned -1, expected 5.",),
    )

    prompt = PromptBuilder().prepare(request)
    assert "calculator.py" in prompt.context
    assert "returned -1" in prompt.context
    assert "README.md" in prompt.context

    result = AIGradingComponent(
        [FakeProvider("The implementation subtracts instead of adding.", 2)],
        retry_policy=RetryPolicy(max_attempts=1),
    ).grade(request)

    assert result.selected.suggested_score == 2
    assert "subtracts" in result.selected.feedback_text

    results_path = tmp_path / "results.json"
    original = {
        "score": 0,
        "tests": [{"name": "Functional: add", "score": 0, "max_score": 5, "output": "Expected 5"}],
    }
    results_path.write_text(json.dumps(original), encoding="utf-8")
    package = FeedbackPackage((FeedbackItem(
        target="calculator.py",
        body=result.selected.feedback_text,
        suggested_score=result.selected.suggested_score,
    ),))
    updated = ResultsPackager().splice(ResultsPackager().parse(results_path), package)
    ResultsPackager().write(results_path, updated)

    persisted = json.loads(results_path.read_text(encoding="utf-8"))
    assert persisted["score"] == 0
    assert persisted["tests"][0] == original["tests"][0]
    assert persisted["tests"][1]["name"] == "AI Feedback: calculator.py"
    assert persisted["tests"][1]["score"] == 0
    assert "Suggested score: 2" in persisted["tests"][1]["output"]


def test_nightly_deferral_preserves_functional_results(tmp_path):
    results_path = tmp_path / "results.json"
    functional = {"name": "Functional: parser", "score": 4, "max_score": 4, "output": "Passed"}
    results_path.write_text(json.dumps({"score": 4, "tests": [functional]}), encoding="utf-8")

    packager = ResultsPackager()
    deferred = packager.append_deferred(packager.parse(results_path))
    packager.write(results_path, deferred)
    persisted = packager.parse(results_path)

    assert persisted["score"] == 4
    assert persisted["tests"][0] == functional
    assert persisted["tests"][1]["name"] == "AI Feedback Deferred"
    assert persisted["tests"][1]["max_score"] == 0

"""Synthetic grading requests; student code is never executed."""

from typing import Callable

from ..models import (
    CodeFile,
    FeedbackRequest,
    GradingTarget,
    ResolvedSource,
    Rubric,
    TargetKind,
)


def _request(
    identifier: str,
    kind: TargetKind,
    max_score: float,
    criteria: str,
    guidance: str,
    functional_test_feedback: tuple[str, ...],
    source_file: CodeFile,
    other_file_paths: tuple[str, ...] = (),
) -> FeedbackRequest:
    return FeedbackRequest(
        target=GradingTarget(identifier, kind, max_score),
        rubric=Rubric(
            version=1,
            criteria_text=criteria,
            feedback_guidance=guidance,
        ),
        functional_test_feedback=functional_test_feedback,
        resolved=ResolvedSource(source_file, other_file_paths),
    )


def sample_request() -> FeedbackRequest:
    """Missing return for an empty list."""
    return _request(
        identifier="solution.py:sum_values",
        kind=TargetKind.METHOD,
        max_score=5.0,
        criteria="Return the sum of a list of integers, including 0 for an empty list.",
        guidance="Explain the issue and give a hint, without a complete solution.",
        functional_test_feedback=(
            "test_nonempty passed: sum_values([1, 2]) returned 3.",
            "test_empty failed: expected 0, got None.",
        ),
        source_file=CodeFile(
            "solution.py",
            "def sum_values(values):\n    if values:\n        return sum(values)\n",
        ),
    )


def multi_file_request() -> FeedbackRequest:
    return _request(
        identifier="report.py:average",
        kind=TargetKind.METHOD,
        max_score=10.0,
        criteria=(
            "Compute the arithmetic mean of comma-separated integers. Ignore "
            "empty fields and return 0.0 when there are no numbers."
        ),
        guidance="For each issue, cite the relevant file and test; offer hints, not replacement code.",
        functional_test_feedback=(
            "test_average failed: average('2,4') returned 6; expected 3.0.",
            "test_empty_field failed: average('2,,4') raised ValueError in parsing.py; expected 3.0.",
            "test_empty_input passed: average('') returned 0.0.",
        ),
        source_file=CodeFile("report.py", (
            "from parsing import parse_numbers\n\n"
            "def average(text):\n"
            "    values = parse_numbers(text)\n"
            "    if not values:\n"
            "        return 0.0\n"
            "    return sum(values)\n"
        )),
        other_file_paths=("parsing.py",),
    )


def passing_style_request() -> FeedbackRequest:
    """AI remains enabled even though the instructor's reported tests passed."""
    return _request(
        identifier="summary.py",
        kind=TargetKind.FILE,
        max_score=5.0,
        criteria=(
            "Return the sum of nonnegative values. For readability, use descriptive "
            "variable names and document the function's input and output."
        ),
        guidance=(
            "All supplied functional tests passed. Focus on rubric-based readability "
            "feedback; do not claim a functional failure or provide a complete rewrite."
        ),
        functional_test_feedback=(
            "test_mixed passed: summarize([-2, 0, 3]) returned 3.",
            "test_empty passed: summarize([]) returned 0.",
        ),
        source_file=CodeFile("summary.py", (
            "def summarize(x):\n"
            "    y = 0\n"
            "    for z in x:\n"
            "        if z >= 0:\n"
            "            y += z\n"
            "    return y\n"
        )),
    )


def error_skip_request() -> FeedbackRequest:
    """No policy decision is inferred from ERROR/SKIP; assume caller selection."""
    return _request(
        identifier="solution.py:sum_values",
        kind=TargetKind.METHOD,
        max_score=5.0,
        criteria="Return the sum of a list of integers, including 0 for an empty list.",
        guidance=(
            "Separate test environment problems from code observations. Report "
            "missing evidence, do not invent pass/fail results, and leave the score "
            "unspecified when the evidence is insufficient."
        ),
        functional_test_feedback=(
            "test_nonempty ERROR: harness setup failed; fixture 'grading_context' not found. The student function was not called.",
            "test_empty SKIP: skipped because the grading environment was unavailable.",
        ),
        source_file=CodeFile("solution.py", "def sum_values(values):\n    return sum(values)\n"),
    )


SCENARIOS: dict[str, Callable[[], FeedbackRequest]] = {
    "empty-list": sample_request,
    "multi-file": multi_file_request,
    "passing-style": passing_style_request,
    "error-skip": error_skip_request,
}


import importlib
import json

import pytest

parser_module = importlib.import_module(
    "ai_grading.ILLMProvider.FeedbackParser"
)

parse_feedback = parser_module.parse_feedback
ProviderError = parser_module.ProviderError

from ai_grading.models import (
    AnalysisConfig,
    CodeFile,
    FeedbackRequest,
    GradingTarget,
    ResolvedSource,
    Rubric,
    TargetKind,
)


# --------------------------------------------------
# Request helpers
# --------------------------------------------------

def make_request(
    *,
    mode="code_feedback",
    max_score=100,
    expected_tests=None,
    points_per_test=None,
    behavior=None,
    grading_items=None,
    evidence=None,
    functional_result_source=None,
):
    expected_tests = expected_tests or []

    if points_per_test is not None:
        max_score = points_per_test * len(expected_tests)

    analysis = AnalysisConfig(
        mode=mode,
        expected_tests=expected_tests,
        points_per_test=points_per_test,
        behavior=behavior,
        grading_items=grading_items or [],
        functional_result_source=functional_result_source,
    )

    return FeedbackRequest(
        target=GradingTarget(
            identifier="example",
            kind=TargetKind.METHOD,
            max_score=max_score,
        ),
        rubric=Rubric(
            criteria_text="Evaluate correctness and code quality."
        ),
        resolved=ResolvedSource(
            source_file=CodeFile(
                path="solution.py",
                content="def example(): return 1",
            )
        ),
        analysis=analysis,
        evidence=evidence or [],
    )


def parse(output, request=None):
    return parse_feedback(
        output,
        request=request or make_request(),
        provider="TEST",
        model_name="test-model",
        prompt_version="v1",
        input_tokens=100,
        output_tokens=40,
    )


def expect_invalid(output, request=None):
    with pytest.raises(ProviderError) as exc:
        parse(output, request)

    assert exc.value.code == "invalid_provider_response"


def structured_output(
    items=None,
    feedback_text="Analysis completed.",
    suggested_score=None,
):
    document = {
        "feedback_text": feedback_text,
        "items": items if items is not None else [
            {
                "test_id": "test_1",
                "verdict": "pass",
                "feedback": "Passed successfully.",
                "evidence": "The test passed successfully.",
            }
        ],
    }

    if suggested_score is not None:
        document["suggested_score"] = suggested_score

    return json.dumps(document)


def make_verdict(
    test_id="test_1",
    verdict="pass",
    feedback="Test feedback",
    evidence=None,
):
    return {
        "test_id": test_id,
        "verdict": verdict,
        "feedback": feedback,
        "evidence": evidence or "Evidence supports this verdict.",
    }


# --------------------------------------------------
# Common response metadata
# --------------------------------------------------

def test_includes_response_metadata():
    result = parse("SCORE: 80\nGood implementation.")

    assert result.provider == "TEST"
    assert result.model_name == "test-model"
    assert result.prompt_version == "v1"
    assert result.input_tokens == 100
    assert result.output_tokens == 40


# --------------------------------------------------
# Legacy code feedback
# --------------------------------------------------

@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("80", 80.0),
        ("75.5", 75.5),
        ("0", 0.0),
        ("100", 100.0),
        ("NONE", None),
    ],
)
def test_parses_legacy_score(score, expected):
    result = parse(f"SCORE: {score}\nGood implementation.")

    assert result.suggested_score == expected
    assert result.feedback_text == "Good implementation."


def test_parses_legacy_feedback_with_multiline_text():
    result = parse(
        "SCORE: 85\n"
        "Good implementation.\n"
        "Consider improving variable names."
    )

    assert result.suggested_score == 85.0
    assert result.feedback_text == (
        "Good implementation.\n"
        "Consider improving variable names."
    )


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Good implementation.",
        "SCORE: 80",
        "SCORE: eighty\nFeedback",
        "SCORE: -5\nFeedback",
        "SCORE: 10.5.2\nFeedback",
        "SCORE:  80\nFeedback",
        "score: 80\nFeedback",
        "SCORE: 80 extra\nFeedback",
    ],
)
def test_rejects_invalid_legacy_score_format(output):
    expect_invalid(output)


def test_rejects_score_above_maximum():
    request = make_request(max_score=100)

    expect_invalid("SCORE: 101\nFeedback", request)


def test_accepts_score_equal_to_maximum():
    request = make_request(max_score=100)

    result = parse("SCORE: 100\nFeedback", request)

    assert result.suggested_score == 100.0


def test_allows_score_without_configured_maximum():
    request = make_request(max_score=None)

    result = parse("SCORE: 500\nFeedback", request)

    assert result.suggested_score == 500.0


# --------------------------------------------------
# Structured analysis
# --------------------------------------------------

def test_parses_structured_feedback():
    request = make_request(mode="test_quality")

    output = structured_output(
        feedback_text="Overall analysis.",
        items=[
            make_verdict("test_1", "pass"),
            make_verdict("test_2", "fail"),
        ],
    )

    result = parse(output, request)

    assert result.feedback_text == "Overall analysis."
    assert len(result.items) == 2
    assert result.suggested_score is None


def test_preserves_structured_verdicts():
    request = make_request(mode="test_quality")

    output = structured_output(
        items=[
            make_verdict(
                "test_1",
                "pass",
                "Correct implementation.",
            ),
            make_verdict(
                "test_2",
                "fail",
                "Incorrect implementation.",
            ),
        ],
    )

    result = parse(output, request)

    assert result.items[0].test_id == "test_1"
    assert result.items[0].verdict == "pass"
    assert result.items[0].feedback == "Correct implementation."

    assert result.items[1].test_id == "test_2"
    assert result.items[1].verdict == "fail"


def test_rejects_invalid_structured_json():
    request = make_request(mode="test_quality")

    expect_invalid("{invalid json}", request)


@pytest.mark.parametrize(
    "document",
    [
        [],
        None,
        "invalid",
        {},
        {"feedback_text": "Missing items"},
        {"items": []},
        {"feedback_text": "Feedback", "items": []},
        {
            "feedback_text": "Feedback",
            "items": [],
            "extra": "not allowed",
        },
    ],
)
def test_rejects_invalid_structured_documents(document):
    request = make_request(mode="test_quality")

    expect_invalid(json.dumps(document), request)


def test_rejects_empty_feedback_items():
    request = make_request(mode="test_quality")

    expect_invalid(structured_output(items=[]), request)


def test_rejects_duplicate_test_ids():
    request = make_request(mode="test_quality")

    output = structured_output(
        items=[
            make_verdict("test_1"),
            make_verdict("test_1", "fail"),
        ]
    )

    expect_invalid(output, request)


def test_rejects_extra_verdict_fields():
    request = make_request(mode="test_quality")

    item = make_verdict()
    item["suggested_score"] = 100

    expect_invalid(
        structured_output(items=[item]),
        request,
    )


def test_rejects_invalid_verdict():
    request = make_request(mode="test_quality")

    expect_invalid(
        structured_output(
            items=[make_verdict(verdict="invalid")]
        ),
        request,
    )


# --------------------------------------------------
# Expected test validation
# --------------------------------------------------

def test_reorders_items_to_match_expected_tests():
    request = make_request(
        mode="testplan",
        expected_tests=["test_1", "test_2"],
    )

    output = structured_output(
        items=[
            make_verdict("test_2", "fail"),
            make_verdict("test_1", "pass"),
        ],
    )

    result = parse(output, request)

    assert [item.test_id for item in result.items] == [
        "test_1",
        "test_2",
    ]


def test_rejects_missing_expected_tests():
    request = make_request(
        mode="testplan",
        expected_tests=["test_1", "test_2"],
    )

    output = structured_output(
        items=[make_verdict("test_1")]
    )

    expect_invalid(output, request)


def test_rejects_unexpected_test_ids():
    request = make_request(
        mode="testplan",
        expected_tests=["test_1"],
    )

    output = structured_output(
        items=[make_verdict("test_2")]
    )

    expect_invalid(output, request)


def test_testplan_requires_expected_identifiers():
    request = make_request(
        mode="testplan",
        expected_tests=[],
    )

    expect_invalid(structured_output(), request)


# --------------------------------------------------
# Structured scoring
# --------------------------------------------------

def test_accepts_valid_suggested_score():
    request = make_request(
        mode="test_quality",
        max_score=100,
    )

    result = parse(
        structured_output(suggested_score=85),
        request,
    )

    assert result.suggested_score == 85


def test_rejects_suggested_score_without_maximum():
    request = make_request(
        mode="test_quality",
        max_score=None,
    )

    expect_invalid(
        structured_output(suggested_score=85),
        request,
    )


def test_rejects_suggested_score_above_maximum():
    request = make_request(
        mode="test_quality",
        max_score=100,
    )

    expect_invalid(
        structured_output(suggested_score=101),
        request,
    )


def test_rejects_model_score_when_points_per_test_configured():
    request = make_request(
        mode="testplan",
        expected_tests=["test_1", "test_2"],
        points_per_test=10,
    )

    expect_invalid(
        structured_output(suggested_score=15),
        request,
    )


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (["pass", "pass"], 20.0),
        (["pass", "fail"], 10.0),
        (["fail", "fail"], 0.0),
    ],
)
def test_computes_score_from_per_test_verdicts(verdicts, expected):
    request = make_request(
        mode="testplan",
        expected_tests=["test_1", "test_2"],
        points_per_test=10,
    )

    output = structured_output(
        items=[
            make_verdict("test_1", verdicts[0]),
            make_verdict("test_2", verdicts[1]),
        ],
    )

    result = parse(output, request)

    assert result.suggested_score == expected
    assert all(item.max_score == 10 for item in result.items)


def test_pending_score_when_verdict_is_uncertain():
    request = make_request(
        mode="testplan",
        expected_tests=["test_1", "test_2"],
        points_per_test=10,
    )

    output = structured_output(
        items=[
            make_verdict("test_1", "pass"),
            make_verdict(
                "test_2",
                "insufficient_evidence",
                "There is insufficient evidence to determine the result.",
            ),
        ],
    )

    result = parse(output, request)

    assert result.suggested_score is None
    assert result.items[1].suggested_score is None


# --------------------------------------------------
# Output size limits
# --------------------------------------------------

def test_rejects_oversized_structured_output():
    request = make_request(mode="test_quality")

    expect_invalid(" " * 64_001, request)


def test_accepts_structured_output_at_size_limit():
    request = make_request(mode="test_quality")

    document = structured_output()
    output = document[:-1] + (
        " " * (64_000 - len(document))
    ) + "}"

    assert len(output) == 64_000

    result = parse(output, request)

    assert result.feedback_text == "Analysis completed."


# --------------------------------------------------
# Provider error consistency
# --------------------------------------------------

def test_invalid_output_uses_provider_name_in_error():
    with pytest.raises(ProviderError) as exc:
        parse_feedback(
            "invalid",
            request=make_request(),
            provider="CUSTOM_PROVIDER",
            model_name="test-model",
            prompt_version="v1",
        )

    assert "CUSTOM_PROVIDER" in str(exc.value)
    assert exc.value.code == "invalid_provider_response"

import pytest
from pydantic import ValidationError

from ai_grading.models import (
    AnalysisConfig,
    CodeFile,
    Comparison,
    Evidence,
    EvidenceSource,
    FeedbackItem,
    FeedbackRequest,
    FeedbackResponse,
    GradingInputError,
    GradingResult,
    GradingTarget,
    ResolvedSource,
    Rubric,
    TargetKind,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_target(**overrides):
    values = {
        "identifier": "src/main.py",
        "kind": TargetKind.FILE,
        "max_score": 10.0,
        "id": "target-1",
    }
    values.update(overrides)
    return GradingTarget(**values)


def make_rubric(**overrides):
    values = {
        "criteria_text": "Check correctness",
        "feedback_guidance": "",
        "version": 1,
        "id": "rubric-1",
        "grading_target_id": "target-1",
    }
    values.update(overrides)
    return Rubric(**values)


def make_resolved(**overrides):
    values = {
        "source_file": CodeFile(path="src/main.py", content="print('hello')"),
    }
    values.update(overrides)
    return ResolvedSource(**values)


def make_request(**overrides):
    values = {
        "target": make_target(),
        "rubric": make_rubric(),
        "resolved": make_resolved(),
    }
    values.update(overrides)
    return FeedbackRequest(**values)


def make_response(**overrides):
    values = {
        "provider": "GOOGLE",
        "model_name": "gemini-test",
        "prompt_version": "v1",
        "feedback_text": "The implementation is correct.",
    }
    values.update(overrides)
    return FeedbackResponse(**values)


# ---------------------------------------------------------------------------
# GradingTarget
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", list(TargetKind))
def test_grading_target_accepts_valid_kinds(kind):
    target = make_target(kind=kind)
    assert target.kind == kind


@pytest.mark.parametrize("score", [0.0, 1.0, 100.0, None])
def test_grading_target_accepts_valid_max_scores(score):
    assert make_target(max_score=score).max_score == score


@pytest.mark.parametrize("score", [-1.0, float("inf"), float("-inf"), float("nan")])
def test_grading_target_rejects_invalid_max_scores(score):
    with pytest.raises(ValidationError):
        make_target(max_score=score)


def test_grading_target_accepts_integer_max_score():
    # Current Pydantic dataclass behavior accepts an integer for this field.
    target = make_target(max_score=10)
    assert target.max_score == 10.0



def test_grading_target_rejects_blank_identifier():
    with pytest.raises(ValidationError):
        make_target(identifier="   ")


def test_grading_target_is_immutable():
    target = make_target()

    with pytest.raises((AttributeError, ValidationError)):
        target.identifier = "other.py"


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------

def test_rubric_accepts_valid_values():
    rubric = make_rubric()
    assert rubric.criteria_text == "Check correctness"
    assert rubric.version == 1


@pytest.mark.parametrize("version", [0, -1])
def test_rubric_rejects_invalid_versions(version):
    with pytest.raises(ValidationError):
        make_rubric(version=version)


def test_rubric_rejects_blank_criteria():
    with pytest.raises(ValidationError):
        make_rubric(criteria_text="   ")


def test_rubric_is_immutable():
    rubric = make_rubric()

    with pytest.raises((AttributeError, ValidationError)):
        rubric.criteria_text = "Changed"


# ---------------------------------------------------------------------------
# CodeFile and EvidenceSource
# ---------------------------------------------------------------------------

def test_code_file_accepts_empty_content():
    source = CodeFile(path="src/main.py", content="")
    assert source.content == ""


def test_code_file_rejects_blank_path():
    with pytest.raises(ValidationError):
        CodeFile(path=" ", content="code")


def test_evidence_source_accepts_valid_configuration():
    source = EvidenceSource(
        format="junit",
        path="results/report.xml",
        label="unit-tests",
        include=("test_login",),
    )

    assert source.format == "junit"
    assert source.include == ("test_login",)


def test_evidence_source_rejects_unknown_format():
    with pytest.raises(ValidationError):
        EvidenceSource(
            format="unknown",
            path="results/report.xml",
            label="tests",
        )


# ---------------------------------------------------------------------------
# AnalysisConfig
# ---------------------------------------------------------------------------

def test_analysis_config_defaults():
    config = AnalysisConfig()

    assert config.mode == "code_feedback"
    assert config.source_globs == ()
    assert config.context_globs == ()
    assert config.expected_tests == ()
    assert config.points_per_test is None


def test_analysis_config_rejects_duplicate_expected_tests():
    with pytest.raises(ValueError, match="distinct"):
        AnalysisConfig(expected_tests=("test_a", "test_a"))


def test_analysis_config_rejects_duplicate_evidence_source_labels():
    first = EvidenceSource(
        format="junit", path="a.xml", label="tests"
    )
    second = EvidenceSource(
        format="text", path="b.txt", label="tests"
    )

    with pytest.raises(ValueError, match="labels must be distinct"):
        AnalysisConfig(evidence_sources=(first, second))


def test_analysis_config_rejects_points_outside_testplan_mode():
    with pytest.raises(ValueError, match="per-test points"):
        AnalysisConfig(
            mode="code_feedback",
            expected_tests=("test_a",),
            points_per_test=1.0,
        )


def test_analysis_config_rejects_points_without_expected_tests():
    with pytest.raises(ValueError, match="per-test points"):
        AnalysisConfig(
            mode="testplan",
            points_per_test=1.0,
        )


def test_analysis_config_accepts_valid_testplan_scoring():
    config = AnalysisConfig(
        mode="testplan",
        expected_tests=("test_a", "test_b"),
        points_per_test=2.5,
    )

    assert config.points_per_test == 2.5
    assert len(config.expected_tests) == 2


def test_analysis_config_rejects_duplicate_labels_even_for_different_formats():
    sources = (
        EvidenceSource(format="junit", path="a.xml", label="report"),
        EvidenceSource(format="jacoco", path="b.xml", label="report"),
    )

    with pytest.raises(ValueError, match="distinct"):
        AnalysisConfig(evidence_sources=sources)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def test_evidence_accepts_valid_observation():
    evidence = Evidence(
        id="ev-1",
        kind="runtime",
        source="unit-tests",
        summary="The test passed",
        test_id="test_login",
        scope="login",
        status="pass",
    )

    assert evidence.status == "pass"
    assert evidence.test_id == "test_login"


@pytest.mark.parametrize("kind", ["invalid", "coverage_report"])
def test_evidence_rejects_unknown_kind(kind):
    with pytest.raises(ValidationError):
        Evidence(
            id="ev-1",
            kind=kind,
            source="tests",
            summary="Result",
        )


@pytest.mark.parametrize("status", ["invalid", "success", "failed"])
def test_evidence_rejects_unknown_status(status):
    with pytest.raises(ValidationError):
        Evidence(
            id="ev-1",
            kind="runtime",
            source="tests",
            summary="Result",
            status=status,
        )


def test_evidence_rejects_blank_summary():
    with pytest.raises(ValidationError):
        Evidence(
            id="ev-1",
            kind="runtime",
            source="tests",
            summary=" ",
        )


# ---------------------------------------------------------------------------
# ResolvedSource
# ---------------------------------------------------------------------------

def test_resolved_source_accepts_primary_and_context_files():
    primary = CodeFile(path="main.py", content="main")
    context = CodeFile(path="helper.py", content="helper")

    resolved = ResolvedSource(
        source_file=primary,
        context_files=(context,),
        other_file_paths=("unread.py",),
    )

    assert resolved.source_file == primary
    assert resolved.context_files == (context,)
    assert resolved.other_file_paths == ("unread.py",)


def test_resolved_source_rejects_duplicate_other_file_paths():
    with pytest.raises(ValueError, match="other file paths"):
        ResolvedSource(
            source_file=CodeFile(path="main.py", content=""),
            other_file_paths=("helper.py", "helper.py"),
        )


def test_resolved_source_rejects_duplicate_supplied_paths():
    with pytest.raises(ValueError, match="supplied source file paths"):
        ResolvedSource(
            source_file=CodeFile(path="main.py", content=""),
            context_files=(
                CodeFile(path="main.py", content="duplicate"),
            ),
        )


def test_resolved_source_rejects_overlap_with_other_file_paths():
    with pytest.raises(ValueError, match="exclude supplied"):
        ResolvedSource(
            source_file=CodeFile(path="main.py", content=""),
            other_file_paths=("helper.py",),
            context_files=(
                CodeFile(path="helper.py", content=""),
            ),
        )


# ---------------------------------------------------------------------------
# FeedbackRequest
# ---------------------------------------------------------------------------

def test_feedback_request_accepts_valid_request():
    request = make_request()

    assert request.target.identifier == "src/main.py"
    assert request.rubric.id == "rubric-1"


def test_feedback_request_rejects_mismatched_rubric_target():
    with pytest.raises(ValueError, match="grading target"):
        make_request(
            rubric=make_rubric(grading_target_id="another-target")
        )


def test_feedback_request_allows_unset_target_ids():
    request = make_request(
        target=make_target(id=None),
        rubric=make_rubric(grading_target_id="another-target"),
    )

    assert request.target.id is None


def test_feedback_request_rejects_duplicate_evidence_ids():
    first = Evidence(
        id="ev-1", kind="runtime", source="tests", summary="First"
    )
    second = Evidence(
        id="ev-1", kind="runtime", source="tests", summary="Second"
    )

    with pytest.raises(ValueError, match="evidence identifiers"):
        make_request(evidence=(first, second))


def test_feedback_request_rejects_inconsistent_testplan_max_score():
    analysis = AnalysisConfig(
        mode="testplan",
        expected_tests=("test_a", "test_b"),
        points_per_test=2.0,
    )

    with pytest.raises(ValueError, match="maximum"):
        make_request(
            target=make_target(max_score=5.0),
            analysis=analysis,
        )


def test_feedback_request_requires_max_score_for_testplan_points():
    analysis = AnalysisConfig(
        mode="testplan",
        expected_tests=("test_a", "test_b"),
        points_per_test=2.0,
    )

    with pytest.raises(ValueError, match="maximum"):
        make_request(
            target=make_target(max_score=None),
            analysis=analysis,
        )


def test_feedback_request_accepts_consistent_testplan_max_score():
    analysis = AnalysisConfig(
        mode="testplan",
        expected_tests=("test_a", "test_b"),
        points_per_test=2.0,
    )

    request = make_request(
        target=make_target(max_score=4.0),
        analysis=analysis,
    )

    assert request.target.max_score == 4.0


# ---------------------------------------------------------------------------
# FeedbackItem
# ---------------------------------------------------------------------------

def make_item(**overrides):
    values = {
        "test_id": "test_login",
        "verdict": "pass",
        "feedback": "",
        "evidence": "Observed successful login",
    }
    values.update(overrides)
    return FeedbackItem(**values)


def test_feedback_item_accepts_passing_verdict_without_feedback():
    item = make_item()

    assert item.verdict == "pass"
    assert item.feedback == ""


@pytest.mark.parametrize("verdict", ["fail", "insufficient_evidence"])
def test_feedback_item_requires_feedback_for_nonpassing_verdicts(verdict):
    with pytest.raises(ValueError, match="require student feedback"):
        make_item(verdict=verdict, feedback="  ")


def test_feedback_item_accepts_nonpassing_verdict_with_feedback():
    item = make_item(
        verdict="fail",
        feedback="The login check fails for invalid credentials.",
    )

    assert item.verdict == "fail"


def test_feedback_item_rejects_unknown_verdict():
    with pytest.raises(ValidationError):
        make_item(verdict="unknown")


def test_feedback_item_rejects_score_without_maximum():
    with pytest.raises(ValueError, match="requires"):
        make_item(suggested_score=2.0)


def test_feedback_item_rejects_score_above_maximum():
    with pytest.raises(ValueError, match="exceed"):
        make_item(suggested_score=3.0, max_score=2.0)


def test_feedback_item_accepts_score_equal_to_maximum():
    item = make_item(suggested_score=2.0, max_score=2.0)

    assert item.suggested_score == item.max_score


def test_feedback_item_rejects_blank_evidence():
    with pytest.raises(ValidationError):
        make_item(evidence=" ")


def test_feedback_item_rejects_feedback_over_character_limit():
    from ai_grading.models import MAX_FEEDBACK_CHARS

    with pytest.raises(ValidationError):
        make_item(feedback="x" * (MAX_FEEDBACK_CHARS + 1))


# ---------------------------------------------------------------------------
# FeedbackResponse
# ---------------------------------------------------------------------------

def test_feedback_response_accepts_valid_response():
    response = make_response()

    assert response.provider == "GOOGLE"
    assert response.feedback_text == "The implementation is correct."


def test_feedback_response_rejects_blank_feedback_text():
    with pytest.raises(ValidationError):
        make_response(feedback_text="   ")


def test_feedback_response_rejects_blank_model_name():
    with pytest.raises(ValidationError):
        make_response(model_name=" ")


def test_feedback_response_rejects_negative_token_counts():
    with pytest.raises(ValidationError):
        make_response(input_tokens=-1)


def test_feedback_response_accepts_zero_token_counts():
    response = make_response(input_tokens=0, output_tokens=0)

    assert response.input_tokens == 0
    assert response.output_tokens == 0


def test_feedback_response_rejects_negative_suggested_score():
    with pytest.raises(ValidationError):
        make_response(suggested_score=-1.0)


def test_feedback_response_rejects_duplicate_or_extra_fields():
    with pytest.raises(ValidationError):
        FeedbackResponse(
            provider="GOOGLE",
            model_name="gemini-test",
            prompt_version="v1",
            feedback_text="Feedback",
            unexpected="value",
        )


def test_feedback_response_accepts_feedback_items():
    item = make_item()
    response = make_response(items=(item,))

    assert response.items == (item,)


# ---------------------------------------------------------------------------
# Comparison and GradingResult
# ---------------------------------------------------------------------------

def test_comparison_accepts_valid_values():
    comparison = Comparison(
        selection_reason="Higher quality feedback",
        score_spread=2.0,
        confidence=0.9,
    )

    assert comparison.confidence == 0.9


@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan")])
def test_comparison_rejects_invalid_score_spread(value):
    with pytest.raises(ValidationError):
        Comparison(
            selection_reason="Reason",
            score_spread=value,
        )


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_comparison_does_not_assume_confidence_is_probability(value):
    # The current contract constrains confidence to be nonnegative, but
    # does not specify an upper bound of 1.
    if value < 0:
        with pytest.raises(ValidationError):
            Comparison(selection_reason="Reason", confidence=value)
    else:
        assert Comparison(
            selection_reason="Reason", confidence=value
        ).confidence == value


def test_grading_result_accepts_selected_response():
    response = make_response()
    result = GradingResult(
        responses=(response,),
        selected=response,
        comparison=Comparison(selection_reason="Only response"),
    )

    assert result.selected == response
    assert result.responses == (response,)


def test_grading_result_does_not_require_selected_response_to_be_in_responses():
    selected = make_response(model_name="gemini-selected")
    other = make_response(model_name="gemini-other")

    result = GradingResult(
        responses=(other,),
        selected=selected,
        comparison=Comparison(selection_reason="Selected separately"),
    )

    # This documents current behavior; add a production invariant if this
    # should be prohibited.
    assert result.selected == selected


# ---------------------------------------------------------------------------
# Contract configuration
# ---------------------------------------------------------------------------

def test_dataclasses_reject_extra_fields():
    with pytest.raises(ValidationError):
        GradingTarget(
            identifier="src/main.py",
            kind=TargetKind.FILE,
            unexpected="value",
        )


def test_dataclasses_are_frozen():
    response = make_response()

    with pytest.raises((AttributeError, ValidationError)):
        response.provider = "OPENAI"


def test_grading_input_error_is_a_value_error():
    assert issubclass(GradingInputError, ValueError)

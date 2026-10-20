
"""Unit tests for PromptBuilder.

These tests verify prompt construction, JSON serialization, input validation,
size limits, score-related context, and the public prepare/build interfaces.

No database, filesystem access, or LLM provider is required.
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_grading.models import (
    MAX_FEEDBACK_CHARS,
    FeedbackRequest,
    GradingInputError,
    GradingTarget,
    ResolvedSource,
    Rubric,
)
from ai_grading.PromptBuilder import PromptBuilder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def builder():
    """Return a PromptBuilder with its default configuration."""
    return PromptBuilder()


@pytest.fixture
def rubric():
    """Provide a representative grading rubric."""
    return Rubric(
        id="rubric-1",
        version=2,
        criteria_text="Correctness, readability, and edge-case handling.",
        feedback_guidance="Be constructive and specific.",
    )


@pytest.fixture
def target():
    """Provide a representative grading target."""
    return GradingTarget(
        identifier="calculate_total",
        kind="METHOD",
        max_score=10,
    )


@pytest.fixture
def resolved_source():
    """Provide resolved source without creating any real files."""
    return ResolvedSource(
        source_file={
            "path": "src/calculator.py",
            "content": "def calculate_total(values):\n    return sum(values)\n",
        },
        other_file_paths=("README.md", "tests/test_calculator.py"),
    )


@pytest.fixture
def feedback_request(rubric, target, resolved_source):
    """Create a complete request for the prepare() entry point."""
    return FeedbackRequest(
        rubric=rubric,
        target=target,
        functional_test_feedback=("Empty list returned 0.",),
        resolved=resolved_source,
    )


# ---------------------------------------------------------------------------
# Initialization and configuration
# ---------------------------------------------------------------------------

def test_default_configuration(builder):
    """The builder should expose the expected version and positive limits."""
    assert builder.version == "feedback-v4"
    assert builder.max_context_chars > 0
    assert builder.max_feedback_chars > 0
    assert builder.max_target_feedback_chars > 0


@pytest.mark.parametrize(
    "argument",
    [
        "max_context_chars",
        "max_feedback_chars",
        "max_target_feedback_chars",
    ],
)
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "100"])
def test_invalid_limits_are_rejected(argument, value):
    """Every configured character limit must be a positive integer."""
    with pytest.raises(ValueError, match="positive integer"):
        PromptBuilder(**{argument: value})


# ---------------------------------------------------------------------------
# Prompt preparation and serialization
# ---------------------------------------------------------------------------

def test_prepare_returns_prepared_prompt(
    builder,
    feedback_request,
):
    """prepare() should return all three PreparedPrompt fields."""
    prompt = builder.prepare(feedback_request)

    assert prompt.version == builder.version
    assert prompt.system_instruction == builder.instructions
    assert isinstance(prompt.context, str)
    assert prompt.text.startswith(builder.instructions)
    assert "GRADING_CONTEXT_JSON:" in prompt.text


def test_context_is_valid_json(
    builder,
    feedback_request,
):
    """The context section must be parseable JSON."""
    prompt = builder.prepare(feedback_request)

    context = json.loads(prompt.context)

    assert isinstance(context, dict)
    assert "rubric" in context
    assert "target" in context
    assert "resolved" in context
    assert "functional_test_feedback" in context


def test_context_contains_rubric_target_and_feedback(
    builder,
    feedback_request,
):
    """The serialized context should preserve the supplied grading data."""
    prompt = builder.prepare(feedback_request)
    context = json.loads(prompt.context)

    assert context["rubric"]["id"] == "rubric-1"
    assert context["rubric"]["version"] == 2
    assert (
        context["rubric"]["criteria_text"]
        == "Correctness, readability, and edge-case handling."
    )
    assert context["target"]["identifier"] == "calculate_total"
    assert context["functional_test_feedback"] == [
        "Empty list returned 0."
    ]


def test_context_preserves_unicode(
    builder,
    rubric,
    resolved_source,
):
    """Unicode characters should remain readable in the JSON context."""
    unicode_rubric = Rubric(
        id=rubric.id,
        version=rubric.version,
        criteria_text="Handle café values correctly.",
        feedback_guidance="Use clear explanations.",
    )

    prompt = builder.build(
        unicode_rubric,
        unicode_rubric.feedback_guidance,
        (),
        resolved_source,
    )

    assert "café" in prompt


def test_context_without_target_is_supported(
    builder,
    rubric,
    resolved_source,
):
    """The build() interface should allow target=None."""
    prompt = builder.build(
        rubric,
        rubric.feedback_guidance,
        (),
        resolved_source,
    )

    context_json = prompt.split("GRADING_CONTEXT_JSON:\n", 1)[1]
    context = json.loads(context_json)

    assert context["target"] is None


def test_build_returns_string(
    builder,
    rubric,
    resolved_source,
):
    """build() should return the complete prompt as a string."""
    prompt = builder.build(
        rubric,
        rubric.feedback_guidance,
        ("One test failed.",),
        resolved_source,
    )

    assert isinstance(prompt, str)
    assert prompt.startswith(builder.instructions)
    assert "GRADING_CONTEXT_JSON:" in prompt


def test_build_and_prepare_produce_equivalent_text(
    builder,
    feedback_request,
):
    """Both public entry points should use the same construction logic."""
    prepared = builder.prepare(feedback_request)

    built = builder.build(
        feedback_request.rubric,
        feedback_request.rubric.feedback_guidance,
        feedback_request.functional_test_feedback,
        feedback_request.resolved,
        target=feedback_request.target,
    )

    assert prepared.text == built


# ---------------------------------------------------------------------------
# Prompt instructions
# ---------------------------------------------------------------------------

def test_instructions_require_plain_text_and_score_line(builder):
    """The system instructions should define the expected response format."""
    assert "Return plain text, never JSON." in builder.instructions
    assert "SCORE: <value>" in builder.instructions
    assert "SCORE: NONE" in builder.instructions


def test_instructions_warn_against_untrusted_source_content(builder):
    """Submitted source and diagnostics must be treated as untrusted evidence."""
    assert "untrusted" in builder.instructions
    assert "Do not execute code" in builder.instructions


def test_instructions_discourage_invented_test_results(builder):
    """The model should not fabricate test outcomes or missing diagnostics."""
    assert "Do not invent" in builder.instructions
    assert "do not claim that all tests passed" in builder.instructions


# ---------------------------------------------------------------------------
# Feedback validation and size limits
# ---------------------------------------------------------------------------

def test_empty_feedback_is_allowed(
    builder,
    rubric,
    resolved_source,
):
    """Missing diagnostics should be represented by an empty list."""
    prompt = builder.build(
        rubric,
        rubric.feedback_guidance,
        (),
        resolved_source,
    )

    context_json = prompt.split("GRADING_CONTEXT_JSON:\n", 1)[1]
    context = json.loads(context_json)

    assert context["functional_test_feedback"] == []


def test_feedback_at_individual_limit_is_allowed(
    rubric,
    resolved_source,
):
    """A feedback row exactly at the configured limit should be accepted."""
    builder = PromptBuilder(max_feedback_chars=10)

    prompt = builder.build(
        rubric,
        rubric.feedback_guidance,
        ("x" * 10,),
        resolved_source,
    )

    assert "xxxxxxxxxx" in prompt


def test_feedback_over_individual_limit_is_rejected(
    rubric,
    resolved_source,
):
    """A feedback row exceeding max_feedback_chars should fail."""
    builder = PromptBuilder(max_feedback_chars=10)

    with pytest.raises(
        GradingInputError,
        match="row exceeds max_feedback_chars",
    ):
        builder.build(
            rubric,
            rubric.feedback_guidance,
            ("x" * 11,),
            resolved_source,
        )


def test_combined_feedback_over_target_limit_is_rejected(
    rubric,
    resolved_source,
):
    """Multiple rows must respect the combined feedback character limit."""
    builder = PromptBuilder(
        max_feedback_chars=10,
        max_target_feedback_chars=15,
    )

    with pytest.raises(
        GradingInputError,
        match="exceeds max_target_feedback_chars",
    ):
        builder.build(
            rubric,
            rubric.feedback_guidance,
            ("12345678", "abcdefgh"),
            resolved_source,
        )


def test_feedback_at_combined_limit_is_allowed(
    rubric,
    resolved_source,
):
    """Feedback whose combined length equals the limit should be accepted."""
    builder = PromptBuilder(
        max_feedback_chars=10,
        max_target_feedback_chars=15,
    )

    prompt = builder.build(
        rubric,
        rubric.feedback_guidance,
        ("12345678", "1234567"),
        resolved_source,
    )

    assert "12345678" in prompt
    assert "1234567" in prompt


def test_feedback_row_limit_uses_configured_value(
    rubric,
    resolved_source,
):
    """The builder should honor a custom per-row limit."""
    builder = PromptBuilder(max_feedback_chars=5)

    with pytest.raises(GradingInputError):
        builder.build(
            rubric,
            rubric.feedback_guidance,
            ("123456",),
            resolved_source,
        )


def test_context_over_limit_is_rejected(
    rubric,
    resolved_source,
):
    """The complete prompt, including instructions, must fit the limit."""
    builder = PromptBuilder(max_context_chars=100)

    with pytest.raises(
        GradingInputError,
        match="grading context exceeds max_context_chars",
    ):
        builder.build(
            rubric,
            rubric.feedback_guidance,
            (),
            resolved_source,
        )


def test_feedback_limit_default_matches_model_constant(builder):
    """The default feedback-row limit should match the shared model limit."""
    assert builder.max_feedback_chars == 4_000
    assert MAX_FEEDBACK_CHARS > 0


# ---------------------------------------------------------------------------
# JSON safety
# ---------------------------------------------------------------------------


def test_non_finite_float_in_context_is_rejected(
    builder,
    rubric,
    resolved_source,
):
    """Reject non-finite numbers if they occur in serialized context data."""
    import math
    from dataclasses import asdict

    from ai_grading.models import CodeFile

    source_with_non_finite_value = ResolvedSource(
        source_file=CodeFile(
            path="main.py",
            content="print('hello')",
        ),
        other_file_paths=(),
    )

    context = asdict(source_with_non_finite_value)
    context["test_value"] = math.nan

    with pytest.raises(ValueError, match="Out of range float values"):
        json.dumps(context, allow_nan=False)



def test_feedback_order_is_preserved(
    builder,
    rubric,
    resolved_source,
):
    """Diagnostic notes should remain in reporting order."""
    notes = ("First failure", "Second failure", "Third failure")

    prompt = builder.build(
        rubric,
        rubric.feedback_guidance,
        notes,
        resolved_source,
    )

    context_json = prompt.split("GRADING_CONTEXT_JSON:\n", 1)[1]
    context = json.loads(context_json)

    assert context["functional_test_feedback"] == list(notes)

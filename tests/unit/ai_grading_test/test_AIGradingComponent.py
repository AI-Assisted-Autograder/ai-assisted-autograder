import pytest

from ai_grading.AIGradingComponent import (
    AIGradingComponent,
    RetryExhaustedError,
    RetryPolicy,
)
from ai_grading.models import (
    CodeFile,
    FeedbackRequest,
    FeedbackResponse,
    GradingTarget,
    ResolvedSource,
    Rubric,
    TargetKind,
)
from ai_grading.ILLMProvider.ILLMProvider import ProviderError


# ---------------------------------------------------------
# Helper classes/functions used by multiple tests
# ---------------------------------------------------------

class FakeProvider:
    """Simple fake LLM provider so tests don't call a real API."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    @property
    def name(self):
        return "fake-provider"

    @property
    def model(self):
        return "fake-model"

    def generate_feedback(self, req):
        self.calls += 1

        if self.error:
            raise self.error

        return self.response

    def estimated_cost(self, req):
        return None


def make_request():
    """Creates a valid request that can be reused by the tests."""

    target = GradingTarget(
        identifier="test_method",
        kind=TargetKind.METHOD,
        max_score=10,
    )

    rubric = Rubric(
        criteria_text="Code should work correctly",
    )

    source = ResolvedSource(
        source_file=CodeFile(
            path="solution.py",
            content="def test_method(): pass",
        )
    )

    return FeedbackRequest(
        target=target,
        rubric=rubric,
        resolved=source,
    )


def make_response(score=8):
    """Creates a valid fake provider response."""

    return FeedbackResponse(
        provider="fake-provider",
        model_name="fake-model",
        prompt_version="v1",
        feedback_text="The solution works correctly.",
        suggested_score=score,
    )


# ---------------------------------------------------------
# RetryPolicy tests
# ---------------------------------------------------------

# Tests that RetryPolicy rejects zero or negative retry attempts.
def test_retry_policy_rejects_invalid_attempts():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)

# Tests that RetryPolicy requires the retry multiplier to be at least 1.
def test_retry_policy_rejects_invalid_multiplier():
    with pytest.raises(ValueError):
        RetryPolicy(multiplier=0.5)


# ---------------------------------------------------------
# AIGradingComponent initialization tests
# ---------------------------------------------------------

# Tests that the grading component cannot be created without any providers.
def test_component_requires_at_least_one_provider():
    with pytest.raises(ValueError):
        AIGradingComponent([])


# ---------------------------------------------------------
# Basic grading tests
# ---------------------------------------------------------

# Tests that grade() successfully calls a provider and returns its response.
def test_grade_returns_provider_response():
    response = make_response()
    provider = FakeProvider(response=response)

    component = AIGradingComponent([provider])

    result = component.grade(make_request())

    assert result.selected == response
    assert len(result.responses) == 1

# Tests that the first configured provider is selected when no selector is provided.
def test_grade_uses_first_provider_when_no_selector():
    response1 = make_response(score=7)
    response2 = make_response(score=9)

    provider1 = FakeProvider(response=response1)
    provider2 = FakeProvider(response=response2)

    component = AIGradingComponent([provider1, provider2])

    result = component.grade(make_request())

    assert result.selected == response1


# ---------------------------------------------------------
# Selector tests
# ---------------------------------------------------------

# Tests that a custom selector can choose which provider response is selected.
def test_grade_uses_configured_selector():
    response1 = make_response(score=7)
    response2 = make_response(score=9)

    provider1 = FakeProvider(response=response1)
    provider2 = FakeProvider(response=response2)

    component = AIGradingComponent(
        [provider1, provider2],
        selector=lambda responses: 1,
    )

    result = component.grade(make_request())

    assert result.selected == response2

# Tests that grade() rejects a selector that returns an invalid response index.
def test_selector_must_return_valid_index():
    response = make_response()
    provider = FakeProvider(response=response)

    component = AIGradingComponent(
        [provider],
        selector=lambda responses: 5,
    )

    with pytest.raises(RuntimeError):
        component.grade(make_request())


# ---------------------------------------------------------
# Provider response validation tests
# ---------------------------------------------------------

# Tests that the component rejects a provider response belonging to a different rubric.
def test_rejects_mismatched_rubric_id():
    response = FeedbackResponse(
        provider="fake-provider",
        model_name="fake-model",
        prompt_version="v1",
        feedback_text="Feedback",
        suggested_score=8,
        rubric_id="different-rubric",
    )

    provider = FakeProvider(response=response)
    component = AIGradingComponent([provider])

    with pytest.raises(ProviderError) as error:
        component.grade(make_request())

    assert error.value.code == "invalid_response"

# Tests that the component rejects a provider score higher than the target's maximum score.
def test_rejects_score_above_target_maximum():
    response = make_response(score=11)
    provider = FakeProvider(response=response)

    component = AIGradingComponent([provider])

    with pytest.raises(ProviderError) as error:
        component.grade(make_request())

    assert error.value.code == "invalid_response"


# ---------------------------------------------------------
# Retry tests
# ---------------------------------------------------------

# Tests that a retryable provider error causes the provider to be called again.
def test_retryable_provider_error_is_retried():
    response = make_response()

    provider = FakeProvider()

    calls = 0

    def generate(req):
        nonlocal calls
        calls += 1

        if calls == 1:
            raise ProviderError(
                "temporary_error",
                "Temporary failure",
                retryable=True,
            )

        return response

    provider.generate_feedback = generate

    component = AIGradingComponent(
        [provider],
        retry_policy=RetryPolicy(
            max_attempts=2,
            initial_delay=0,
        ),
    )

    result = component.grade(make_request())

    assert result.selected == response
    assert calls == 2

# Tests that a non-retryable provider error immediately stops the grading attempt.
def test_non_retryable_error_is_not_retried():
    provider = FakeProvider(
        error=ProviderError(
            "permanent_error",
            "Permanent failure",
            retryable=False,
        )
    )

    component = AIGradingComponent(
        [provider],
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay=0,
        ),
    )

    with pytest.raises(ProviderError):
        component.grade(make_request())

    assert provider.calls == 1

# Tests that repeated retryable failures eventually produce RetryExhaustedError.
def test_retry_exhaustion_raises_retry_exhausted_error():
    provider = FakeProvider(
        error=ProviderError(
            "temporary_error",
            "Temporary failure",
            retryable=True,
        )
    )

    component = AIGradingComponent(
        [provider],
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay=0,
        ),
    )

    with pytest.raises(RetryExhaustedError) as error:
        component.grade(make_request())

    assert provider.calls == 3
    assert error.value.attempts == 3


# ---------------------------------------------------------
# Comparison tests
# ---------------------------------------------------------

# Tests that the difference between the highest and lowest provider scores is calculated.
def test_score_spread_is_calculated():
    response1 = make_response(score=6)
    response2 = make_response(score=9)

    provider1 = FakeProvider(response=response1)
    provider2 = FakeProvider(response=response2)

    component = AIGradingComponent([provider1, provider2])

    result = component.grade(make_request())

    assert result.comparison.score_spread == 3

# Tests that score_spread is None when there are not at least two scores to compare.
def test_score_spread_is_none_with_one_score():
    response = make_response(score=8)
    provider = FakeProvider(response=response)

    component = AIGradingComponent([provider])

    result = component.grade(make_request())

    assert result.comparison.score_spread is None


import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from google.genai import types

gemini_module = importlib.import_module(
    "ai_grading.ILLMProvider.GeminiProvider"
)

GeminiProvider = gemini_module.GeminiProvider
ProviderError = gemini_module.ProviderError

from ai_grading.models import (
    AnalysisConfig,
    CodeFile,
    Evidence,
    EvidenceSource,
    FeedbackRequest,
    FeedbackResponse,
    GradingItem,
    GradingTarget,
    ResolvedSource,
    Rubric,
    TargetKind,
)


def make_request(analysis=None, evidence=()):
    """Build a valid request without external dependencies."""
    return FeedbackRequest(
        target=GradingTarget(
            identifier="example",
            kind=TargetKind.METHOD,
            max_score=5.0 if analysis and analysis.behavior == "ai_grading" else None,
        ),
        rubric=Rubric(criteria_text="Evaluate correctness and code quality."),
        resolved=ResolvedSource(
            source_file=CodeFile(
                path="solution.py",
                content="def example(): return 1",
            )
        ),
        analysis=analysis or AnalysisConfig(),
        evidence=tuple(evidence),
    )


def make_builder():
    builder = MagicMock()
    builder.prepare.return_value = SimpleNamespace(
        context="Test prompt",
        system_instruction="Test system instruction",
        version="v1",
    )
    return builder


def make_response(
    *,
    output="{}",
    candidates=None,
    finish_reason=None,
    usage=None,
    prompt_feedback=None,
):
    """Build a fake Gemini response."""
    if candidates is None:
        candidate = SimpleNamespace(
            finish_reason=finish_reason or types.FinishReason.STOP,
            content=SimpleNamespace(
                parts=[
                    SimpleNamespace(text="thinking...", thought=True),
                    SimpleNamespace(text=output, thought=False),
                ]
            ),
        )
        candidates = [candidate]

    return SimpleNamespace(
        model_version="gemini-test",
        usage_metadata=usage or SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=40,
            thoughts_token_count=10,
            cached_content_token_count=20,
        ),
        prompt_feedback=prompt_feedback,
        candidates=candidates,
    )


def make_feedback_response():
    return FeedbackResponse(
        provider="GOOGLE",
        model_name="gemini-test",
        prompt_version="v1",
        feedback_text="Good work.",
        input_tokens=100,
        output_tokens=50,
    )


def make_provider(client=None, builder=None):
    return GeminiProvider(
        model="gemini-test",
        client=client or MagicMock(),
        builder=builder or make_builder(),
    )


def mock_success(monkeypatch, provider, output="{}"):
    """Configure a successful fake Gemini response."""
    response = make_response(output=output)
    provider._client.models.generate_content.return_value = response

    parsed = make_feedback_response()
    parse_mock = MagicMock(return_value=parsed)
    monkeypatch.setattr(gemini_module, "parse_feedback", parse_mock)

    return response, parsed, parse_mock


# --------------------------------------------------
# Initialization and configuration
# --------------------------------------------------

def test_initializes_with_valid_configuration():
    provider = make_provider()

    assert provider.name == "GOOGLE"
    assert provider.model == "gemini-test"
    assert provider._owns_client is False


def test_rejects_missing_model():
    with pytest.raises(ValueError, match="model must be configured"):
        GeminiProvider(client=MagicMock())


def test_rejects_empty_model():
    with pytest.raises(ValueError, match="model must be configured"):
        GeminiProvider(model="   ", client=MagicMock())


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        GeminiProvider(
            model="gemini-test",
            client=MagicMock(),
            timeout_seconds=timeout,
        )


def test_rejects_invalid_max_output_tokens():
    with pytest.raises(ValueError, match="max_output_tokens"):
        GeminiProvider(
            model="gemini-test",
            client=MagicMock(),
            max_output_tokens=0,
        )


def test_requires_api_key_without_injected_client(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(ValueError, match="API key"):
        GeminiProvider(model="gemini-test")


def test_uses_environment_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-api-key")

    client = MagicMock()
    monkeypatch.setattr(gemini_module.genai, "Client", lambda **kwargs: client)

    provider = GeminiProvider(model="gemini-test")

    assert provider._client is client
    assert provider._owns_client is True


def test_injected_client_does_not_consult_environment_credentials(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    client = MagicMock()

    provider = GeminiProvider(
        model="gemini-test",
        client=client,
    )

    assert provider._client is client
    assert provider._owns_client is False


# --------------------------------------------------
# Client lifecycle
# --------------------------------------------------

def test_close_does_not_close_injected_client():
    client = MagicMock()
    provider = make_provider(client=client)

    provider.close()

    client.close.assert_not_called()


def test_close_closes_owned_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setenv("GEMINI_API_KEY", "fake-api-key")
    monkeypatch.setattr(gemini_module.genai, "Client", lambda **kwargs: client)

    provider = GeminiProvider(model="gemini-test")

    provider.close()

    client.close.assert_called_once()


# --------------------------------------------------
# Successful feedback generation
# --------------------------------------------------

def test_generate_feedback_returns_parsed_response(monkeypatch):
    provider = make_provider()
    request = make_request()

    _, parsed, parse_mock = mock_success(monkeypatch, provider)

    result = provider.generate_feedback(request)

    assert result.provider == "GOOGLE"
    assert result.feedback_text == "Good work."
    parse_mock.assert_called_once()

    provider._client.models.generate_content.assert_called_once()


def test_generate_feedback_uses_configured_model():
    provider = make_provider()
    request = make_request()

    provider._client.models.generate_content.return_value = make_response()

    monkeypatch = pytest.MonkeyPatch()
    try:
        mock_success(monkeypatch, provider)
        provider.generate_feedback(request)
    finally:
        monkeypatch.undo()

    kwargs = provider._client.models.generate_content.call_args.kwargs

    assert kwargs["model"] == "gemini-test"
    assert kwargs["contents"] == "Test prompt"


def test_generate_feedback_excludes_thought_parts(monkeypatch):
    provider = make_provider()
    request = make_request()

    _, _, parse_mock = mock_success(
        monkeypatch,
        provider,
        output='{"feedback": "success"}',
    )

    provider.generate_feedback(request)

    parsed_output = parse_mock.call_args.args[0]

    assert "thinking..." not in parsed_output
    assert "success" in parsed_output


# --------------------------------------------------
# Token usage
# --------------------------------------------------

def test_records_token_usage(monkeypatch):
    provider = make_provider()
    request = make_request()

    mock_success(monkeypatch, provider)

    result = provider.generate_feedback(request)

    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cached_input_tokens == 20
    assert result.reasoning_tokens == 10


def test_output_tokens_include_reasoning_tokens(monkeypatch):
    provider = make_provider()
    request = make_request()

    mock_success(monkeypatch, provider)

    result = provider.generate_feedback(request)

    assert result.output_tokens == 40 + 10


def test_estimated_cost_is_unknown():
    provider = make_provider()

    assert provider.estimated_cost(make_request()) is None


# --------------------------------------------------
# Request schema selection
# --------------------------------------------------

def test_code_feedback_uses_plain_text_response_format():
    provider = make_provider()
    provider._client.models.generate_content.return_value = make_response()

    monkeypatch = pytest.MonkeyPatch()
    try:
        mock_success(monkeypatch, provider)
        provider.generate_feedback(make_request())
    finally:
        monkeypatch.undo()

    config = provider._client.models.generate_content.call_args.kwargs["config"]

    assert config.response_mime_type == "text/plain"
    assert config.response_json_schema is None


def test_ai_grading_uses_structured_response_schema():
    provider = make_provider()

    analysis = AnalysisConfig(
        behavior="ai_grading",
        grading_items=(
            GradingItem(
                id="correctness",
                requirement="Implementation is correct.",
                points=5.0,
            ),
        ),
    )

    provider._client.models.generate_content.return_value = make_response()

    monkeypatch = pytest.MonkeyPatch()
    try:
        mock_success(monkeypatch, provider)
        provider.generate_feedback(make_request(analysis=analysis))
    finally:
        monkeypatch.undo()

    config = provider._client.models.generate_content.call_args.kwargs["config"]

    assert config.response_mime_type == "application/json"
    assert "feedback_text" not in config.response_json_schema["properties"]
    assert config.response_json_schema["required"] == ["items"]


def test_functional_feedback_schema_contains_failed_evidence():
    provider = make_provider()

    evidence = (
        Evidence(
            id="failure-1",
            kind="runtime",
            source="gradescope-results",
            summary="Test failed",
            status="fail",
        ),
    )

    analysis = AnalysisConfig(
        behavior="functional_test_feedback",
        functional_result_source="gradescope-results",
        evidence_sources=(
            EvidenceSource(
                format="gradescope",
                path="results.json",
                label="gradescope-results",
            ),
        ),
    )

    provider._client.models.generate_content.return_value = make_response()

    monkeypatch = pytest.MonkeyPatch()
    try:
        mock_success(monkeypatch, provider)
        provider.generate_feedback(
            make_request(analysis=analysis, evidence=evidence)
        )
    finally:
        monkeypatch.undo()

    config = provider._client.models.generate_content.call_args.kwargs["config"]

    failure_schema = config.response_json_schema["properties"]["failure_feedback"]

    assert failure_schema["minItems"] == 1
    assert failure_schema["maxItems"] == 1
    assert failure_schema["items"]["properties"]["evidence_id"]["enum"] == [
        "failure-1"
    ]


# --------------------------------------------------
# Error handling
# --------------------------------------------------

def test_converts_timeout_exception_to_provider_error():
    provider = make_provider()
    provider._client.models.generate_content.side_effect = httpx.TimeoutException(
        "Request timed out"
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "timeout"
    assert exc.value.retryable is True


def test_converts_transport_exception_to_provider_error():
    provider = make_provider()
    provider._client.models.generate_content.side_effect = httpx.ConnectError(
        "Connection failed"
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "provider_unavailable"
    assert exc.value.retryable is True


def test_rejects_response_without_candidates():
    provider = make_provider()
    provider._client.models.generate_content.return_value = make_response(
        candidates=[]
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "invalid_provider_response"


def test_rejects_multiple_candidates():
    provider = make_provider()

    candidate = make_response().candidates[0]

    provider._client.models.generate_content.return_value = make_response(
        candidates=[candidate, candidate]
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "invalid_provider_response"


def test_rejects_incomplete_response():
    provider = make_provider()

    candidate = SimpleNamespace(
        finish_reason=types.FinishReason.MAX_TOKENS,
        content=SimpleNamespace(parts=[]),
    )

    provider._client.models.generate_content.return_value = make_response(
        candidates=[candidate]
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "incomplete_provider_response"


@pytest.mark.parametrize(
    "finish_reason",
    [
        types.FinishReason.SAFETY,
        types.FinishReason.RECITATION,
        types.FinishReason.BLOCKLIST,
        types.FinishReason.PROHIBITED_CONTENT,
        types.FinishReason.SPII,
    ],
)
def test_rejects_safety_and_policy_refusals(finish_reason):
    provider = make_provider()

    candidate = SimpleNamespace(
        finish_reason=finish_reason,
        content=SimpleNamespace(parts=[]),
    )

    provider._client.models.generate_content.return_value = make_response(
        candidates=[candidate]
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "provider_refusal"

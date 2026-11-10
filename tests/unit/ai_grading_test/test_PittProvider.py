
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

pitt_module = importlib.import_module(
    "ai_grading.ILLMProvider.PittProvider"
)

PittProvider = pitt_module.PittProvider
ProviderError = pitt_module.ProviderError

from ai_grading.models import (
    AnalysisConfig,
    CodeFile,
    FeedbackRequest,
    FeedbackResponse,
    GradingTarget,
    ResolvedSource,
    Rubric,
    TargetKind,
)


def make_request(temperature=None):
    return FeedbackRequest(
        target=GradingTarget(
            identifier="example",
            kind=TargetKind.METHOD,
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
        analysis=AnalysisConfig(temperature=temperature),
    )


def make_builder():
    builder = MagicMock()
    builder.prepare.return_value = SimpleNamespace(
        context="Test prompt",
        system_instruction="Test system instruction",
        version="v1",
    )
    return builder


def make_provider(client=None, builder=None):
    provider = PittProvider(
        model="test-model",
        api_key="fake-api-key",
        builder=builder or make_builder(),
    )
    if client is not None:
        provider._client.close()
        provider._client = client
    return provider


def make_api_response(
    *,
    content="Test feedback",
    status_code=200,
    usage=None,
    finish_reason="stop",
    refusal=None,
    tool_calls=None,
    choices=None,
    model="test-model",
):
    if choices is None:
        choices = [
            {
                "message": {
                    "content": content,
                    "refusal": refusal,
                    "tool_calls": tool_calls,
                },
                "finish_reason": finish_reason,
            }
        ]

    response = MagicMock()
    response.status_code = status_code
    response.is_success = 200 <= status_code < 300
    response.json.return_value = {
        "model": model,
        "choices": choices,
        "usage": usage or {
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "prompt_tokens_details": {
                "cached_tokens": 20,
            },
            "completion_tokens_details": {
                "reasoning_tokens": 10,
            },
        },
    }
    return response


def make_feedback_response(provider="PITT"):
    return FeedbackResponse(
        provider=provider,
        model_name="test-model",
        prompt_version="v1",
        feedback_text="Test feedback",
        input_tokens=100,
        output_tokens=40,
    )


def mock_success(monkeypatch, provider):
    response = make_api_response()

    post_mock = MagicMock(return_value=response)
    provider._client.post = post_mock

    parsed = make_feedback_response(provider.name)
    parse_mock = MagicMock(return_value=parsed)

    monkeypatch.setattr(
        pitt_module,
        "parse_feedback",
        parse_mock,
    )

    return response, parsed, parse_mock, post_mock


# --------------------------------------------------
# Initialization and configuration
# --------------------------------------------------

def test_initializes_with_valid_configuration():
    provider = make_provider()

    assert provider.name == "PITT"
    assert provider.model == "test-model"
    assert provider._max_output_tokens == 16_384

    provider.close()


def test_rejects_missing_model():
    with pytest.raises(ValueError, match="model must be configured"):
        PittProvider(
            model="",
            api_key="fake-api-key",
        )


def test_rejects_empty_model():
    with pytest.raises(ValueError, match="model must be configured"):
        PittProvider(
            model="   ",
            api_key="fake-api-key",
        )


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("inf"), float("nan")],
)
def test_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        PittProvider(
            model="test-model",
            api_key="fake-api-key",
            timeout_seconds=timeout,
        )


@pytest.mark.parametrize(
    "max_tokens",
    [0, -1, 1.5, True],
)
def test_rejects_invalid_max_output_tokens(max_tokens):
    with pytest.raises(ValueError, match="max_output_tokens"):
        PittProvider(
            model="test-model",
            api_key="fake-api-key",
            max_output_tokens=max_tokens,
        )


def test_requires_api_key(monkeypatch):
    monkeypatch.delenv("PITT_API_KEY", raising=False)

    with pytest.raises(ProviderError) as exc:
        PittProvider(model="test-model")

    assert exc.value.code == "backend_auth_unavailable"


def test_reads_api_key_from_environment(monkeypatch):
    monkeypatch.setenv("PITT_API_KEY", "fake-environment-key")

    provider = PittProvider(model="test-model")

    assert provider._client.headers["x-portkey-api-key"] == (
        "fake-environment-key"
    )

    provider.close()


def test_explicit_api_key_overrides_environment(monkeypatch):
    monkeypatch.setenv("PITT_API_KEY", "environment-key")

    provider = PittProvider(
        model="test-model",
        api_key="explicit-key",
    )

    assert provider._client.headers["x-portkey-api-key"] == "explicit-key"

    provider.close()


def test_configures_http_client_safely():
    provider = make_provider()

    assert provider._client.trust_env is False
    assert provider._client.follow_redirects is False
    assert provider._client.headers["x-portkey-cache-force-refresh"] == "true"

    provider.close()


def test_close_closes_http_client():
    provider = make_provider()

    provider.close()

    assert provider._client.is_closed


# --------------------------------------------------
# Successful feedback generation
# --------------------------------------------------

def test_generate_feedback_returns_parsed_response(monkeypatch):
    provider = make_provider()
    request = make_request()

    _, _, parse_mock, post_mock = mock_success(monkeypatch, provider)

    result = provider.generate_feedback(request)

    assert result.provider == "PITT"
    assert result.feedback_text == "Test feedback"

    post_mock.assert_called_once()
    parse_mock.assert_called_once()

    provider.close()


def test_sends_correct_portkey_request(monkeypatch):
    provider = make_provider()
    request = make_request()

    mock_success(monkeypatch, provider)

    provider.generate_feedback(request)

    args = provider._client.post.call_args

    assert args.args[0] == (
        "https://api.portkey.ai/v1/chat/completions"
    )

    payload = args.kwargs["json"]

    assert payload["model"] == "test-model"
    assert payload["stream"] is False
    assert payload["max_tokens"] == 16_384

    assert payload["messages"] == [
        {
            "role": "system",
            "content": "Test system instruction",
        },
        {
            "role": "user",
            "content": "Test prompt",
        },
    ]

    provider.close()


def test_includes_temperature_when_configured(monkeypatch):
    provider = make_provider()
    request = make_request(temperature=0.5)

    mock_success(monkeypatch, provider)

    provider.generate_feedback(request)

    payload = provider._client.post.call_args.kwargs["json"]

    assert payload["temperature"] == 0.5

    provider.close()


def test_omits_temperature_when_not_configured(monkeypatch):
    provider = make_provider()
    request = make_request()

    mock_success(monkeypatch, provider)

    provider.generate_feedback(request)

    payload = provider._client.post.call_args.kwargs["json"]

    assert "temperature" not in payload

    provider.close()


def test_uses_configured_provider_name(monkeypatch):
    provider = PittProvider(
        model="test-model",
        api_key="fake-api-key",
        provider="CUSTOM",
        builder=make_builder(),
    )

    response = make_api_response()
    provider._client.post = MagicMock(return_value=response)

    monkeypatch.setattr(
        pitt_module,
        "parse_feedback",
        MagicMock(return_value=make_feedback_response("CUSTOM")),
    )

    result = provider.generate_feedback(make_request())

    assert result.provider == "CUSTOM"
    assert pitt_module.parse_feedback.call_args.kwargs["provider"] == "CUSTOM"

    provider.close()


# --------------------------------------------------
# Token usage
# --------------------------------------------------

def test_records_token_usage(monkeypatch):
    provider = make_provider()

    mock_success(monkeypatch, provider)

    result = provider.generate_feedback(make_request())

    assert result.input_tokens == 100
    assert result.output_tokens == 40
    assert result.cached_input_tokens == 20
    assert result.reasoning_tokens == 10

    provider.close()


def test_uses_model_returned_by_api(monkeypatch):
    provider = make_provider()

    response = make_api_response(model="actual-model")
    provider._client.post = MagicMock(return_value=response)

    parse_mock = MagicMock(
        return_value=make_feedback_response()
    )
    monkeypatch.setattr(pitt_module, "parse_feedback", parse_mock)

    provider.generate_feedback(make_request())

    assert parse_mock.call_args.kwargs["model_name"] == "actual-model"

    provider.close()


def test_estimated_cost_is_unknown():
    provider = make_provider()

    assert provider.estimated_cost(make_request()) is None

    provider.close()


# --------------------------------------------------
# HTTP error handling
# --------------------------------------------------

def test_converts_timeout_to_provider_error():
    provider = make_provider()

    provider._client.post = MagicMock(
        side_effect=httpx.TimeoutException("Request timed out")
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "timeout"
    assert exc.value.retryable is True

    provider.close()


def test_converts_transport_error_to_provider_error():
    provider = make_provider()

    provider._client.post = MagicMock(
        side_effect=httpx.ConnectError("Connection failed")
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "provider_unavailable"
    assert exc.value.retryable is True

    provider.close()


@pytest.mark.parametrize("status_code", [401, 403])
def test_handles_authentication_errors(status_code):
    provider = make_provider()

    provider._client.post = MagicMock(
        return_value=make_api_response(status_code=status_code)
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "backend_auth_unavailable"
    assert exc.value.retryable is False

    provider.close()


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (400, False),
        (408, True),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
    ],
)
def test_handles_http_errors(status_code, retryable):
    provider = make_provider()

    provider._client.post = MagicMock(
        return_value=make_api_response(status_code=status_code)
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "provider_error"
    assert exc.value.retryable is retryable

    provider.close()


# --------------------------------------------------
# Response validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "response_data",
    [
        None,
        [],
        "invalid",
    ],
)
def test_rejects_non_object_response(response_data):
    provider = make_provider()

    response = make_api_response()
    response.json.return_value = response_data

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "invalid_provider_response"

    provider.close()


def test_rejects_invalid_json():
    provider = make_provider()

    response = make_api_response()
    response.json.side_effect = ValueError("Invalid JSON")

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "invalid_provider_response"

    provider.close()


def test_rejects_missing_choices():
    provider = make_provider()

    response = make_api_response()
    response.json.return_value = {
        "model": "test-model",
        "usage": {},
        "choices": [],
    }

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "invalid_provider_response"

    provider.close()


def test_rejects_multiple_choices():
    provider = make_provider()

    choice = {
        "message": {"content": "Feedback"},
        "finish_reason": "stop",
    }

    response = make_api_response(choices=[choice, choice])

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "invalid_provider_response"

    provider.close()


def test_rejects_missing_message():
    provider = make_provider()

    response = make_api_response(
        choices=[
            {
                "finish_reason": "stop",
            }
        ]
    )

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "invalid_provider_response"

    provider.close()


def test_rejects_non_text_feedback():
    provider = make_provider()

    response = make_api_response(
        choices=[
            {
                "message": {
                    "content": None,
                },
                "finish_reason": "stop",
            }
        ]
    )

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "invalid_provider_response"

    provider.close()


# --------------------------------------------------
# Refusals and incomplete responses
# --------------------------------------------------

def test_rejects_refusal():
    provider = make_provider()

    response = make_api_response(
        refusal="I cannot help with that request."
    )

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "provider_refusal"

    provider.close()


def test_rejects_content_filter():
    provider = make_provider()

    response = make_api_response(finish_reason="content_filter")

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "provider_refusal"

    provider.close()


def test_rejects_incomplete_generation():
    provider = make_provider()

    response = make_api_response(finish_reason="length")

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "incomplete_provider_response"

    provider.close()


def test_rejects_tool_calls():
    provider = make_provider()

    response = make_api_response(
        tool_calls=[
            {
                "id": "tool-1",
                "type": "function",
            }
        ]
    )

    provider._client.post = MagicMock(return_value=response)

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(make_request())

    assert exc.value.code == "incomplete_provider_response"

    provider.close()

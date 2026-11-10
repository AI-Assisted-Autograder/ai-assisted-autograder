
import importlib
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import GoogleAuthError
from google.auth import impersonated_credentials

vertex_module = importlib.import_module(
    "ai_grading.ILLMProvider.VertexProvider"
)

VertexProvider = vertex_module.VertexProvider
ProviderError = vertex_module.ProviderError


@pytest.fixture
def credentials():
    creds = MagicMock(
        spec=impersonated_credentials.Credentials
    )
    creds.service_account_email = "grader@example.iam.gserviceaccount.com"
    return creds


@pytest.fixture
def mock_client():
    client = MagicMock()
    return client


@pytest.fixture
def configured_provider(credentials, mock_client):
    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ),
        patch.object(
            vertex_module,
            "GeminiProvider",
        ) as gemini_mock,
    ):
        gemini_instance = gemini_mock.return_value
        gemini_instance.model = "gemini-test-model"
        gemini_instance.generate_feedback.return_value = "feedback"
        gemini_instance.estimated_cost.return_value = None

        provider = VertexProvider(
            model="gemini-test-model",
            project="test-project",
            location="us-central1",
            service_account="grader@example.iam.gserviceaccount.com",
        )

        yield provider, gemini_mock, gemini_instance


# --------------------------------------------------
# Initialization and configuration
# --------------------------------------------------

def test_initializes_with_valid_configuration(configured_provider):
    provider, _, _ = configured_provider

    assert provider.name == "GOOGLE"
    assert provider.model == "gemini-test-model"
    assert provider._client is not None

    provider.close()


def test_uses_google_application_default_credentials(
    credentials,
    mock_client,
):
    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ) as auth_mock,
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ),
        patch.object(vertex_module, "GeminiProvider"),
    ):
        provider = VertexProvider(
            model="gemini-test-model",
            project="test-project",
            location="us-central1",
            service_account="grader@example.iam.gserviceaccount.com",
        )

        auth_mock.assert_called_once_with(
            scopes=[
                "https://www.googleapis.com/auth/cloud-platform"
            ]
        )

        provider.close()


def test_requires_impersonated_credentials(mock_client):
    credentials = MagicMock()

    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ) as client_mock,
    ):
        with pytest.raises(ProviderError) as exc:
            VertexProvider(
                model="gemini-test-model",
                project="test-project",
                location="us-central1",
                service_account="grader@example.iam.gserviceaccount.com",
            )

        assert exc.value.code == "backend_auth_unavailable"
        client_mock.assert_not_called()


def test_rejects_wrong_service_account(
    credentials,
    mock_client,
):
    credentials.service_account_email = "wrong@example.iam.gserviceaccount.com"

    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ) as client_mock,
    ):
        with pytest.raises(ProviderError) as exc:
            VertexProvider(
                model="gemini-test-model",
                project="test-project",
                location="us-central1",
                service_account="grader@example.iam.gserviceaccount.com",
            )

        assert exc.value.code == "backend_auth_unavailable"
        client_mock.assert_not_called()


def test_does_not_use_api_key_or_legacy_configuration(
    credentials,
    mock_client,
    monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("GEMINI_MODEL", "legacy-model")

    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ) as client_mock,
        patch.object(vertex_module, "GeminiProvider") as gemini_mock,
    ):
        gemini_mock.return_value.model = "gemini-test-model"

        provider = VertexProvider(
            model="gemini-test-model",
            project="test-project",
            location="us-central1",
            service_account="grader@example.iam.gserviceaccount.com",
        )

        client_kwargs = client_mock.call_args.kwargs

        assert client_kwargs["vertexai"] is True
        assert client_kwargs["credentials"] is credentials
        assert "api_key" not in client_kwargs

        gemini_kwargs = gemini_mock.call_args.kwargs

        assert gemini_kwargs["model"] == "gemini-test-model"
        assert gemini_kwargs["client"] is mock_client

        provider.close()


# --------------------------------------------------
# Vertex client configuration
# --------------------------------------------------

def test_configures_vertex_client(
    credentials,
    mock_client,
):
    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ) as client_mock,
        patch.object(vertex_module, "GeminiProvider"),
    ):
        provider = VertexProvider(
            model="gemini-test-model",
            project="test-project",
            location="us-central1",
            service_account="grader@example.iam.gserviceaccount.com",
        )

        kwargs = client_mock.call_args.kwargs

        assert kwargs["vertexai"] is True
        assert kwargs["credentials"] is credentials
        assert kwargs["project"] == "test-project"
        assert kwargs["location"] == "us-central1"

        http_options = kwargs["http_options"]

        assert http_options.api_version == "v1"
        assert http_options.base_url == (
            "https://us-central1-aiplatform.googleapis.com"
        )

        provider.close()


def test_uses_global_vertex_endpoint(
    credentials,
    mock_client,
):
    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ) as client_mock,
        patch.object(vertex_module, "GeminiProvider"),
    ):
        provider = VertexProvider(
            model="gemini-test-model",
            project="test-project",
            location="global",
            service_account="grader@example.iam.gserviceaccount.com",
        )

        http_options = client_mock.call_args.kwargs["http_options"]

        assert http_options.base_url == (
            "https://aiplatform.googleapis.com"
        )

        provider.close()


def test_disables_environment_proxy_and_redirects(
    credentials,
    mock_client,
):
    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ) as client_mock,
        patch.object(vertex_module, "GeminiProvider"),
    ):
        provider = VertexProvider(
            model="gemini-test-model",
            project="test-project",
            location="us-central1",
            service_account="grader@example.iam.gserviceaccount.com",
        )

        http_options = client_mock.call_args.kwargs["http_options"]

        assert http_options.client_args == {
            "trust_env": False,
            "follow_redirects": False,
        }

        assert http_options.async_client_args == {
            "trust_env": False,
            "follow_redirects": False,
        }

        provider.close()


def test_configures_timeout_and_retries(
    credentials,
    mock_client,
):
    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ) as client_mock,
        patch.object(vertex_module, "GeminiProvider"),
    ):
        provider = VertexProvider(
            model="gemini-test-model",
            project="test-project",
            location="us-central1",
            service_account="grader@example.iam.gserviceaccount.com",
            timeout_seconds=30,
        )

        http_options = client_mock.call_args.kwargs["http_options"]

        assert http_options.timeout == 30_000
        assert http_options.retry_options.attempts == 1

        provider.close()


# --------------------------------------------------
# Gemini provider delegation
# --------------------------------------------------

def test_delegates_feedback_generation(configured_provider):
    provider, _, gemini_instance = configured_provider

    request = MagicMock()

    result = provider.generate_feedback(request)

    gemini_instance.generate_feedback.assert_called_once_with(request)
    assert result == "feedback"

    provider.close()


def test_delegates_estimated_cost(configured_provider):
    provider, _, gemini_instance = configured_provider

    request = MagicMock()

    result = provider.estimated_cost(request)

    gemini_instance.estimated_cost.assert_called_once_with(request)
    assert result is None

    provider.close()


def test_uses_gemini_provider_model(configured_provider):
    provider, _, _ = configured_provider

    assert provider.model == "gemini-test-model"

    provider.close()


# --------------------------------------------------
# Authentication error handling
# --------------------------------------------------

def test_converts_authentication_error_during_initialization():
    with patch.object(
        vertex_module.google.auth,
        "default",
        side_effect=GoogleAuthError("Authentication failed"),
    ):
        with pytest.raises(ProviderError) as exc:
            VertexProvider(
                model="gemini-test-model",
                project="test-project",
                location="us-central1",
                service_account="grader@example.iam.gserviceaccount.com",
            )

    assert exc.value.code == "backend_auth_unavailable"


def test_converts_authentication_error_during_generation(
    configured_provider,
):
    provider, _, gemini_instance = configured_provider

    gemini_instance.generate_feedback.side_effect = GoogleAuthError(
        "Authentication expired"
    )

    with pytest.raises(ProviderError) as exc:
        provider.generate_feedback(MagicMock())

    assert exc.value.code == "backend_auth_unavailable"

    provider.close()


def test_converts_client_creation_error(
    credentials,
):
    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            side_effect=RuntimeError("Client creation failed"),
        ),
    ):
        with pytest.raises(ProviderError) as exc:
            VertexProvider(
                model="gemini-test-model",
                project="test-project",
                location="us-central1",
                service_account="grader@example.iam.gserviceaccount.com",
            )

    assert exc.value.code == "backend_auth_unavailable"


# --------------------------------------------------
# Client lifecycle
# --------------------------------------------------

def test_close_closes_client(configured_provider, mock_client):
    provider, _, _ = configured_provider

    provider.close()

    mock_client.close.assert_called_once()
    assert provider._client is None


def test_close_is_safe_when_client_is_missing():
    provider = VertexProvider.__new__(VertexProvider)
    provider._client = None

    provider.close()

    assert provider._client is None


def test_close_can_be_called_multiple_times(
    configured_provider,
    mock_client,
):
    provider, _, _ = configured_provider

    provider.close()
    provider.close()

    mock_client.close.assert_called_once()


def test_closes_client_when_initialization_fails(
    credentials,
    mock_client,
):
    with (
        patch.object(
            vertex_module.google.auth,
            "default",
            return_value=(credentials, None),
        ),
        patch.object(
            vertex_module.genai,
            "Client",
            return_value=mock_client,
        ),
        patch.object(
            vertex_module,
            "GeminiProvider",
            side_effect=RuntimeError("Provider initialization failed"),
        ),
    ):
        with pytest.raises(ProviderError) as exc:
            VertexProvider(
                model="gemini-test-model",
                project="test-project",
                location="us-central1",
                service_account="grader@example.iam.gserviceaccount.com",
            )

    assert exc.value.code == "backend_auth_unavailable"
    mock_client.close.assert_called_once()

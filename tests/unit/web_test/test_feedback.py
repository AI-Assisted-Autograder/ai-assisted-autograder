
import json
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from web.app.feedback import (
    FeedbackInput,
    default_provider_factory,
    error,
    matches_path,
    resolve_input,
)
from ai_grading.models import CodeFile
from ai_grading.ILLMProvider.ILLMProvider import ProviderError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_target(
    *,
    source_path="src/main.py",
    source_globs=None,
    context_globs=(),
    max_source_files=None,
    evidence_sources=(),
):
    """Create a minimal target without loading database configuration."""
    return SimpleNamespace(
        source_path=source_path,
        analysis=SimpleNamespace(
            source_globs=source_globs or (source_path,),
            context_globs=context_globs,
            max_source_files=max_source_files,
            evidence_sources=evidence_sources,
        ),
    )


def make_payload(**overrides):
    """Create a valid feedback input, allowing individual fields to vary."""
    values = {
        "request_id": "request-001",
        "target_id": "target-001",
        "code": "print('hello')",
    }
    values.update(overrides)
    return FeedbackInput(**values)


# ---------------------------------------------------------------------------
# FeedbackInput validation
# ---------------------------------------------------------------------------

def test_valid_code_payload():
    payload = make_payload()

    assert payload.request_id == "request-001"
    assert payload.target_id == "target-001"
    assert payload.code == "print('hello')"
    assert payload.files == []
    assert payload.evidence == []


def test_code_is_required_when_files_are_absent():
    with pytest.raises(ValidationError):
        make_payload(code=None)


def test_code_and_files_cannot_both_be_provided():
    with pytest.raises(ValidationError):
        make_payload(
            code="print('hello')",
            files=[CodeFile(path="src/main.py", content="print('hello')")],
        )


def test_file_only_payload_is_valid():
    payload = make_payload(
        code=None,
        files=[CodeFile(path="src/main.py", content="print('hello')")],
    )

    assert payload.code is None
    assert len(payload.files) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", ""),
        ("request_id", "bad request id"),
        ("request_id", "x" * 129),
        ("target_id", ""),
        ("target_id", "bad/target"),
        ("target_id", "x" * 129),
    ],
)
def test_invalid_identifiers_are_rejected(field, value):
    with pytest.raises(ValidationError):
        make_payload(**{field: value})


def test_unknown_payload_fields_are_rejected():
    with pytest.raises(ValidationError):
        make_payload(unexpected_field="not allowed")


def test_non_string_code_is_rejected():
    with pytest.raises(ValidationError):
        make_payload(code=123)


def test_too_many_files_are_rejected():
    files = [
        CodeFile(path=f"src/file_{i}.py", content="pass")
        for i in range(65)
    ]

    with pytest.raises(ValidationError):
        make_payload(code=None, files=files)


def test_too_many_evidence_items_are_rejected():
    # Evidence objects are intentionally not constructed here: the list limit
    # is enforced by Pydantic before the model-level validation runs.
    with pytest.raises(ValidationError):
        FeedbackInput.model_validate({
            "request_id": "request-001",
            "target_id": "target-001",
            "code": "pass",
            "evidence": [{}] * 257,
        })


# ---------------------------------------------------------------------------
# Path matching
# ---------------------------------------------------------------------------

def test_matches_path_with_exact_pattern():
    assert matches_path("src/main.py", ("src/main.py",))


def test_matches_path_with_nonmatching_pattern():
    assert not matches_path("src/main.py", ("src/other.py",))


def test_matches_path_with_one_of_multiple_patterns():
    assert matches_path(
        "src/main.py",
        ("src/other.py", "src/main.py"),
    )


def test_matches_path_with_no_patterns():
    assert not matches_path("src/main.py", ())


# ---------------------------------------------------------------------------
# Input resolution and target scoping
# ---------------------------------------------------------------------------

def test_resolve_code_uses_configured_target_path():
    payload = make_payload(code="print('hello')")
    target = make_target(source_path="assignments/task.py")

    resolved = resolve_input(payload, target)

    assert resolved.source_file.path == "assignments/task.py"
    assert resolved.source_file.content == "print('hello')"
    assert resolved.context_files == ()


def test_resolve_files_accepts_configured_primary_file():
    payload = make_payload(
        code=None,
        files=[
            CodeFile(path="src/main.py", content="main()"),
        ],
    )
    target = make_target(source_path="src/main.py")

    resolved = resolve_input(payload, target)

    assert resolved.source_file.path == "src/main.py"
    assert resolved.source_file.content == "main()"


def test_resolve_files_rejects_primary_file_outside_target():
    payload = make_payload(
        code=None,
        files=[
            CodeFile(path="other/secret.py", content="secret()"),
        ],
    )
    target = make_target(source_path="src/main.py")

    with pytest.raises(ValueError, match="outside the configured target"):
        resolve_input(payload, target)


def test_resolve_files_allows_configured_context_file():
    payload = make_payload(
        code=None,
        files=[
            CodeFile(path="src/main.py", content="main()"),
            CodeFile(path="src/helper.py", content="helper()"),
        ],
    )
    target = make_target(
        source_path="src/main.py",
        context_globs=("src/helper.py",),
    )

    resolved = resolve_input(payload, target)

    assert resolved.source_file.path == "src/main.py"
    assert len(resolved.context_files) == 1
    assert resolved.context_files[0].path == "src/helper.py"


def test_resolve_files_rejects_unconfigured_context_file():
    payload = make_payload(
        code=None,
        files=[
            CodeFile(path="src/main.py", content="main()"),
            CodeFile(path="outside/helper.py", content="helper()"),
        ],
    )
    target = make_target(source_path="src/main.py")

    with pytest.raises(ValueError, match="outside the configured target"):
        resolve_input(payload, target)


def test_resolve_files_enforces_max_source_file_count():
    payload = make_payload(
        code=None,
        files=[
            CodeFile(path="src/main.py", content="main()"),
            CodeFile(path="src/second.py", content="second()"),
        ],
    )
    target = make_target(
        source_path="src/main.py",
        source_globs=("src/*.py",),
        max_source_files=1,
    )

    with pytest.raises(ValueError, match="primary-file limit"):
        resolve_input(payload, target)


# ---------------------------------------------------------------------------
# Standardized HTTP errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "code,status",
    [
        ("unauthorized", 401),
        ("assignment_forbidden", 403),
        ("invalid_request", 422),
        ("input_too_large", 413),
        ("busy", 429),
        ("backend_error", 500),
    ],
)
def test_error_response_contains_expected_status_and_headers(code, status):
    response = error(code, status, "request-001")

    assert isinstance(response, JSONResponse)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"

    body = json.loads(response.body)
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["request_id"] == "request-001"


def test_error_response_omits_request_id_when_not_provided():
    response = error("unauthorized", 401)

    body = json.loads(response.body)

    assert "request_id" not in body


def test_unknown_error_code_uses_safe_fallback_message():
    response = error("unknown_error", 500)

    body = json.loads(response.body)

    assert body["error"]["code"] == "unknown_error"
    assert body["error"]["message"] == (
        "Backend could not safely finish this request."
    )


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def test_provider_factory_rejects_unsupported_engine():
    config = SimpleNamespace(
        timeout_seconds=10,
        max_output_tokens=100,
    )
    assignment = SimpleNamespace(
        engine="unsupported",
        model="test-model",
        provider="test-provider",
    )

    with pytest.raises(ValueError, match="Unsupported feedback engine"):
        default_provider_factory(config, assignment)


def test_gemini_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    config = SimpleNamespace(
        timeout_seconds=10,
        max_output_tokens=100,
    )
    assignment = SimpleNamespace(
        engine="gemini_api",
        model="test-model",
        provider="gemini",
    )

    # Verify that the missing-key path does not need the Gemini SDK.
    import sys
    from unittest.mock import MagicMock

    module_name = "ai_grading.ILLMProvider.GeminiProvider"
    monkeypatch.setitem(sys.modules, module_name, MagicMock())

    with pytest.raises(ProviderError) as exc_info:
        default_provider_factory(config, assignment)

    assert exc_info.value.code == "backend_auth_unavailable"


def test_gemini_provider_accepts_google_api_key(monkeypatch):
    # Mock the provider module so no network or model SDK is required.
    import sys
    from unittest.mock import MagicMock

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key")

    mock_module = MagicMock()
    mock_provider = MagicMock()
    mock_module.GeminiProvider = mock_provider

    monkeypatch.setitem(
        sys.modules,
        "ai_grading.ILLMProvider.GeminiProvider",
        mock_module,
    )

    config = SimpleNamespace(
        timeout_seconds=10,
        max_output_tokens=100,
    )
    assignment = SimpleNamespace(
        engine="gemini_api",
        model="test-model",
    )

    result = default_provider_factory(config, assignment)

    assert result is mock_provider.return_value
    mock_provider.assert_called_once_with(
        model="test-model",
        timeout_seconds=10,
        max_output_tokens=100,
        api_key="test-api-key",
    )

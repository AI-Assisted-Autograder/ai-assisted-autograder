
import pytest
from datetime import datetime, timedelta, timezone
from pydantic import ValidationError

from web.app.feedback_config import (
    AssignmentAccess,
    AssignmentConfig,
    AssignmentCreateInput,
    BackendConfig,
    RequestRecord,
    RubricConfig,
    State,
    TargetConfig,
    TokenRecord,
    validate_analysis,
    validate_engine_model,
    validate_relative_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_backend_config(**overrides):
    values = {
        "allowed_models": {"gemini_api": ["gemini-test-model"]},
    }
    values.update(overrides)
    return BackendConfig(**values)


def make_request(**overrides):
    values = {
        "request_id": "request-1",
        "target_id": "target-1",
        "engine": "gemini_api",
        "model": "gemini-test-model",
        "digest": "a" * 64,
        "status": "processing",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return RequestRecord(**values)


def make_rubric():
    return RubricConfig(
        id="rubric-1",
        criteria_text="Correctness and clarity",
    )


def make_target(**overrides):
    values = {
        "identifier": "target-1",
        "kind": "FILE",
        "source_path": "src/main.py",
        "rubric": make_rubric(),
    }
    values.update(overrides)
    return TargetConfig(**values)


# ---------------------------------------------------------------------------
# Relative path validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "src/main.py",
        "README.md",
        "src/tests/test_main.py",
        "*.py",
        "src/**/*.py",
    ],
)
def test_validate_relative_path_accepts_normalized_paths(path):
    assert validate_relative_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../secret.py",
        "src/../secret.py",
        "src\\main.py",
        "src//main.py",
        "./main.py",
        "src/\x00main.py",
        "a" * 257,
    ],
)
def test_validate_relative_path_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        validate_relative_path(path)


# ---------------------------------------------------------------------------
# Backend configuration
# ---------------------------------------------------------------------------

def test_backend_config_applies_defaults():
    config = make_backend_config()

    assert config.max_code_chars == 12_000
    assert config.max_guidance_chars == 2_000
    assert config.max_output_tokens == 16_384
    assert config.timeout_seconds == 240.0
    assert config.max_concurrency == 2
    assert config.max_body_bytes == 65_536


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_code_chars", 0),
        ("max_code_chars", 120_001),
        ("max_guidance_chars", -1),
        ("max_output_tokens", 0),
        ("timeout_seconds", 0),
        ("timeout_seconds", 301),
        ("max_concurrency", 0),
        ("max_concurrency", 65),
        ("max_body_bytes", 0),
        ("max_body_bytes", 1_048_577),
    ],
)
def test_backend_config_rejects_out_of_range_limits(field, value):
    with pytest.raises(ValidationError):
        make_backend_config(**{field: value})


def test_backend_config_rejects_duplicate_models():
    with pytest.raises(ValidationError, match="unique"):
        make_backend_config(
            allowed_models={
                "gemini_api": [
                    "gemini-test-model",
                    "gemini-test-model",
                ]
            }
        )


def test_vertex_configuration_requires_all_credentials():
    with pytest.raises(ValidationError, match="Vertex requires"):
        make_backend_config(
            allowed_models={"vertex": ["gemini-test-model"]}
        )


def test_vertex_configuration_accepts_complete_credentials():
    config = make_backend_config(
        allowed_models={"vertex": ["gemini-test-model"]},
        project="valid-project-123",
        location="us-central1",
        service_account=(
            "grader@valid-project.iam.gserviceaccount.com"
        ),
    )

    assert "vertex" in config.allowed_models


# ---------------------------------------------------------------------------
# Engine/model validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "engine,model",
    [
        ("vertex", "gemini-2.5-pro"),
        ("gemini_api", "gemini-test-model"),
        ("pitt", "@provider/model"),
    ],
)
def test_validate_engine_model_accepts_supported_models(engine, model):
    validate_engine_model(engine, model)


@pytest.mark.parametrize(
    "engine,model",
    [
        ("vertex", "gpt-4"),
        ("gemini_api", "claude-model"),
        ("pitt", "provider/model"),
        ("pitt", "@provider/"),
        ("pitt", "@"),
    ],
)
def test_validate_engine_model_rejects_invalid_models(engine, model):
    with pytest.raises(ValueError):
        validate_engine_model(engine, model)


def test_assignment_rejects_google_engine_with_non_google_provider():
    with pytest.raises(ValidationError, match="provider GOOGLE"):
        AssignmentConfig(
            id="assignment-1",
            engine="gemini_api",
            provider="OPENAI",
            model="gemini-test-model",
            targets={},
        )


# ---------------------------------------------------------------------------
# Assignment access and tokens
# ---------------------------------------------------------------------------

def test_assignment_access_defaults():
    access = AssignmentAccess()

    assert access.enabled is True
    assert access.max_calls == 5
    assert access.used_calls == 0


@pytest.mark.parametrize("max_calls", [-1, 100_001])
def test_assignment_access_rejects_invalid_quota(max_calls):
    with pytest.raises(ValidationError):
        AssignmentAccess(max_calls=max_calls)


def test_assignment_access_rejects_negative_used_calls():
    with pytest.raises(ValidationError):
        AssignmentAccess(used_calls=-1)


def test_token_requires_timezone_aware_expiration():
    with pytest.raises(ValidationError, match="timezone"):
        TokenRecord(
            assignment_id="assignment-1",
            expires_at=datetime(2030, 1, 1),
        )


def test_token_accepts_timezone_aware_expiration():
    token = TokenRecord(
        assignment_id="assignment-1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    assert token.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Request record invariants
# ---------------------------------------------------------------------------

def test_processing_request_is_valid():
    request = make_request()

    assert request.status == "processing"
    assert request.result is None
    assert request.error_code is None


def test_completed_request_requires_result():
    with pytest.raises(ValidationError, match="completed requests"):
        make_request(status="completed")


def test_completed_request_accepts_result():
    request = make_request(
        status="completed",
        result={"feedback_text": "Good work"},
    )

    assert request.result["feedback_text"] == "Good work"


def test_completed_request_rejects_error_code():
    with pytest.raises(ValidationError):
        make_request(
            status="completed",
            result={"feedback_text": "Good work"},
            error_code="model_error",
        )


def test_failed_request_requires_error_code():
    with pytest.raises(ValidationError, match="safe error code"):
        make_request(status="failed")


def test_failed_request_rejects_result():
    with pytest.raises(ValidationError):
        make_request(
            status="failed",
            error_code="model_error",
            result={"feedback_text": "Some feedback"},
        )


def test_request_rejects_invalid_digest():
    with pytest.raises(ValidationError):
        make_request(digest="not-a-sha256-digest")


def test_request_rejects_naive_timestamps():
    with pytest.raises(ValidationError, match="timezone"):
        make_request(
            created_at=datetime(2030, 1, 1),
        )


def test_request_rejects_unknown_error_code():
    with pytest.raises(ValidationError):
        make_request(
            status="failed",
            error_code="secret_internal_error",
        )


# ---------------------------------------------------------------------------
# State relationships
# ---------------------------------------------------------------------------

def test_state_rejects_token_for_unknown_assignment():
    with pytest.raises(ValidationError, match="token assignment"):
        State(
            config=make_backend_config(),
            assignments={},
            tokens={
                "a" * 64: TokenRecord(
                    assignment_id="missing-assignment",
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(days=1),
                )
            },
        )


def test_state_rejects_request_for_unknown_assignment():
    with pytest.raises(ValidationError, match="request assignment"):
        State(
            config=make_backend_config(),
            assignments={},
            tokens={},
            requests={"missing-assignment": {}},
        )


def test_state_rejects_quota_below_reserved_request_count():
    request = make_request()

    with pytest.raises(ValidationError, match="quota counter"):
        State(
            config=make_backend_config(),
            assignments={
                "assignment-1": AssignmentAccess(
                    max_calls=10,
                    used_calls=0,
                )
            },
            tokens={},
            requests={
                "assignment-1": {
                    request.request_id: request,
                }
            },
        )


def test_state_rejects_mismatched_request_dictionary_key():
    request = make_request()

    with pytest.raises(ValidationError, match="request key"):
        State(
            config=make_backend_config(),
            assignments={
                "assignment-1": AssignmentAccess(
                    used_calls=1,
                )
            },
            tokens={},
            requests={
                "assignment-1": {
                    "different-id": request,
                }
            },
        )


def test_state_accepts_consistent_records():
    request = make_request()

    state = State(
        config=make_backend_config(),
        assignments={
            "assignment-1": AssignmentAccess(
                max_calls=5,
                used_calls=1,
            )
        },
        tokens={
            "b" * 64: TokenRecord(
                assignment_id="assignment-1",
                expires_at=datetime.now(timezone.utc)
                + timedelta(days=1),
            )
        },
        requests={
            "assignment-1": {
                request.request_id: request,
            }
        },
    )

    assert state.schema_version == 3
    assert state.assignments["assignment-1"].used_calls == 1


# ---------------------------------------------------------------------------
# Assignment creation validation
# ---------------------------------------------------------------------------

def test_assignment_create_requires_exactly_one_course_source():
    common = {
        "platform_assignment_id": "gs-123",
        "name": "Assignment",
        "language": "python",
        "targets": [
            {
                "identifier": "target-1",
                "rubric": {"criteria_text": "Correctness"},
            }
        ],
    }

    with pytest.raises(ValidationError, match="Provide either"):
        AssignmentCreateInput(**common)

    with pytest.raises(ValidationError, match="Provide either"):
        AssignmentCreateInput(
            **common,
            course_id="00000000-0000-0000-0000-000000000001",
            course={"name": "Course"},
        )


def test_assignment_create_rejects_duplicate_target_identifiers():
    with pytest.raises(ValidationError, match="unique"):
        AssignmentCreateInput(
            course={"name": "Course"},
            platform_assignment_id="gs-123",
            name="Assignment",
            language="python",
            targets=[
                {
                    "identifier": "same",
                    "rubric": {"criteria_text": "Correctness"},
                },
                {
                    "identifier": "same",
                    "rubric": {"criteria_text": "Style"},
                },
            ],
        )

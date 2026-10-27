
import json
import sys
import types
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from uuid import uuid4

import pytest

from web.app.feedback_assignment import (
    MAX_CONFIG_BYTES,
    HttpAssignmentConfigReader,
    _NoRedirects,
    primary_model_config,
    read_assignment_config,
)
from web.app.feedback_config import AssignmentConfig
from web.app.feedback_store import StoreError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def valid_assignment_json(**overrides):
    assignment_id = str(overrides.pop("id", uuid4()))
    data = {
        "id": assignment_id,
        "enabled": True,
        "engine": "gemini_api",
        "provider": "GOOGLE",
        "model": "gemini-test-model",
        "targets": {},
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8"), assignment_id


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.read = Mock(side_effect=self._read)

    def _read(self, size=-1):
        if size >= 0:
            return self.body[:size]
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_reader(monkeypatch, body=None, *, error=None):
    reader = HttpAssignmentConfigReader(
        "https://grading.example.edu",
        "test-internal-token",
    )

    opener = Mock()

    if error is not None:
        opener.open.side_effect = error
    else:
        opener.open.return_value = FakeResponse(body)

    monkeypatch.setattr(reader, "_opener", opener)
    return reader, opener


def make_http_error(status):
    return HTTPError(
        url="https://grading.example.edu",
        code=status,
        msg="error",
        hdrs=None,
        fp=BytesIO(b"test error body"),
    )


@pytest.fixture
def fake_repository_module(monkeypatch):
    """Install the repository exception imported by read_assignment_config."""
    base_module = types.ModuleType("app.repository.base")

    class NotFoundError(Exception):
        pass

    base_module.NotFoundError = NotFoundError

    # Create the parent packages as well so the local import works in
    # environments where the application package is not installed.
    app_module = types.ModuleType("app")
    app_module.__path__ = []

    repository_module = types.ModuleType("app.repository")
    repository_module.__path__ = []

    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(
        sys.modules, "app.repository", repository_module
    )
    monkeypatch.setitem(
        sys.modules, "app.repository.base", base_module
    )

    return NotFoundError


# ---------------------------------------------------------------------------
# Primary provider selection
# ---------------------------------------------------------------------------

def test_primary_model_config_selects_primary_provider():
    providers = [
        SimpleNamespace(
            is_primary=False,
            engine="gemini_api",
            provider="GOOGLE",
            model_name="gemini-secondary",
        ),
        SimpleNamespace(
            is_primary=True,
            engine="gemini_api",
            provider="GOOGLE",
            model_name="gemini-primary",
        ),
    ]

    result = primary_model_config(providers)

    assert result == {
        "engine": "gemini_api",
        "provider": "GOOGLE",
        "model": "gemini-primary",
    }


def test_primary_model_config_rejects_no_primary_provider():
    providers = [
        SimpleNamespace(
            is_primary=False,
            engine="gemini_api",
            provider="GOOGLE",
            model_name="gemini-test-model",
        )
    ]

    with pytest.raises(StoreError) as exc:
        primary_model_config(providers)

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503


def test_primary_model_config_rejects_multiple_primary_providers():
    providers = [
        SimpleNamespace(
            is_primary=True,
            engine="gemini_api",
            provider="GOOGLE",
            model_name="gemini-test-model",
        ),
        SimpleNamespace(
            is_primary=True,
            engine="gemini_api",
            provider="GOOGLE",
            model_name="gemini-other-model",
        ),
    ]

    with pytest.raises(StoreError) as exc:
        primary_model_config(providers)

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503


@pytest.mark.parametrize(
    "engine,provider,model",
    [
        ("unknown", "GOOGLE", "gemini-test-model"),
        ("gemini_api", "OPENAI", "gemini-test-model"),
        ("gemini_api", "GOOGLE", "claude-model"),
        ("pitt", "OPENAI", "invalid-model"),
    ],
)
def test_primary_model_config_rejects_invalid_provider_settings(
    engine, provider, model
):
    providers = [
        SimpleNamespace(
            is_primary=True,
            engine=engine,
            provider=provider,
            model_name=model,
        )
    ]

    with pytest.raises(StoreError) as exc:
        primary_model_config(providers)

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# HTTP reader URL and credential validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://grading.example.edu",
        "https://grading.example.edu/",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_reader_accepts_secure_origins(url):
    reader = HttpAssignmentConfigReader(url, "internal-token")

    assert reader._origin == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "grading.example.edu",
        "ftp://grading.example.edu",
        "http://grading.example.edu",
        "http://localhost:8000",
        "https://user@grading.example.edu",
        "https://user:password@grading.example.edu",
        "https://grading.example.edu/path",
        "https://grading.example.edu/?secret=value",
        "https://grading.example.edu/#fragment",
        "https://grading.example.edu:99999",
        "https://grading.example.edu/ bad",
    ],
)
def test_reader_rejects_unsafe_or_malformed_origins(url):
    with pytest.raises(ValueError):
        HttpAssignmentConfigReader(url, "internal-token")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "has whitespace",
        "has\nnewline",
        "has\ttab",
        "x" * 4097,
    ],
)
def test_reader_rejects_invalid_internal_tokens(token):
    with pytest.raises(ValueError, match="token"):
        HttpAssignmentConfigReader(
            "https://grading.example.edu",
            token,
        )


@pytest.mark.parametrize(
    "timeout",
    [0, -1, 301, float("inf"), float("nan")],
)
def test_reader_rejects_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="timeout"):
        HttpAssignmentConfigReader(
            "https://grading.example.edu",
            "internal-token",
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# HTTP request behavior
# ---------------------------------------------------------------------------

def test_reader_sends_expected_url_and_headers(monkeypatch):
    body, assignment_id = valid_assignment_json()
    reader, opener = make_reader(monkeypatch, body)

    result = reader(assignment_id)

    assert isinstance(result, AssignmentConfig)
    assert result.id == assignment_id

    request, = opener.open.call_args.args

    assert request.full_url == (
        "https://grading.example.edu/internal/assignments/"
        f"{assignment_id}/config"
    )
    assert request.get_header("X-internal-token") == "test-internal-token"
    assert request.get_header("Accept") == "application/json"
    assert opener.open.call_args.kwargs["timeout"] == 10


def test_reader_rejects_invalid_assignment_id():
    reader = HttpAssignmentConfigReader(
        "https://grading.example.edu",
        "internal-token",
    )

    with pytest.raises(StoreError) as exc:
        reader("not-a-uuid")

    assert exc.value.code == "assignment_forbidden"
    assert exc.value.status_code == 403


@pytest.mark.parametrize("status", [403, 404])
def test_reader_maps_forbidden_and_missing_responses_to_403(
    monkeypatch, status
):
    reader, _ = make_reader(
        monkeypatch,
        error=make_http_error(status),
    )

    with pytest.raises(StoreError) as exc:
        reader(str(uuid4()))

    assert exc.value.code == "assignment_forbidden"
    assert exc.value.status_code == 403


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_reader_maps_other_http_errors_to_503(monkeypatch, status):
    reader, _ = make_reader(
        monkeypatch,
        error=make_http_error(status),
    )

    with pytest.raises(StoreError) as exc:
        reader(str(uuid4()))

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503


def test_reader_maps_network_errors_to_503(monkeypatch):
    reader, _ = make_reader(
        monkeypatch,
        error=OSError("sensitive internal network details"),
    )

    with pytest.raises(StoreError) as exc:
        reader(str(uuid4()))

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503
    assert "sensitive internal network details" not in str(exc.value)


def test_reader_rejects_malformed_json(monkeypatch):
    reader, _ = make_reader(monkeypatch, b"{invalid json")

    with pytest.raises(StoreError) as exc:
        reader(str(uuid4()))

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503


def test_reader_rejects_configuration_larger_than_limit(monkeypatch):
    body = b" " * (MAX_CONFIG_BYTES + 1)
    reader, opener = make_reader(monkeypatch, body)

    with pytest.raises(StoreError) as exc:
        reader(str(uuid4()))

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503

    response = opener.open.return_value
    response.read.assert_called_once_with(MAX_CONFIG_BYTES + 1)


def test_reader_rejects_assignment_id_mismatch(monkeypatch):
    body, _ = valid_assignment_json()
    reader, _ = make_reader(monkeypatch, body)

    with pytest.raises(StoreError) as exc:
        reader(str(uuid4()))

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503


def test_reader_rejects_disabled_assignment(monkeypatch):
    body, assignment_id = valid_assignment_json(enabled=False)
    reader, _ = make_reader(monkeypatch, body)

    with pytest.raises(StoreError) as exc:
        reader(assignment_id)

    assert exc.value.code == "assignment_forbidden"
    assert exc.value.status_code == 403


def test_no_redirect_handler_refuses_redirects():
    handler = _NoRedirects()

    assert handler.redirect_request(
        None,
        None,
        302,
        "Found",
        {},
        "https://other.example.edu",
    ) is None


# ---------------------------------------------------------------------------
# Database configuration reader
# ---------------------------------------------------------------------------

def test_read_assignment_config_rejects_invalid_uuid():
    with pytest.raises(StoreError) as exc:
        read_assignment_config(Mock(), "not-a-uuid")

    assert exc.value.code == "assignment_forbidden"
    assert exc.value.status_code == 403


def test_read_assignment_config_maps_missing_assignment(
    fake_repository_module,
):
    repository = Mock()
    repository.get_assignment_config.side_effect = fake_repository_module()

    with pytest.raises(StoreError) as exc:
        read_assignment_config(repository, str(uuid4()))

    assert exc.value.code == "assignment_forbidden"
    assert exc.value.status_code == 403


def test_read_assignment_config_maps_repository_errors(
    fake_repository_module,
):
    repository = Mock()
    repository.get_assignment_config.side_effect = RuntimeError(
        "database credentials must not leak"
    )

    with pytest.raises(StoreError) as exc:
        read_assignment_config(repository, str(uuid4()))

    assert exc.value.code == "assignment_config_unavailable"
    assert exc.value.status_code == 503
    assert "database credentials" not in str(exc.value)


def test_read_assignment_config_rejects_inactive_assignment(
    fake_repository_module,
):
    assignment_id = uuid4()

    repository = Mock()
    repository.get_assignment_config.return_value = SimpleNamespace(
        is_active=False,
    )

    with pytest.raises(StoreError) as exc:
        read_assignment_config(repository, str(assignment_id))

    assert exc.value.code == "assignment_forbidden"
    assert exc.value.status_code == 403

    repository.get_assignment_config.assert_called_once_with(assignment_id)

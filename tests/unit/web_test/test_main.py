
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from web.app import main
from app.repository import InMemoryRepository
from app.auth import InstructorAccount, require_instructor
from app.ui import get_repository


def test_health_returns_ok():
    """Health check succeeds when the database query succeeds."""
    mock_engine = MagicMock()
    mock_connection = mock_engine.connect.return_value.__enter__.return_value

    with patch.object(main, "engine", mock_engine):
        response = main.health()

    assert response == {"status": "ok"}
    mock_connection.execute.assert_called_once()


def test_health_raises_when_database_fails():
    """Health check propagates database connection failures."""
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = RuntimeError("Database unavailable")

    with patch.object(main, "engine", mock_engine):
        with pytest.raises(RuntimeError, match="Database unavailable"):
            main.health()


def test_require_integration_token_accepts_valid_token():
    """A valid internal token is accepted."""
    with patch.object(main.settings, "integration_api_token", "test-token"):
        result = main.require_integration_token("test-token")

    assert result is None


@pytest.mark.parametrize(
    "configured_token, supplied_token",
    [
        ("test-token", None),
        ("test-token", "wrong-token"),
        (None, "test-token"),
        ("", "test-token"),
    ],
)
def test_require_integration_token_rejects_invalid_tokens(
    configured_token, supplied_token
):
    """Missing configuration or an invalid token is rejected."""
    with patch.object(
        main.settings, "integration_api_token", configured_token
    ):
        with pytest.raises(HTTPException) as exc_info:
            main.require_integration_token(supplied_token)

    assert exc_info.value.status_code == 401


def test_nightly_submissions_rejects_invalid_mode():
    """Only NIGHTLY mode is accepted."""
    with pytest.raises(HTTPException) as exc_info:
        main.nightly_submissions(mode="INVALID")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "mode must be NIGHTLY"


def test_nightly_submissions_returns_empty_list():
    """No pending submissions produces an empty list."""
    repository = MagicMock()
    repository.list_submissions_pending_ai.return_value = []

    with patch.object(main, "grading_repository", repository):
        result = main.nightly_submissions(mode="NIGHTLY")

    assert result == []
    repository.list_submissions_pending_ai.assert_called_once_with("NIGHTLY")


def test_nightly_submissions_returns_serialized_records():
    """Pending submissions are converted into API response dictionaries."""
    submission = MagicMock()
    submission.submission_id = "11111111-1111-1111-1111-111111111111"
    submission.platform = "canvas"
    submission.platform_assignment_id = "assignment-123"
    submission.platform_submission_id = "submission-456"
    submission.student_identifier = "student-789"

    repository = MagicMock()
    repository.list_submissions_pending_ai.return_value = [submission]

    with patch.object(main, "grading_repository", repository):
        result = main.nightly_submissions(mode="NIGHTLY")

    assert result == [
        {
            "submission_id": "11111111-1111-1111-1111-111111111111",
            "platform": "canvas",
            "platform_assignment_id": "assignment-123",
            "platform_submission_id": "submission-456",
            "student_identifier": "student-789",
        }
    ]


def test_authorize_nightly_submission_succeeds():
    """A valid submission is authorized through the repository."""
    from uuid import UUID

    submission_id = UUID("11111111-1111-1111-1111-111111111111")
    repository = MagicMock()

    with patch.object(main, "grading_repository", repository):
        result = main.authorize_nightly_submission(submission_id)

    assert result == {
        "submission_id": str(submission_id),
        "authorized": True,
    }
    repository.authorize_for_grading.assert_called_once_with(submission_id)


def test_authorize_nightly_submission_propagates_not_found():
    """A missing submission produces an HTTP 404."""
    from uuid import UUID
    from app.repository import NotFoundError

    submission_id = UUID("11111111-1111-1111-1111-111111111111")
    repository = MagicMock()
    repository.authorize_for_grading.side_effect = NotFoundError(
        "Submission not found"
    )

    with patch.object(main, "grading_repository", repository):
        with pytest.raises(HTTPException) as exc_info:
            main.authorize_nightly_submission(submission_id)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Submission not found"


def test_index_uses_in_memory_repository_override():
    """The home page can be tested without a real grading repository."""
    from fastapi.testclient import TestClient

    app = main.app
    app.dependency_overrides[get_repository] = lambda: InMemoryRepository()
    app.dependency_overrides[require_instructor] = lambda: InstructorAccount(
        uuid.uuid4(), "ada@example.edu", None, "unused", True)

    try:
        with TestClient(app) as client:
            response = client.get("/")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200

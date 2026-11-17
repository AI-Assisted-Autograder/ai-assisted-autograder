"""FastAPI + SQL repository integration tests.

Requires a dedicated PostgreSQL database selected with TEST_DATABASE_URL.
The suite refuses to fall back to the application's normal DATABASE_URL.
"""
import os
import sys
from pathlib import Path
from uuid import UUID

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = PROJECT_ROOT / "web"


@pytest.fixture(scope="module")
def web_app():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set TEST_DATABASE_URL to a dedicated PostgreSQL test database")

    # Configure before importing app.db: it creates the SQLAlchemy engine at import time.
    os.environ["DATABASE_URL"] = database_url
    os.environ["INTEGRATION_API_TOKEN"] = "integration-test-token"
    os.environ["SESSION_SECRET"] = "integration-test-session-secret"
    os.environ.pop("FEEDBACK_STATE_PATH", None)
    sys.path.insert(0, str(WEB_DIR))
    previous_cwd = Path.cwd()
    os.chdir(WEB_DIR)

    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from app.models import TriggerRun, TriggerRunSubmission
    from app.db import engine, SessionLocal
    from app.main import app

    # This test suite is intended only for an explicitly named test database.
    if "test" not in database_url.rsplit("/", 1)[-1].lower():
        pytest.fail("TEST_DATABASE_URL must point to a database whose name contains 'test'")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"Configured PostgreSQL test database is unavailable: {error}")
        pytest.skip(f"PostgreSQL test database is unavailable: {error}")

    # Apply the real application migrations. Base.metadata.create_all() does not
    # create migration-only objects such as the partial unique index that
    # enforces the one-RUNNING-nightly-run invariant.
    alembic_config = Config(str(WEB_DIR / "alembic.ini"))
    command.upgrade(alembic_config, "head")

    # This is a dedicated test database. Clear leftovers from an interrupted
    # local run so a stale RUNNING row cannot affect the next test execution.
    with SessionLocal.begin() as session:
        session.query(TriggerRunSubmission).delete()
        session.query(TriggerRun).delete()

    with TestClient(app) as client:
        yield client, SessionLocal, TriggerRun

    # Clean audit rows while preserving the migrated schema.
    with SessionLocal.begin() as session:
        session.query(TriggerRunSubmission).delete()
        session.query(TriggerRun).delete()
    engine.dispose()
    os.chdir(previous_cwd)


def test_internal_nightly_api_authenticates_and_persists_run_lifecycle(web_app):
    client, sessions, TriggerRun = web_app

    unauthorized = client.post("/internal/nightly/runs")
    assert unauthorized.status_code == 401

    headers = {"X-Internal-Token": "integration-test-token"}
    started = client.post("/internal/nightly/runs", headers=headers)
    assert started.status_code == 200
    run_id = started.json()["run_id"]

    # The database's single-running-run constraint is exercised through HTTP.
    conflict = client.post("/internal/nightly/runs", headers=headers)
    assert conflict.status_code == 409

    finished = client.post(
        f"/internal/nightly/runs/{run_id}/finish",
        headers=headers,
        json={
            "status": "SUCCEEDED",
            "submissions_authorized": 2,
            "submissions_rerun": 2,
            "submissions_failed_to_trigger": 0,
        },
    )
    assert finished.status_code == 200
    assert finished.json()["status"] == "SUCCEEDED"

    with sessions() as session:
        run = session.get(TriggerRun, UUID(run_id))
        assert run is not None
        assert run.status == "SUCCEEDED"
        assert run.finished_at is not None
        assert run.submissions_authorized == 2
        assert run.submissions_rerun == 2
        assert run.submissions_failed_to_trigger == 0

    # A finished run releases the database-backed singleton lock.
    next_run = client.post("/internal/nightly/runs", headers=headers)
    assert next_run.status_code == 200

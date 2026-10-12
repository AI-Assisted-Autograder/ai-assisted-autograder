from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, text

from app.config import settings
from app.db import SessionLocal, engine
from app.models import TriggerRun
from app.repository import NotFoundError, SqlGradingRepository
from app.ui import router as ui_router

app = FastAPI(title="AI-Assisted Autograder")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(ui_router)
grading_repository = SqlGradingRepository(SessionLocal)


def require_integration_token(x_internal_token: str | None = Header(default=None)) -> None:
    """Protect integration endpoints with a shared secret."""
    if not settings.integration_api_token or x_internal_token != settings.integration_api_token:
        raise HTTPException(status_code=401, detail="valid X-Internal-Token required")


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/internal/nightly/submissions")
def nightly_submissions(
    mode: str = Query(default="NIGHTLY"),
    _: None = Depends(require_integration_token),
):
    """Return database-backed submissions that the nightly trigger may process."""
    if mode != "NIGHTLY":
        raise HTTPException(status_code=400, detail="mode must be NIGHTLY")

    submissions = grading_repository.list_submissions_pending_ai(mode)
    return [
        {
            "submission_id": str(item.submission_id),
            "platform": item.platform,
            "platform_assignment_id": item.platform_assignment_id,
            "platform_submission_id": item.platform_submission_id,
            "student_identifier": item.student_identifier,
        }
        for item in submissions
    ]


@app.post("/internal/nightly/submissions/{submission_id}/authorize")
def authorize_nightly_submission(
    submission_id: UUID,
    _: None = Depends(require_integration_token),
):
    """Authorize a submission before the browser clicks Rerun Autograder."""
    try:
        grading_repository.authorize_for_grading(submission_id)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"submission_id": str(submission_id), "authorized": True}


class TriggerRunFinishRequest(BaseModel):
    status: Literal["SUCCEEDED", "FAILED"]
    submissions_authorized: int
    submissions_rerun: int
    submissions_failed_to_trigger: int


@app.post("/internal/nightly/runs")
def start_nightly_run(_: None = Depends(require_integration_token)):
    """Create an audit row before the nightly trigger starts browser work."""
    with SessionLocal.begin() as session:
        run = TriggerRun(trigger="SCHEDULE", status="RUNNING")
        session.add(run)
        session.flush()
        return {"run_id": str(run.id)}


@app.post("/internal/nightly/runs/{run_id}/finish")
def finish_nightly_run(
    run_id: UUID,
    request: TriggerRunFinishRequest,
    _: None = Depends(require_integration_token),
):
    """Persist the aggregate result when the nightly trigger exits."""
    with SessionLocal.begin() as session:
        run = session.get(TriggerRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Trigger run {run_id} not found")
        run.finished_at = func.now()
        run.status = request.status
        run.submissions_authorized = request.submissions_authorized
        run.submissions_rerun = request.submissions_rerun
        run.submissions_failed_to_trigger = request.submissions_failed_to_trigger
    return {"run_id": str(run_id), "status": request.status}

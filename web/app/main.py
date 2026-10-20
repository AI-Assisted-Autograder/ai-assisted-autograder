from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal, engine
from .feedback_assignment import MAX_CONFIG_BYTES, primary_google_model, read_assignment_config
from .feedback_config import (
    AssignmentConfig, AssignmentCreateInput, CreatedAssignmentConfig, RubricConfig, TargetConfig,
)
from .feedback_store import StoreError
from app.models import (
    Assignment, Course, GradingTarget, LlmProviderConfig, Rubric, TriggerRun, TriggerRunSubmission,
)
from app.repository import NotFoundError, SqlGradingRepository
from app.ui import router as ui_router

app = FastAPI(title="AI-Assisted Autograder")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(ui_router)
grading_repository = SqlGradingRepository(SessionLocal)

if settings.feedback_state_path:
    from .feedback import create_feedback_router

    app.include_router(create_feedback_router(
        settings.feedback_state_path,
        assignment_loader=lambda assignment_id: read_assignment_config(
            grading_repository, assignment_id,
        ),
    ))


def require_integration_token(x_internal_token: str | None = Header(default=None)) -> None:
    """Protect integration endpoints with a shared secret."""
    if not settings.integration_api_token or x_internal_token != settings.integration_api_token:
        raise HTTPException(status_code=401, detail="valid X-Internal-Token required")


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/internal/assignments/{assignment_id}/config", response_model=AssignmentConfig)
def assignment_config(
    assignment_id: UUID,
    response: Response,
    _: None = Depends(require_integration_token),
):
    """Read current feedback configuration without recording submissions or feedback."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return read_assignment_config(grading_repository, str(assignment_id))
    except StoreError as error:
        raise HTTPException(
            status_code=error.status_code, detail=error.code,
            headers={"Cache-Control": "no-store"},
        ) from None


@app.post("/internal/assignments", status_code=201, response_model=CreatedAssignmentConfig)
def create_assignment(
    request: AssignmentCreateInput,
    response: Response,
    _: None = Depends(require_integration_token),
):
    """Atomically create instructor configuration; never write submission or feedback data."""
    try:
        with SessionLocal.begin() as session:
            existing = session.scalar(select(Assignment.id).where(
                Assignment.platform == request.platform,
                Assignment.platform_assignment_id == request.platform_assignment_id,
            ))
            if existing is not None:
                raise HTTPException(status_code=409, detail="Assignment already exists for this platform ID")
            model = primary_google_model(session.scalars(
                select(LlmProviderConfig).where(LlmProviderConfig.is_enabled)
            ).all())
            if request.course_id is not None:
                course = session.get(Course, request.course_id)
                if course is None:
                    raise HTTPException(status_code=404, detail="Course not found")
            else:
                course = Course(name=request.course.name, term=request.course.term)
                session.add(course)
                session.flush()
            assignment = Assignment(
                course_id=course.id, platform=request.platform,
                platform_assignment_id=request.platform_assignment_id,
                name=request.name, language=request.language,
                grading_mode=request.grading_mode, is_active=True,
            )
            session.add(assignment)
            session.flush()
            targets = {}
            for order, item in enumerate(request.targets):
                target = GradingTarget(
                    assignment_id=assignment.id, identifier=item.identifier,
                    kind=item.kind, display_order=order,
                )
                session.add(target)
                session.flush()
                rubric = Rubric(
                    grading_target_id=target.id, version=1, is_current=True,
                    criteria_text=item.rubric.criteria_text,
                    feedback_guidance=item.rubric.feedback_guidance,
                )
                session.add(rubric)
                session.flush()
                targets[str(target.id)] = TargetConfig(
                    identifier=target.identifier, kind=target.kind,
                    source_path=target.identifier if target.kind == "FILE" else "submitted-source",
                    rubric=RubricConfig(id=str(rubric.id), version=rubric.version,
                                        **item.rubric.model_dump()),
                )
            result = CreatedAssignmentConfig(
                id=str(assignment.id), course_id=course.id, enabled=True,
                model=model, targets=targets,
            )
            if len(result.model_dump_json().encode("utf-8")) > MAX_CONFIG_BYTES:
                raise HTTPException(status_code=413, detail="Assignment configuration exceeds 1 MiB")
    except StoreError:
        raise HTTPException(
            status_code=503,
            detail="Configure an enabled primary GOOGLE provider with a supported Gemini model first",
        ) from None
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Assignment configuration conflicts with existing data") from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Location"] = f"/internal/assignments/{result.id}/config"
    return result


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


class TriggerRunSubmissionRequest(BaseModel):
    status: Literal["AUTHORIZED", "RERUN_SUCCEEDED", "RERUN_FAILED"]
    error_message: str | None = None


@app.post("/internal/nightly/runs")
def start_nightly_run(_: None = Depends(require_integration_token)):
    """Create an audit row before the nightly trigger starts browser work."""
    try:
        with SessionLocal.begin() as session:
            run = TriggerRun(trigger="SCHEDULE", status="RUNNING")
            session.add(run)
            session.flush()
            return {"run_id": str(run.id)}
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="another nightly run is already RUNNING") from error


@app.post("/internal/nightly/runs/{run_id}/submissions/{submission_id}")
def record_nightly_submission(
    run_id: UUID,
    submission_id: UUID,
    request: TriggerRunSubmissionRequest,
    _: None = Depends(require_integration_token),
):
    """Record the latest per-submission outcome for a nightly run."""
    with SessionLocal.begin() as session:
        run = session.get(TriggerRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Trigger run {run_id} not found")
        if run.finished_at is not None:
            raise HTTPException(status_code=409, detail=f"Trigger run {run_id} is already finished")
        record = session.scalar(
            select(TriggerRunSubmission).where(
                TriggerRunSubmission.trigger_run_id == run_id,
                TriggerRunSubmission.submission_id == submission_id,
            )
        )
        if record is None:
            record = TriggerRunSubmission(
                trigger_run_id=run_id,
                submission_id=submission_id,
                status=request.status,
                error_message=request.error_message,
            )
            session.add(record)
        else:
            record.status = request.status
            record.error_message = request.error_message
            record.recorded_at = func.now()
    return {"run_id": str(run_id), "submission_id": str(submission_id), "status": request.status}


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

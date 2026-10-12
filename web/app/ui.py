"""Instructor UI: server-rendered pages (Jinja2, HTMX where a page needs it).

Pages read through the repository directly rather than calling the JSON
API, so each operation is written once. Read-only so far.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

from app.db import SessionLocal
from app.repository import (
    Action,
    IGradingRepository,
    NotFoundError,
    SqlGradingRepository,
    TargetStatus,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_repository = SqlGradingRepository(SessionLocal)


def get_repository() -> IGradingRepository:
    """Overridden in tests with an InMemoryRepository."""
    return _repository


def when(value: datetime | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def number(value: Decimal | None) -> str:
    """2.50 -> 2.5, 10 -> 10."""
    if value is None:
        return ""
    return format(value.normalize(), "f")


def submission_state(action: Action, grading_mode: str) -> tuple[str, str]:
    """A label and badge style for where a submission stands (AD-11)."""
    if action is Action.REEMIT:
        return "Graded", "done"
    if action is Action.DEFER:
        return "Waiting for nightly run", "waiting"
    if grading_mode == "NIGHTLY":
        return "Authorized, not graded yet", "waiting"
    return "Not graded yet", "waiting"


TARGET_STATES = {
    TargetStatus.DONE: ("Feedback given", "done"),
    TargetStatus.FAILED: ("Failed", "failed"),
    TargetStatus.WAITING: ("Waiting", "waiting"),
    TargetStatus.OPTED_OUT: ("Opted out", "muted"),
}

templates.env.filters["when"] = when
templates.env.filters["number"] = number
templates.env.globals["submission_state"] = submission_state
templates.env.globals["TARGET_STATES"] = TARGET_STATES


def not_found(request: Request, message: str):
    return templates.TemplateResponse(request, "not_found.html", {"message": message}, status_code=404)


@router.get("/")
def assignments(request: Request, repo: IGradingRepository = Depends(get_repository)):
    return templates.TemplateResponse(request, "assignments.html", {"assignments": repo.list_assignments()})


@router.get("/assignments/{assignment_id}")
def assignment(
    request: Request, assignment_id: uuid.UUID, repo: IGradingRepository = Depends(get_repository)
):
    try:
        config = repo.get_assignment_config(assignment_id)
        submissions = repo.list_submissions(assignment_id)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    return templates.TemplateResponse(
        request, "assignment.html", {"assignment": config, "submissions": submissions}
    )


@router.get("/submissions/{submission_id}")
def submission(
    request: Request, submission_id: uuid.UUID, repo: IGradingRepository = Depends(get_repository)
):
    try:
        detail = repo.get_submission_detail(submission_id)
    except NotFoundError:
        return not_found(request, "That submission does not exist.")
    return templates.TemplateResponse(request, "submission.html", {"submission": detail})

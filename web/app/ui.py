"""Instructor UI: server-rendered pages (Jinja2, HTMX where a page needs it).

Pages read and write through the repository directly rather than calling
the JSON API, so each operation is written once. Forms post back and
redirect on success (post/redirect/get); on a validation error the form
is shown again with what was typed and the reason.
"""

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import SessionLocal
from app.models import GRADING_MODES, TARGET_KINDS
from app.repository import (
    Action,
    ConflictError,
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

# Suggestions; FILE targets and explicit source patterns support other languages.
LANGUAGES = ("JAVA", "PYTHON", "TYPESCRIPT", "JAVASCRIPT")

templates.env.filters["when"] = when
templates.env.filters["number"] = number
templates.env.globals["submission_state"] = submission_state
templates.env.globals["TARGET_STATES"] = TARGET_STATES
templates.env.globals["GRADING_MODES"] = GRADING_MODES
templates.env.globals["TARGET_KINDS"] = TARGET_KINDS
templates.env.globals["LANGUAGES"] = LANGUAGES


def not_found(request: Request, message: str):
    return templates.TemplateResponse(request, "not_found.html", {"message": message}, status_code=404)


@router.get("/")
def assignments(request: Request, repo: IGradingRepository = Depends(get_repository)):
    return templates.TemplateResponse(request, "assignments.html", {"assignments": repo.list_assignments()})


def form_error(error: ValueError | ConflictError) -> tuple[str, int]:
    """The message to show above a form, and the status to send it with."""
    message = str(error)
    return message[:1].upper() + message[1:], 409 if isinstance(error, ConflictError) else 400


def parse_score(value: str, label: str = "max score") -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    try:
        score = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number, not {value!r}") from None
    if not score.is_finite():
        raise ValueError(f"{label} must be a number")
    return score


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/assignments/new")
def new_assignment(request: Request):
    return templates.TemplateResponse(request, "assignment_form.html", {"form": {}, "error": None})


@router.post("/assignments")
def create_assignment(
    request: Request,
    course_name: Annotated[str, Form()] = "",
    term: Annotated[str, Form()] = "",
    platform_assignment_id: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    grading_mode: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "",
    repo: IGradingRepository = Depends(get_repository),
):
    form = {
        "course_name": course_name,
        "term": term,
        "platform_assignment_id": platform_assignment_id,
        "name": name,
        "grading_mode": grading_mode,
        "language": language,
    }
    try:
        assignment_id = repo.create_assignment(**form)
    except (ValueError, ConflictError) as error:
        message, status = form_error(error)
        return templates.TemplateResponse(
            request, "assignment_form.html", {"form": form, "error": message}, status_code=status
        )
    return see_other(f"/assignments/{assignment_id}")


def assignment_page(
    request: Request,
    repo: IGradingRepository,
    assignment_id: uuid.UUID,
    target_form: dict | None = None,
    target_error: str | None = None,
    status_code: int = 200,
    view: str = "students",
    delete_error: str | None = None,
):
    by_target = view == "targets"
    try:
        config = repo.get_assignment_config(assignment_id)
        if by_target:
            submissions, target_summaries = [], repo.list_target_summaries(assignment_id)
        else:
            submissions, target_summaries = repo.list_submissions(assignment_id), []
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    return templates.TemplateResponse(
        request,
        "assignment.html",
        {
            "assignment": config,
            "by_target": by_target,
            "submissions": submissions,
            "target_summaries": target_summaries,
            "target_form": target_form or {},
            "target_error": target_error,
            "delete_error": delete_error,
        },
        status_code=status_code,
    )


@router.get("/assignments/{assignment_id}")
def assignment(
    request: Request,
    assignment_id: uuid.UUID,
    view: str = "students",
    repo: IGradingRepository = Depends(get_repository),
):
    """view=targets lists submissions by grading target instead of by student."""
    return assignment_page(request, repo, assignment_id, view=view)


@router.post("/assignments/{assignment_id}/targets")
def create_target(
    request: Request,
    assignment_id: uuid.UUID,
    identifier: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    behavior: Annotated[str, Form()] = "ai_grading",
    source_globs: Annotated[str, Form()] = "",
    repo: IGradingRepository = Depends(get_repository),
):
    form = {"identifier": identifier, "kind": kind, "behavior": behavior, "source_globs": source_globs}
    try:
        if behavior not in ("ai_grading", "functional_test_feedback"):
            raise ValueError("Choose AI grading or Functional-test feedback")
        patterns = [line for line in source_globs.splitlines() if line.strip()]
        target_id = repo.create_target(assignment_id, identifier=identifier, kind=kind, max_score=None,
                                      analysis_config={"behavior": behavior, "source_globs": patterns})
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    except (ValueError, ConflictError) as error:
        message, status = form_error(error)
        return assignment_page(request, repo, assignment_id, form, message, status)
    return see_other(f"/assignments/{assignment_id}/targets/{target_id}/rubric")


@router.post("/assignments/{assignment_id}/targets/{target_id}/delete")
def delete_target(
    request: Request,
    assignment_id: uuid.UUID,
    target_id: uuid.UUID,
    repo: IGradingRepository = Depends(get_repository),
):
    try:
        repo.delete_target(assignment_id, target_id)
    except NotFoundError:
        return not_found(request, "That grading target does not exist.")
    except ConflictError as error:
        message, status = form_error(error)
        return assignment_page(request, repo, assignment_id, status_code=status, delete_error=message)
    return see_other(f"/assignments/{assignment_id}#targets")


def rubric_page(
    request: Request,
    repo: IGradingRepository,
    assignment_id: uuid.UUID,
    target_id: uuid.UUID,
    form: dict | None = None,
    error: str | None = None,
    status_code: int = 200,
):
    try:
        config = repo.get_assignment_config(assignment_id)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    target = next((t for t in config.targets if t.id == target_id), None)
    if target is None:
        return not_found(request, "That grading target does not exist.")
    if form is None:
        current = target.rubric
        analysis = target.analysis_config
        behavior = analysis.get("behavior") or ("ai_grading" if target.max_score is not None else "functional_test_feedback")
        sources = analysis.get("evidence_sources", [])
        functional_source = analysis.get("functional_result_source")
        form = {
            "criteria_text": (current and current.criteria_text) or "",
            "feedback_guidance": (current and current.feedback_guidance) or "",
            "analysis_mode": analysis.get("mode", "code_feedback"),
            "behavior": behavior,
            "functional_result_source": functional_source,
            "grading_items": analysis.get("grading_items") or [
                {"id": identifier, "requirement": "", "points": analysis.get("points_per_test")}
                for identifier in analysis.get("expected_tests", [])
            ],
            "legacy_test_plan": analysis.get("test_plan", "") if not analysis.get("behavior") else "",
            "source_globs": "\n".join(analysis.get("source_globs", [])),
            "context_globs": "\n".join(analysis.get("context_globs", [])),
            "test_plan": analysis.get("test_plan", ""),
            "max_source_files": analysis.get("max_source_files"),
            "temperature": analysis.get("temperature"),
            "evidence_sources": [
                {**source, "include": "\n".join(source.get("include", []))}
                for source in sources
            ],
        }
    return templates.TemplateResponse(
        request,
        "rubric_form.html",
        {"assignment": config, "target": target, "form": form, "error": error},
        status_code=status_code,
    )


@router.get("/assignments/{assignment_id}/targets/{target_id}")
def target(
    request: Request,
    assignment_id: uuid.UUID,
    target_id: uuid.UUID,
    repo: IGradingRepository = Depends(get_repository),
):
    try:
        detail = repo.get_target_submissions(assignment_id, target_id)
    except NotFoundError:
        return not_found(request, "That grading target does not exist.")
    return templates.TemplateResponse(request, "target.html", {"target": detail})


@router.get("/assignments/{assignment_id}/targets/{target_id}/rubric")
def edit_rubric(
    request: Request,
    assignment_id: uuid.UUID,
    target_id: uuid.UUID,
    repo: IGradingRepository = Depends(get_repository),
):
    return rubric_page(request, repo, assignment_id, target_id)


@router.post("/assignments/{assignment_id}/targets/{target_id}/rubric")
async def save_rubric(
    request: Request,
    assignment_id: uuid.UUID,
    target_id: uuid.UUID,
    criteria_text: Annotated[str, Form()] = "",
    feedback_guidance: Annotated[str, Form()] = "",
    repo: IGradingRepository = Depends(get_repository),
):
    data = await request.form()
    form = {**dict(data), "criteria_text": criteria_text, "feedback_guidance": feedback_guidance,
            "evidence_sources": [], "grading_items": []}
    try:
        config = await run_in_threadpool(repo.get_assignment_config, assignment_id)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    # So a target cannot be edited under another assignment's URL.
    current_target = next((target for target in config.targets if target.id == target_id), None)
    if current_target is None:
        return not_found(request, "That grading target does not exist.")
    form.setdefault("test_plan", current_target.analysis_config.get("test_plan", ""))
    try:
        options = {}
        if "behavior" in data:
            behavior = form["behavior"]
            if behavior not in ("ai_grading", "functional_test_feedback"):
                raise ValueError("Choose AI grading or Functional-test feedback")
            form["evidence_sources"] = [
                {"format": fmt, "path": path, "label": label, "include": include}
                for fmt, path, label, include in zip(
                    data.getlist("evidence_format"), data.getlist("evidence_path"),
                    data.getlist("evidence_label"), data.getlist("evidence_include"), strict=True,
                ) if path.strip() or label.strip() or include.strip()
            ]
            analysis = {
                "behavior": behavior,
                "test_plan": form["test_plan"],
                "attempt_id": (str(uuid.uuid4()) if form.get("new_ai_attempt") == "1"
                               else current_target.analysis_config.get("attempt_id")),
                "mode": form.get("analysis_mode", current_target.analysis_config.get("mode", "code_feedback")),
                "evidence_sources": [
                    {**source, "include": [line for line in source["include"].splitlines() if line.strip()]}
                    for source in form["evidence_sources"]
                ],
            }
            if behavior == "ai_grading":
                form["grading_items"] = [
                    {"id": item_id or str(uuid.uuid4()), "requirement": requirement, "points": points}
                    for item_id, requirement, points in zip(data.getlist("item_id"),
                        data.getlist("item_requirement"), data.getlist("item_points"), strict=True)
                ]
                analysis["grading_items"] = []
                for item in form["grading_items"]:
                    points = parse_score(item["points"], "Item points")
                    if points is None or points <= 0 or points.normalize().as_tuple().exponent < -2:
                        raise ValueError("Item points must be positive with at most two decimal places")
                    analysis["grading_items"].append({**item, "points": float(points)})
                default_background = "Evaluate the configured items using the supplied student source and permitted evidence."
            else:
                analysis["functional_result_source"] = form.get("functional_result_source", "")
                default_background = "Explain the reported functional-test failures using the supplied student source and permitted evidence."
            criteria_text = criteria_text.strip() or default_background
            for key in ("source_globs", "context_globs"):
                analysis[key] = [line for line in form.get(key, "").splitlines() if line.strip()]
            temperature = parse_score(form.get("temperature", ""), "temperature")
            analysis["temperature"] = float(temperature) if temperature is not None else None
            limit = form.get("max_source_files", "").strip()
            analysis["max_source_files"] = int(limit) if limit else None
            options = {"analysis_config": analysis, "max_score": None}
        await run_in_threadpool(repo.upsert_rubric, target_id, criteria_text=criteria_text,
                                feedback_guidance=feedback_guidance, **options)
    except (ValueError, ConflictError) as error:
        message, status = form_error(error)
        return await run_in_threadpool(rubric_page, request, repo, assignment_id, target_id, form, message, status)
    return see_other(f"/assignments/{assignment_id}#targets")


@router.get("/submissions/{submission_id}")
def submission(
    request: Request, submission_id: uuid.UUID, repo: IGradingRepository = Depends(get_repository)
):
    try:
        detail = repo.get_submission_detail(submission_id)
    except NotFoundError:
        return not_found(request, "That submission does not exist.")
    return templates.TemplateResponse(request, "submission.html", {"submission": detail})

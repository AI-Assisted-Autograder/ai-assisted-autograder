"""Instructor UI: server-rendered pages (Jinja2, HTMX where a page needs it).

Pages read and write through the repository directly rather than calling
the JSON API, so each operation is written once. Forms post back and
redirect on success (post/redirect/get); on a validation error the form
is shown again with what was typed and the reason.
"""

import io
import json
import re
import secrets
import uuid
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile as StarletteUploadFile

from ai_grading import local_client
from app.assignment_context import MAX_FILES, MAX_TOTAL_CHARS, MAX_UPLOAD_BYTES, check_context, clean_filename, extract_text
from app.auth import InstructorAccount, require_instructor
from app.config import settings
from app.db import SessionLocal
from app.feedback_assignment import connector_config
from app.feedback_config import AI_MODE_BEHAVIORS, AssignmentAccess, RubricInput
from app.feedback_store import COST_PERIODS, StoreError
from app.models import AI_MODES, GRADING_MODES, TARGET_KINDS
from app.pitt_settings import PITT_MODELS, encrypt_pitt_key, resolve_form_key, validate_pitt_model
from app.repository import (
    Action,
    ConflictError,
    IGradingRepository,
    NotFoundError,
    SqlGradingRepository,
    TargetStatus,
)
from app.repository.base import REVIEW_COMMENT_LIMIT, REVIEW_RATINGS

# Every page here needs a signed-in instructor; /login itself is in app.auth.
router = APIRouter(dependencies=[Depends(require_instructor)])
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


def usd(value: str | Decimal) -> str:
    return f"${Decimal(value):.6f}"


def cost_axis_max(value: str) -> Decimal:
    """Round the chart ceiling up to one significant digit."""
    peak = Decimal(value)
    step = Decimal(1).scaleb(peak.adjusted())
    return peak.quantize(step, rounding=ROUND_CEILING)


def submission_state(action: Action, grading_mode: str) -> tuple[str, str]:
    """A label and badge style for where a submission stands (AD-11)."""
    if action is Action.REEMIT:
        return "Graded", "done"
    if action is Action.DEFER:
        return "Waiting for nightly run", "waiting"
    if grading_mode == "NIGHTLY":
        return "Authorized, not graded yet", "waiting"
    return "Not graded yet", "waiting"


def ai_result_state(status: str | None, targets, grading_mode: str, action: Action) -> tuple[str, str]:
    if grading_mode == "NIGHTLY" and action is not Action.REEMIT:
        label = "Awaiting nightly authorization" if action is Action.DEFER else "Awaiting AI processing"
        return label, "waiting"
    if status is None:
        return "—", "muted"
    behaviors = {target.analysis_config.get("behavior") for target in targets if target.rubric is not None}
    if behaviors == {"functional_test_feedback"}:
        labels = ("Feedback checked ✓", "Feedback failed")
    elif behaviors == {"ai_grading"}:
        labels = ("AI grading processed ✓", "AI grading failed")
    else:
        labels = ("AI processing complete ✓", "AI processing failed")
    return (labels[0], "done") if status == "SUCCEEDED" else (labels[1], "failed")


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
templates.env.filters["decimal"] = Decimal
templates.env.filters["usd"] = usd
templates.env.filters["cost_axis_max"] = cost_axis_max
templates.env.globals["submission_state"] = submission_state
templates.env.globals["ai_result_state"] = ai_result_state
templates.env.globals["TARGET_STATES"] = TARGET_STATES
templates.env.globals["AI_MODES"] = AI_MODES
templates.env.globals["GRADING_MODES"] = GRADING_MODES
templates.env.globals["TARGET_KINDS"] = TARGET_KINDS
templates.env.globals["LANGUAGES"] = LANGUAGES
templates.env.globals["CONTEXT_LIMITS"] = {
    "files": MAX_FILES, "chars": MAX_TOTAL_CHARS, "upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
}


async def chosen_files(request: Request) -> list[StarletteUploadFile]:
    """The files chosen in a form's context_files input.

    Read from the parsed form rather than declared as File(), because a file
    input left empty still sends one part with no filename, which Starlette
    hands over as a string and FastAPI would reject as invalid.
    """
    form = await request.form()
    return [f for f in form.getlist("context_files") if isinstance(f, StarletteUploadFile) and f.filename]


def not_found(request: Request, message: str):
    return templates.TemplateResponse(request, "not_found.html", {"message": message}, status_code=404)


@router.get("/")
def assignments(request: Request, repo: IGradingRepository = Depends(get_repository)):
    store = getattr(request.app.state, "feedback_store", None)
    return templates.TemplateResponse(request, "assignments.html", {
        "assignments": repo.list_assignments(),
        "cost_summaries": store.cost_summaries() if store is not None else {},
    })


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


def ai_settings_page(
    request: Request,
    repo: IGradingRepository,
    *,
    selected_model: str | None = None,
    error: str | None = None,
    saved: bool = False,
    status_code: int = 200,
):
    try:
        settings = repo.get_pitt_settings()
    except Exception:
        settings = None
        error = "AI settings are unavailable. Try again later."
        status_code = 503
    model = selected_model if selected_model in PITT_MODELS else (settings.model if settings else None)
    return templates.TemplateResponse(
        request, "ai_settings.html",
        {"settings": settings, "models": PITT_MODELS, "selected_model": model,
         "error": error, "saved": saved and settings is not None},
        status_code=status_code, headers={"Cache-Control": "no-store"},
    )


@router.get("/settings/ai")
def ai_settings(request: Request, repo: IGradingRepository = Depends(get_repository)):
    return ai_settings_page(request, repo, saved=request.query_params.get("saved") == "1")


async def pitt_form_values(request: Request) -> tuple[str, str]:
    # Parse directly so validation errors never echo submitted credentials.
    form = await request.form()
    model, api_key = form.get("model", ""), form.get("api_key", "")
    if not isinstance(model, str) or not isinstance(api_key, str):
        raise ValueError("Choose a model and enter an API key as text.")
    return validate_pitt_model(model), api_key


@router.post("/settings/ai")
async def save_ai_settings(request: Request, repo: IGradingRepository = Depends(get_repository)):
    model = None
    try:
        model, raw_key = await pitt_form_values(request)
        key = resolve_form_key(raw_key)
        encrypted_key = encrypt_pitt_key(key) if raw_key.strip() else None
    except ValueError as error:
        return await run_in_threadpool(
            ai_settings_page, request, repo, selected_model=model, error=str(error), status_code=400,
        )
    try:
        store = getattr(request.app.state, "feedback_store", None)
        if store is not None:
            await run_in_threadpool(store.allow_pitt_models)
        await run_in_threadpool(repo.save_pitt_settings, model, encrypted_key)
    except Exception:
        return await run_in_threadpool(
            ai_settings_page, request, repo, selected_model=model,
            error="Settings could not be saved. Try again later.", status_code=503,
        )
    return RedirectResponse("/settings/ai?saved=1", status_code=303, headers={"Cache-Control": "no-store"})


def test_pitt_connection(model: str, key: str) -> None:
    from ai_grading.ILLMProvider.PittProvider import PittProvider

    provider = PittProvider(model, api_key=key, timeout_seconds=30, max_output_tokens=64)
    try:
        provider.test_connection()
    finally:
        provider.close()


@router.post("/settings/ai/test")
async def test_ai_settings(request: Request):
    from ai_grading.ILLMProvider.ILLMProvider import ProviderError

    try:
        model, raw_key = await pitt_form_values(request)
        key = resolve_form_key(raw_key)
    except ValueError as error:
        return JSONResponse({"ok": False, "message": str(error)}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    try:
        await run_in_threadpool(test_pitt_connection, model, key)
    except ProviderError as error:
        message = {
            "backend_auth_unavailable": "The API key was rejected or cannot access this model.",
            "model_unavailable": "The selected model is unavailable for this API key.",
            "rate_limited": "Pitt is rate-limiting this API key. Wait and try again.",
            "timeout": "Pitt did not respond in time. Try again later.",
            "provider_unavailable": "Could not connect to Pitt AI Connect. Try again later.",
        }.get(error.code, error.message)
        return JSONResponse({"ok": False, "message": message}, status_code=502,
                            headers={"Cache-Control": "no-store"})
    except Exception:
        return JSONResponse({"ok": False, "message": "The API test could not be completed. Try again later."},
                            status_code=502, headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": True, "message": "API connection successful. Settings have not been saved."},
                        headers={"Cache-Control": "no-store"})


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
    ai_mode: Annotated[str, Form()] = "",
    grading_mode: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "",
    context_notes: Annotated[str, Form()] = "",
    context_files: list[StarletteUploadFile] = Depends(chosen_files),
    repo: IGradingRepository = Depends(get_repository),
):
    form = {
        "course_name": course_name,
        "term": term,
        "platform_assignment_id": platform_assignment_id,
        "name": name,
        "ai_mode": ai_mode,
        "grading_mode": grading_mode,
        "language": language,
    }
    try:
        # Read and checked before the assignment exists, so a bad file does
        # not leave a half-made assignment behind.
        uploads = read_uploads(context_files)
        check_context(context_notes.strip(), [text for _, text in uploads])
        assignment_id = repo.create_assignment(**form)
    except (ValueError, ConflictError) as error:
        message, status = form_error(error)
        return templates.TemplateResponse(
            request, "assignment_form.html",
            {"form": {**form, "context_notes": context_notes}, "error": message}, status_code=status,
        )
    if context_notes.strip():
        repo.set_context_notes(assignment_id, context_notes)
    if uploads:
        repo.add_context_files(assignment_id, uploads)
    return see_other(f"/assignments/{assignment_id}/targets/new")


def read_uploads(files: list[StarletteUploadFile]) -> list[tuple[str, str]]:
    """(filename, text) for each chosen file."""
    found = []
    for f in files:
        name = clean_filename(f.filename)
        # One byte over the limit is enough to refuse it without reading it all.
        found.append((name, extract_text(name, f.file.read(MAX_UPLOAD_BYTES + 1))))
    return found


def connector_origin(request: Request, active: bool) -> str:
    if not active:
        raise HTTPException(409, "This assignment is inactive.")
    if getattr(request.app.state, "feedback_store", None) is None:
        raise HTTPException(503, "Configure the Web feedback service to enable token downloads.")
    try:
        return local_client.validate_backend_url(settings.feedback_public_base_url or "")
    except local_client.ClientError:
        raise HTTPException(503, "Set FEEDBACK_PUBLIC_BASE_URL to a valid backend origin reachable from Gradescope.") from None


def connector_archive(config: dict, raw_token: str) -> bytes:
    scripts = Path(local_client.__file__).parent
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("gradescope_feedback.py", "local_client.py"):
            archive.writestr(name, (scripts / name).read_bytes())
        archive.writestr("config.json", json.dumps(config) + "\n")
        archive.writestr("assignment.token", raw_token + "\n")
    return output.getvalue()


def assignment_page(
    request: Request,
    repo: IGradingRepository,
    assignment_id: uuid.UUID,
    target_form: dict | None = None,
    target_error: str | None = None,
    status_code: int = 200,
    view: str = "students",
    delete_error: str | None = None,
    context_error: str | None = None,
    context_form: dict | None = None,
    connector_error: str | None = None,
    ai_mode_error: str | None = None,
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
    store = getattr(request.app.state, "feedback_store", None)
    cost_period = request.query_params.get("cost_range", "7d")
    if cost_period not in COST_PERIODS:
        cost_period = "7d"
    cost_summary = access = None
    connector_ready = False
    try:
        if store is not None:
            cost_summary = store.cost_summary(str(assignment_id), period=cost_period)
            access = store.find_assignment_access(str(assignment_id))
        connector_origin(request, config.is_active)
        connector_ready = access is None or access.enabled
        if not connector_ready:
            connector_error = "Connector access is disabled for this assignment."
        if "token_csrf" not in request.session:
            request.session["token_csrf"] = secrets.token_urlsafe(32)
    except HTTPException as error:
        connector_error = error.detail
    except StoreError:
        connector_error = "The feedback service is unavailable."
    return templates.TemplateResponse(
        request,
        "assignment.html",
        {
            "assignment": config,
            "grading_item_count": sum(len(t.analysis_config.get("grading_items") or ()) for t in config.targets),
            "grading_points": sum((t.max_score for t in config.targets if t.max_score is not None), Decimal(0)),
            "ai_mode_error": ai_mode_error,
            "cost_summary": cost_summary,
            "connector_error": connector_error,
            "connector_ready": connector_ready,
            "connector_access": access,
            "default_max_calls": AssignmentAccess().max_calls,
            "token_csrf": request.session.get("token_csrf", ""),
            "cost_period": cost_period,
            "cost_periods": COST_PERIODS,
            "by_target": by_target,
            "submissions": submissions,
            "target_summaries": target_summaries,
            "target_form": target_form or {},
            "target_error": target_error,
            "delete_error": delete_error,
            "context_error": context_error,
            "assignment_context_chars": len(config.context_notes or "") + sum(len(f.content) for f in config.context_files),
            "context_form": context_form or {},
        },
        status_code=status_code, headers={"Cache-Control": "no-store"},
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


@router.post("/assignments/{assignment_id}/ai-mode")
def set_ai_mode(
    request: Request,
    assignment_id: uuid.UUID,
    ai_mode: Annotated[str, Form()],
    repo: IGradingRepository = Depends(get_repository),
):
    try:
        repo.set_ai_mode(assignment_id, ai_mode)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    except (ValueError, ConflictError) as error:
        message, status = form_error(error)
        return assignment_page(request, repo, assignment_id, ai_mode_error=message, status_code=status)
    return see_other(f"/assignments/{assignment_id}/targets/new")


@router.post("/assignments/{assignment_id}/connector.zip")
def download_assignment_connector(
    request: Request,
    assignment_id: uuid.UUID,
    max_calls: Annotated[int | None, Form(ge=1, le=100_000)] = None,
    csrf_token: Annotated[str, Form()] = "",
    repo: IGradingRepository = Depends(get_repository),
):
    try:
        assignment = repo.get_assignment_config(assignment_id)
        origin = connector_origin(request, assignment.is_active)
        expected = request.session.get("token_csrf")
        if not expected or not secrets.compare_digest(expected.encode(), csrf_token.encode()):
            raise HTTPException(403, "Your session changed. Download the connector again.")
        token = repo.get_assignment_token(assignment_id)
        content = connector_archive(connector_config(origin, assignment), token)
        request.app.state.feedback_store.ensure_assignment_token(str(assignment_id), token, max_calls=max_calls)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    except HTTPException as error:
        return assignment_page(request, repo, assignment_id, status_code=error.status_code,
                               connector_error=error.detail)
    except (StoreError, OSError) as error:
        status = error.status_code if isinstance(error, StoreError) else 503
        return assignment_page(request, repo, assignment_id, status_code=status,
                               connector_error="Connector download is unavailable for this assignment.")
    file_id = re.sub(r"[^A-Za-z0-9_-]", "_", assignment.platform_assignment_id)[:100]
    return Response(content, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="gradescope-ai-{file_id}.zip"',
        "Cache-Control": "no-store",
    })


RESULT_FILTERS = ("all", "has_deduction")


@router.get("/assignments/{assignment_id}/results")
def results(
    request: Request,
    assignment_id: uuid.UUID,
    filter: str = "all",
    repo: IGradingRepository = Depends(get_repository),
):
    """The results dashboard (FR-8..FR-10). filter=has_deduction keeps only
    students with at least one deduction (OQ-1: a failing latest test, or an
    AI score below the target's max)."""
    try:
        found = repo.list_results(assignment_id)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    if filter not in RESULT_FILTERS:
        filter = "all"
    rows = found.rows
    statuses = [c.status for r in rows for c in r.cells if c is not None]
    summary = {
        "students": len(rows),
        "with_deduction": sum(1 for r in rows if r.has_deduction),
        "ai_failed": sum(1 for r in rows if r.ai_status == "FAILED"),
        "waiting": statuses.count(TargetStatus.WAITING),
    }
    column_deductions = [
        sum(1 for r in rows if r.cells[i] is not None and r.cells[i].deduction)
        for i in range(len(found.columns))
    ]
    shown = [r for r in rows if r.has_deduction] if filter == "has_deduction" else list(rows)
    return templates.TemplateResponse(request, "results.html", {
        "results": found,
        "rows": shown,
        "filter": filter,
        "summary": summary,
        "column_deductions": column_deductions,
    })


@router.post("/assignments/{assignment_id}/context/notes")
def save_context_notes(
    request: Request,
    assignment_id: uuid.UUID,
    context_notes: Annotated[str, Form()] = "",
    repo: IGradingRepository = Depends(get_repository),
):
    try:
        repo.set_context_notes(assignment_id, context_notes)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    except ValueError as error:
        message, status = form_error(error)
        return assignment_page(request, repo, assignment_id, status_code=status, context_error=message,
                               context_form={"context_notes": context_notes})
    return see_other(f"/assignments/{assignment_id}#context")


@router.post("/assignments/{assignment_id}/context/files")
def upload_context_files(
    request: Request,
    assignment_id: uuid.UUID,
    context_files: list[StarletteUploadFile] = Depends(chosen_files),
    repo: IGradingRepository = Depends(get_repository),
):
    try:
        uploads = read_uploads(context_files)
        if not uploads:
            raise ValueError("Choose at least one file to upload")
        repo.add_context_files(assignment_id, uploads)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    except ValueError as error:
        message, status = form_error(error)
        return assignment_page(request, repo, assignment_id, status_code=status, context_error=message)
    return see_other(f"/assignments/{assignment_id}#context")


@router.post("/assignments/{assignment_id}/context/files/{file_id}/delete")
def delete_context_file(
    request: Request,
    assignment_id: uuid.UUID,
    file_id: uuid.UUID,
    repo: IGradingRepository = Depends(get_repository),
):
    try:
        repo.delete_context_file(assignment_id, file_id)
    except NotFoundError:
        return not_found(request, "That context file does not exist.")
    return see_other(f"/assignments/{assignment_id}#context")


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


FINAL_RESULTS = "results/results.json"
DEFAULT_EVIDENCE_LABEL = "Functional test results"
TARGET_BACKGROUND = {
    "ai_grading": "Evaluate the configured items using the supplied student source and permitted evidence.",
    "functional_test_feedback": "Explain the reported functional-test failures using the supplied student source and permitted evidence.",
}


def evidence_selection(sources):
    """Show exact test names when the existing report filters can be edited as names."""
    if not sources:
        return "none", ""
    if len(sources) != 1 or sources[0]["format"] != "gradescope" or sources[0]["path"] != FINAL_RESULTS:
        return "custom", ""
    names = []
    for pattern in sources[0].get("include", []):
        name = re.sub(r"\\(.)", r"\1", pattern[1:-1])
        if pattern != f"^{re.escape(name)}$" or not name.strip():
            return "custom", ""
        names.append(name)
    return ("named" if names else "all"), "\n".join(names)


def rubric_form(ai_mode, target):
    analysis = target.analysis_config if target else {}
    rubric = target.rubric if target else None
    sources = analysis.get("evidence_sources", [])
    selection, names = evidence_selection(sources)
    behavior = AI_MODE_BEHAVIORS.get(ai_mode) or analysis.get("behavior")
    if not behavior:
        behavior = "ai_grading" if target.max_score is not None else "functional_test_feedback"
    return {
        "identifier": target.identifier if target else "",
        "behavior": behavior,
        "feedback_guidance": (rubric and rubric.feedback_guidance) or "",
        "shared_grading_rules": analysis.get("shared_grading_rules", ""),
        "grading_items": analysis.get("grading_items") or [
            {"id": identifier, "requirement": "", "points": analysis.get("points_per_test")}
            for identifier in analysis.get("expected_tests", [])
        ],
        "legacy_test_plan": analysis.get("test_plan", "") if not analysis.get("behavior") else "",
        "source_globs": "\n".join(analysis.get("source_globs", [])),
        "evidence_selection": selection,
        "has_custom_evidence": selection == "custom",
        "test_names": names,
    }


def target_evidence(form, behavior, sources):
    include = []
    if behavior == "ai_grading":
        selection = form["evidence_selection"]
        if selection == "none":
            return []
        if selection == "custom" and form["has_custom_evidence"]:
            return sources
        if selection == "named":
            include = [f"^{re.escape(name)}$" for name in form["test_names"].splitlines() if name.strip()]
            if not include:
                raise ValueError("Enter at least one visible test name")
        elif selection != "all":
            raise ValueError("Choose a test evidence option")
    label = DEFAULT_EVIDENCE_LABEL
    if len(sources) == 1 and sources[0]["format"] == "gradescope" and sources[0]["path"] == FINAL_RESULTS:
        label = sources[0]["label"]
    return [{"format": "gradescope", "path": FINAL_RESULTS, "label": label, "include": include}]


def target_analysis(form, ai_mode, target):
    behavior = AI_MODE_BEHAVIORS.get(ai_mode) or form["behavior"]
    if behavior not in AI_MODE_BEHAVIORS.values():
        raise ValueError("Choose AI grading or Functional-test feedback")
    previous = target.analysis_config if target else {}
    source_globs = [line for line in form["source_globs"].splitlines() if line.strip()]
    if ai_mode and not source_globs and (not target or (target.kind == "FILE" and previous.get("source_globs"))):
        raise ValueError("Enter the student source files for this configuration")

    # Keep the target's stored collection and model settings when editing its visible fields.
    analysis = dict(previous, behavior=behavior, source_globs=source_globs,
                    evidence_sources=target_evidence(form, behavior, previous.get("evidence_sources", ())))
    # Explicit behaviors use grading items instead of legacy per-test scoring.
    analysis.pop("expected_tests", None)
    analysis.pop("points_per_test", None)
    if behavior == "ai_grading":
        analysis.pop("functional_result_source", None)
        if not form["grading_items"]:
            raise ValueError("Add at least one grading item")
        analysis["shared_grading_rules"] = form["shared_grading_rules"]
        analysis["grading_items"] = []
        for item in form["grading_items"]:
            points = parse_score(item["points"], "Item points")
            if points is None or points <= 0 or points.normalize().as_tuple().exponent < -2:
                raise ValueError("Item points must be positive with at most two decimal places")
            analysis["grading_items"].append({**item, "points": float(points)})
    else:
        analysis.pop("grading_items", None)
        analysis.pop("shared_grading_rules", None)
        analysis["functional_result_source"] = analysis["evidence_sources"][0]["label"]
    return analysis


@router.get("/assignments/{assignment_id}/targets/new")
@router.post("/assignments/{assignment_id}/targets/new")
@router.get("/assignments/{assignment_id}/targets/{target_id}/rubric")
@router.post("/assignments/{assignment_id}/targets/{target_id}/rubric")
async def configure_target(
    request: Request,
    assignment_id: uuid.UUID,
    target_id: uuid.UUID | None = None,
    repo: IGradingRepository = Depends(get_repository),
):
    try:
        config = await run_in_threadpool(repo.get_assignment_config, assignment_id)
    except NotFoundError:
        return not_found(request, "That assignment does not exist.")
    target = next((t for t in config.targets if t.id == target_id), None)
    if target_id is not None and target is None:
        return not_found(request, "That grading target does not exist.")
    if target is None:
        if config.ai_mode is None:
            return see_other(f"/assignments/{assignment_id}#targets")
        if config.ai_mode == "FEEDBACK" and config.targets and request.method == "GET":
            return see_other(f"/assignments/{assignment_id}/targets/{config.targets[0].id}/rubric")

    form = rubric_form(config.ai_mode, target)
    error, status = None, 200
    if request.method == "POST":
        data = await request.form()
        for field in ("identifier", "behavior", "source_globs", "shared_grading_rules", "evidence_selection",
                      "test_names", "feedback_guidance"):
            if field in data:
                form[field] = data[field]
        # Earlier forms omitted the selection when the evidence checkbox was off.
        if "ai_mode" in data and "evidence_selection" not in data:
            form["evidence_selection"] = "none"
        try:
            if config.ai_mode and data.get("ai_mode", config.ai_mode) != config.ai_mode:
                raise ConflictError("The assignment AI mode changed. Reopen its configuration before saving.")
            form["grading_items"] = [
                {"id": item_id or str(uuid.uuid4()), "requirement": requirement, "points": points}
                for item_id, requirement, points in zip(data.getlist("item_id"), data.getlist("item_requirement"),
                                                       data.getlist("item_points"), strict=True)
            ]
            options = {}
            rubric = target.rubric if target else None
            guidance = form["feedback_guidance"]
            if target is None or "ai_mode" in data or "behavior" in data:
                analysis = target_analysis(form, config.ai_mode, target)
                options = {"analysis_config": analysis, "max_score": None}
                criteria = rubric.criteria_text if rubric else TARGET_BACKGROUND[analysis["behavior"]]
                if analysis["behavior"] == "ai_grading":
                    guidance = (rubric and rubric.feedback_guidance) or ""
            else:
                # Older rubric-only callers update the rubric without replacing target configuration.
                criteria = data.get("criteria_text", rubric.criteria_text if rubric else "")
            if target:
                await run_in_threadpool(repo.upsert_rubric, target.id, criteria_text=criteria,
                                        feedback_guidance=guidance, **options)
            else:
                identifier = "Functional test feedback" if config.ai_mode == "FEEDBACK" else form["identifier"]
                await run_in_threadpool(repo.create_target, assignment_id, identifier=identifier,
                                        kind="FILE",
                                        rubric=RubricInput(criteria_text=criteria, feedback_guidance=guidance),
                                        **options)
            return see_other(f"/assignments/{assignment_id}#targets")
        except NotFoundError:
            return not_found(request, "That assignment or target no longer exists.")
        except (ValueError, ConflictError) as exc:
            error, status = form_error(exc)
    return templates.TemplateResponse(request, "rubric_form.html", {
        "assignment": config, "target": target, "form": form, "error": error,
    }, status_code=status)


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


def submission_page(
    request: Request,
    repo: IGradingRepository,
    submission_id: uuid.UUID,
    *,
    review_error: tuple[uuid.UUID, str] | None = None,
    status_code: int = 200,
):
    try:
        detail = repo.get_submission_detail(submission_id)
    except NotFoundError:
        return not_found(request, "That submission does not exist.")
    return templates.TemplateResponse(request, "submission.html", {
        "submission": detail,
        "review_error": review_error,
        "REVIEW_RATINGS": REVIEW_RATINGS,
        "REVIEW_COMMENT_LIMIT": REVIEW_COMMENT_LIMIT,
    }, status_code=status_code)


@router.get("/submissions/{submission_id}")
def submission(
    request: Request, submission_id: uuid.UUID, repo: IGradingRepository = Depends(get_repository)
):
    return submission_page(request, repo, submission_id)


@router.post("/feedback/{feedback_id}/review")
def review_feedback(
    request: Request,
    feedback_id: uuid.UUID,
    rating: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    # Only used to show a validation error on the page the form came from.
    submission_id: Annotated[str, Form()] = "",
    account: InstructorAccount = Depends(require_instructor),
    repo: IGradingRepository = Depends(get_repository),
):
    """Rate one provider's response 1-5, with an optional comment (FR-11)."""
    try:
        stars = int(rating) if rating.strip().isdigit() else 0
        sid = repo.create_review(feedback_id, rating=stars, comment=comment, reviewer=account.email)
    except NotFoundError:
        return not_found(request, "That feedback does not exist.")
    except ValueError as error:
        message, status = form_error(error)
        try:
            back = uuid.UUID(submission_id)
        except ValueError:
            return JSONResponse({"detail": message}, status_code=status)
        return submission_page(request, repo, back, review_error=(feedback_id, message), status_code=status)
    return see_other(f"/submissions/{sid}#feedback-{feedback_id}")

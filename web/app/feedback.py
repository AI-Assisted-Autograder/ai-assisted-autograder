"""Assignment-token feedback using DB configuration and local runtime state."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
from types import SimpleNamespace
import threading
import time
from typing import Literal
from uuid import UUID
from weakref import WeakValueDictionary

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_grading import gradescope_feedback as gradescope, local_client
from ai_grading.models import (
    CodeFile, Evidence, FeedbackRequest, GradingTarget, ResolvedSource, Rubric, TargetKind,
)
from ai_grading.FileResolver import FileResolver
from ai_grading.PromptBuilder import PromptBuilder
from ai_grading.ILLMProvider.ILLMProvider import ProviderError
from .feedback_assignment import feedback_assignment_config, read_active_assignment
from .feedback_store import JsonStore, StoreError
from .feedback_config import validate_relative_path
from .repository.base import FeedbackResult, ProviderFeedback, SubmissionRef
from .run_results import map_outcomes

LOG = logging.getLogger(__name__)
MESSAGES = {
    "unauthorized": "A valid assignment token is required.",
    "assignment_forbidden": "Assignment is unavailable.",
    "target_forbidden": "Target is not available for this assignment.",
    "assignment_config_unavailable": "Database assignment configuration is unavailable.",
    "config_changed": "Assignment configuration changed. Fetch the current configuration and retry.",
    "invalid_request": "Request fields or JSON are invalid.",
    "input_too_large": "Input exceeds the backend limit.",
    "quota_exceeded": "Assignment call quota is exhausted.",
    "busy": "Backend concurrency limit reached. Try this request later.",
    "request_conflict": "This request_id was used with different content.",
    "processing": "This request is already processing; no new call was made.",
    "interrupted": "This request was interrupted; its reserved call remains consumed.",
    "model_error": "The model did not return usable feedback; the reserved call remains consumed.",
    "upstream_timeout": "The model timed out; the reserved call remains consumed.",
    "grading_response_timeout": "The Grading result was not ready within the hook time budget.",
    "backend_auth_unavailable": "Backend model authentication is unavailable.",
    "backend_error": "Backend could not safely finish this request.",
    "ai_not_authorized": "This submission has not received nightly AI authorization.",
}


class SubmissionContext(BaseModel):
    """The platform identity needed to connect feedback to one DB row."""

    model_config = ConfigDict(extra="forbid", strict=True)
    platform_submission_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    submitted_at: datetime
    student_identifier: str | None = Field(default=None, max_length=512)


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    run_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    target_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    config_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    code: str | None = Field(default=None, min_length=1)
    files: list[CodeFile] = Field(default_factory=list, max_length=64)
    evidence: list[Evidence] = Field(default_factory=list, max_length=256)
    guidance: str | None = ""
    functional_test_feedback: list[str] = Field(default_factory=list)
    # Present in a Gradescope autograder run.  It is optional so the feedback
    # endpoint remains usable for local/provider-only calls.
    submission: SubmissionContext | None = None

    @model_validator(mode="after")
    def source_and_evidence(self):
        if (self.code is None) == (not self.files):
            raise ValueError("provide either code or files")
        paths = [validate_relative_path(source.path) for source in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("source paths must be distinct")
        ids = [item.id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence identifiers must be distinct")
        for item in self.evidence:
            if (max(len(item.id), len(item.source), len(item.test_id or "")) > 256
                    or len(item.scope) > 512 or len(item.summary) > 4_000):
                raise ValueError("evidence exceeds its limits")
        return self


class GradingRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    submission_id: str | None = Field(default=None, max_length=256, pattern=r"^[0-9]+$")
    submission: SubmissionContext | None = None
    results: dict
    files: dict[str, str | None]
    reports: dict[str, dict] = Field(default_factory=dict)
    symlink_dirs: list[str] = Field(default_factory=list)
    glob_star_in_segment: bool = False
    collection_error: bool = False
    budget_seconds: float = Field(gt=0, le=295)

    @model_validator(mode="after")
    def uploaded_paths_and_submission(self):
        if self.submission and self.submission.platform_submission_id != self.submission_id:
            raise ValueError("submission identity does not match its metadata")
        if len(self.files) + len(self.symlink_dirs) > local_client.MAX_GRADING_ENTRIES:
            raise ValueError("submission contains too many entries")
        for name in (*self.files, *self.symlink_dirs):
            path = PurePosixPath(name)
            if (not path.parts or path.is_absolute() or ".." in path.parts or str(path) != name
                    or "\x00" in name):
                raise ValueError("uploaded paths must be normalized relative POSIX paths")
        for name in self.reports:
            validate_relative_path(name)
        return self


TEST_RESULTS_MAX_BYTES = 2_097_152
# The connector leaves five seconds to receive and write the final results.
GRADING_REPLAY_GRACE_SECONDS = 2
GRADING_HEARTBEAT_SECONDS = 5
# One-byte heartbeats over the hook's entire 300-second lifetime.
GRADING_HEARTBEAT_BYTES = 1 + 300 // GRADING_HEARTBEAT_SECONDS


class ReplayVerificationTimeout(TimeoutError):
    """The deadline passed before this target's existing outcome could be established."""


@dataclass
class GradingTargetAttempt:
    record: dict
    payload: FeedbackInput | None = None
    response: dict | None = None
    error: Exception | None = None


class TestRow(BaseModel):
    """One row of Gradescope's results.json "tests" list, as the hook sends it."""

    __test__ = False  # Not a pytest test class, despite the name.
    model_config = ConfigDict(extra="ignore")
    name: str | None = Field(default=None, max_length=1_024)
    status: str | None = Field(default=None, max_length=32)
    score: float | None = None
    max_score: float | None = None
    output: str | None = Field(default=None, max_length=16_000)
    visibility: str | None = Field(default=None, max_length=32)


class TestResultsInput(BaseModel):
    __test__ = False
    model_config = ConfigDict(extra="forbid")
    submission: SubmissionContext
    run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    # results.json's top-level default for rows without their own visibility.
    visibility: str | None = Field(default=None, max_length=32)
    tests: list[TestRow] = Field(default_factory=list, max_length=2_000)


class AiCompleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    submission: SubmissionContext
    ai_status: Literal["SUCCEEDED", "FAILED"]
    completion_reason: Literal["processed", "not_needed"] = "processed"

    @model_validator(mode="after")
    def completion_outcome(self):
        if self.completion_reason == "not_needed" and self.ai_status != "SUCCEEDED":
            raise ValueError("No-AI-needed completion must be successful")
        return self


def matches_path(path: str, patterns: tuple[str, ...]) -> bool:
    return any(FileResolver._matches(path, pattern) for pattern in patterns)


def grading_item_label(item_id: str, index: int) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", item_id):
        return f"Criterion {index}"
    return item_id


def format_feedback_body(result: dict, evidence: list[Evidence]) -> str:
    """Render the summary and student-facing details as plain text."""
    sections = [result["feedback_text"]]
    behavior = result.get("behavior")
    if behavior == "ai_grading":
        for index, item in enumerate(result.get("items", ()), 1):
            label = grading_item_label(item["test_id"], index)
            maximum = str(item["max_score"]).removesuffix(".0")
            score = item["suggested_score"]
            if score is None:
                outcome = f"Pending regrade (max {maximum})"
            else:
                awarded = str(score).removesuffix(".0")
                outcome = f"{item['verdict'].capitalize()} ({awarded}/{maximum})"
            sections.append(f"{label} — {outcome}\n{item['feedback']}".rstrip())
    elif behavior == "functional_test_feedback":
        test_names = {observation.id: observation.test_id for observation in evidence}
        for item in result.get("failure_feedback", ()):
            name = test_names[item["evidence_id"]] or "Unnamed functional result"
            sections.append(f"{name}\n{item['feedback']}")
    # PostgreSQL text cannot store a literal NUL.
    return "\n\n".join(sections).replace("\x00", r"\x00")


def json_digest(content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assignment_revision(assignment) -> str:
    """Bind collected inputs and replay IDs to the grading configuration."""
    content = {"assignment": assignment.model_dump(mode="json"),
               "prompts": [PromptBuilder.version, PromptBuilder.extended_version,
                           PromptBuilder.per_test_version, PromptBuilder.grading_version,
                           PromptBuilder.functional_version]}
    return json_digest(content)


def collection_targets(assignment):
    targets = {}
    for target_id, target in assignment.targets.items():
        analysis = asdict(target.analysis)
        # On Python < 3.13 Path.glob("dir/**") only returns directories.
        for key in ("source_globs", "context_globs"):
            analysis[key] = [p + "/*" if p.rsplit("/", 1)[-1] == "**" else p for p in analysis[key]]
        targets[target_id] = {
            "identifier": target.identifier, "kind": target.kind,
            "source_path": target.source_path, "max_score": target.max_score,
            "analysis": {key: analysis[key] for key in (
                "mode", "behavior", "functional_result_source", "source_globs", "context_globs",
                "evidence_sources", "max_source_files",
            )},
        }
        targets[target_id]["analysis"]["grading_items"] = [
            {"id": item.id, "points": item.points, "label": grading_item_label(item.id, index)}
            for index, item in enumerate(target.analysis.grading_items, 1)
        ]
    return targets


def grading_response_room(records, targets, revision):
    """Leave room for every configured row, even when later targets cannot run."""
    # The cleaned functional results fit in the upload. Top-level scores, the
    # appended notice and an optional final database diagnostic need additional space.
    size = local_client.MAX_GRADING_BODY_BYTES + 8192 + GRADING_HEARTBEAT_BYTES
    size += len(JSONResponse(records).body) + 512 * len(records)  # stamped run/submission IDs
    for target_id, target in targets.items():
        rows = gradescope.grading_rows(target_id, target, revision)
        # Include every configured row, even if the target fails. Allow for the
        # request ID, numeric scores and bounded error message added to each row.
        size += len(JSONResponse(rows).body) + 512 * len(rows) + 2048
    return local_client.MAX_GRADING_RESPONSE_BYTES - size


def resolve_input(payload: FeedbackInput, target) -> ResolvedSource:
    """Apply the database target's file and artifact scope before reserving quota."""
    if payload.files:
        primary, *context = payload.files
        source_patterns = target.analysis.source_globs or (target.source_path,)
        if (target.analysis.max_source_files is not None
                and sum(matches_path(source.path, source_patterns) for source in payload.files)
                > target.analysis.max_source_files):
            raise ValueError("source files exceed the configured primary-file limit")
        if (not matches_path(primary.path, source_patterns)
                or any(not matches_path(source.path, source_patterns + target.analysis.context_globs)
                       for source in context)):
            raise ValueError("source files are outside the configured target")
        resolved = ResolvedSource(source_file=primary, context_files=tuple(context))
    else:
        resolved = ResolvedSource(source_file=CodeFile(path=target.source_path, content=payload.code))
    sources = {source.label: source for source in target.analysis.evidence_sources}
    if target.analysis.behavior is not None and payload.functional_test_feedback:
        raise ValueError("use scoped evidence for configured behaviors")
    kinds = {"gradescope": "runtime", "junit": "runtime", "jacoco": "coverage", "text": "diagnostic"}
    for item in payload.evidence:
        source = sources.get(item.source)
        if source is None:
            raise ValueError("evidence source is outside the configured target")
        if target.analysis.behavior is not None:
            # Opaque logs have no row visibility or reliable student-facing scope.
            if source.format != "gradescope" or item.kind != "runtime":
                raise ValueError("student feedback requires scoped Gradescope observations")
            if target.analysis.behavior == "functional_test_feedback" and (
                source.label != target.analysis.functional_result_source or item.status != "fail"
            ):
                raise ValueError("functional feedback requires the selected failed rows")
        if (item.kind == "diagnostic" and item.status == "unknown"
                and item.test_id is None and item.scope == source.label):
            continue
        selector = "\n".join((item.test_id or "", item.scope))
        if (item.kind != kinds[source.format]
                or (source.include and not any(re.search(pattern, selector) for pattern in source.include))):
            raise ValueError("evidence is outside the configured target")
    return resolved


def error(code: str, status: int, request_id: str | None = None, *, accounting=None):
    body = {"error": {"code": code, "message": MESSAGES.get(code, MESSAGES["backend_error"])}}
    if request_id is not None:
        body["request_id"] = request_id
    if accounting is not None:
        body["accounting"] = accounting.model_dump(mode="json")
    return JSONResponse(body, status_code=status,
                        headers={"Cache-Control": "no-store"})


def default_provider_factory(config, assignment, *, pitt_api_key=None):
    options = {"model": assignment.model, "timeout_seconds": config.timeout_seconds,
               "max_output_tokens": config.max_output_tokens}
    if assignment.engine == "vertex":
        from ai_grading.ILLMProvider.VertexProvider import VertexProvider
        return VertexProvider(
            **options, project=config.project, location=config.location,
            service_account=config.service_account,
        )
    if assignment.engine == "pitt":
        from ai_grading.ILLMProvider.PittProvider import PittProvider
        return PittProvider(**options, provider=assignment.provider, api_key=pitt_api_key)
    if assignment.engine == "gemini_api":
        from ai_grading.ILLMProvider.GeminiProvider import GeminiProvider
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key or not key.strip():
            raise ProviderError("backend_auth_unavailable", MESSAGES["backend_auth_unavailable"])
        return GeminiProvider(**options, api_key=key)
    raise ValueError("Unsupported feedback engine")


def create_feedback_router(state_path: str | Path, *, assignment_loader,
                           provider_factory=None, grading_repository=None,
                           pitt_key_resolver=None) -> APIRouter:
    factory = provider_factory or default_provider_factory
    runtime = SimpleNamespace()

    @asynccontextmanager
    async def lifespan(app):
        # The lifetime file lock refuses a second process/worker on this state.
        try:
            store = JsonStore(Path(state_path))
        except Exception:
            raise RuntimeError("Backend state is unavailable or locked.") from None
        runtime.store = store
        app.state.feedback_store = store
        runtime.providers = {}
        runtime.provider_lock = threading.Lock()
        runtime.inflight = 0
        runtime.tasks = set()
        runtime.jobs = {}
        runtime.grading_runs = set()
        runtime.grading_outcome_locks = WeakValueDictionary()
        pool = ThreadPoolExecutor(max_workers=store.config.max_concurrency,
                                  thread_name_prefix="feedback")
        runtime.pool = pool
        try:
            if grading_repository is not None:
                store.allow_pitt_models()
            yield
        finally:
            if runtime.grading_runs:
                await asyncio.gather(*list(runtime.grading_runs), return_exceptions=True)
            if runtime.jobs:
                await asyncio.gather(*list(runtime.jobs.values()), return_exceptions=True)
            # A timed-out HTTP request may still have a running SDK call. Retain
            # its slot and worker lock until it exits; never start an extra call.
            if runtime.tasks:
                await asyncio.gather(*list(runtime.tasks), return_exceptions=True)
            pool.shutdown(wait=True)
            for provider in runtime.providers.values():
                try:
                    provider.close()
                except Exception:
                    LOG.warning("provider_close_failed")
            store.close()

    router = APIRouter(lifespan=lifespan)

    def db_submission(assignment_id, payload: SubmissionContext):
        if grading_repository is None:
            return None
        ref = SubmissionRef(
            assignment_id=UUID(assignment_id),
            platform_submission_id=payload.platform_submission_id,
            submitted_at=payload.submitted_at,
            student_identifier=payload.student_identifier,
        )
        submission_id = grading_repository.upsert_submission(ref)
        grading_repository.create_targets_default_needed(submission_id)
        return submission_id

    def persist_feedback(submission_id, payload, target, result, cache_digest):
        """Write one successful provider response to the grading repository."""
        usage = result.get("usage") or {}
        cached_result = None
        if cache_digest is not None and result.get("grading_status") == "complete":
            cached_result = {
                key: value for key, value in result.items() if key not in ("request_id", "usage", "accounting")
            }
        response = ProviderFeedback(
            provider=result["provider"],
            model_name=result["model_name"],
            body=format_feedback_body(result, payload.evidence),
            prompt_version="v1",
            suggested_score=result.get("suggested_score"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_digest=cache_digest if cached_result is not None else None,
            cached_result=cached_result,
        )
        grading_repository.save_feedback(
            submission_id,
            target.identifier,
            FeedbackResult(
                rubric_id=UUID(str(target.rubric.id)),
                responses=(response,),
                selected_index=0,
                selection_reason="single configured provider response",
            ),
        )

    def persist_failure(assignment_id, assignment, payload, target, message):
        if grading_repository is None or payload.submission is None:
            return
        try:
            submission_id = db_submission(assignment_id, payload.submission)
            grading_repository.mark_target_failed(submission_id, target.identifier, message)
        except Exception:
            LOG.warning("feedback_failure_not_recorded", exc_info=True)

    def authenticate(request):
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            raise StoreError("unauthorized", 401)
        scheme, _, token = headers[0].partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256 or not token.isascii():
            raise StoreError("unauthorized", 401)
        return runtime.store.authenticate(token)

    def check_assignment(assignment, assignment_id):
        if assignment.id != assignment_id or not assignment.enabled:
            raise StoreError("assignment_forbidden", 403)
        if assignment.model not in runtime.store.config.allowed_models.get(assignment.engine, []):
            raise StoreError("assignment_config_unavailable", 503)
        return assignment

    async def load_assignment(assignment_id):
        try:
            assignment = await asyncio.to_thread(assignment_loader, assignment_id)
            return check_assignment(assignment, assignment_id)
        except StoreError:
            raise
        except Exception:
            raise StoreError("assignment_config_unavailable", 503) from None

    @router.get("/v1/assignment/config")
    async def assignment_config(request: Request):
        """Return collection rules for the authenticated assignment, without using quota."""
        try:
            assignment = await load_assignment(authenticate(request))
        except StoreError as exc:
            return error(exc.code, exc.status_code)
        return JSONResponse({"assignment_id": assignment.id,
                             "revision": assignment_revision(assignment), "targets": collection_targets(assignment)},
                            headers={"Cache-Control": "no-store"})

    async def read_body(request, limit):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise OverflowError
            body.extend(chunk)
        return bytes(body)

    def invoke(assignment_id, rid, assignment, grading_request, deadline, pitt_api_key):
        if time.monotonic() >= deadline:
            raise TimeoutError
        transient = assignment.engine == "pitt" and provider_factory is None
        if transient:
            provider = default_provider_factory(runtime.store.config, assignment,
                                                pitt_api_key=pitt_api_key)
        else:
            with runtime.provider_lock:
                key = (assignment_id, assignment.engine, assignment.provider, assignment.model)
                if key not in runtime.providers:
                    runtime.providers[key] = factory(runtime.store.config, assignment)
                provider = runtime.providers[key]
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError
            response = provider.generate_feedback(grading_request)
        except ProviderError as exc:
            if exc.usage is not None:
                runtime.store.record_usage(assignment_id, rid, exc.usage, exc.model_name or assignment.model)
            raise
        finally:
            if transient:
                provider.close()
        runtime.store.record_usage(assignment_id, rid, response.usage, response.model_name)
        return response

    def processing_response(request_id):
        return JSONResponse(
            {"request_id": request_id, "status": "processing"}, status_code=202,
            headers={"Cache-Control": "no-store", "Retry-After": "2",
                     "Preference-Applied": "respond-async"},
        )

    async def finish_feedback(future, assignment, target, payload, submission_id, cache_digest, timeout):
        store = runtime.store
        assignment_id, rid = assignment.id, payload.request_id
        try:
            response = await asyncio.wait_for(asyncio.shield(future), timeout)
            if (target.max_score is not None and response.suggested_score is not None
                    and response.suggested_score > target.max_score):
                raise ProviderError("invalid_provider_response", "Suggested score exceeds the configured maximum.")
            result = {
                "request_id": rid, "status": "completed",
                "engine": assignment.engine, "provider": assignment.provider,
                "requested_model": assignment.model,
                "feedback_text": response.feedback_text, "model_name": response.model_name,
                "usage": {"input_tokens": response.input_tokens,
                          "output_tokens": response.output_tokens},
                "accounting": store.get_request(assignment_id, rid).accounting.model_dump(mode="json"),
            }
            if response.items:
                result["items"] = [asdict(item) for item in response.items]
            if target.analysis.behavior is not None:
                result["behavior"] = target.analysis.behavior
                result["grading_status"] = response.grading_status
            if response.failure_feedback:
                result["failure_feedback"] = [asdict(item) for item in response.failure_feedback]
            if target.max_score is not None:
                result["suggested_score"] = response.suggested_score
                result["max_score"] = target.max_score
            if submission_id is not None:
                await asyncio.to_thread(
                    persist_feedback, submission_id, payload, target, result, cache_digest,
                )
            store.complete(assignment_id, rid, result)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except asyncio.CancelledError:
            # Only cancellation of the job itself interrupts generation; an HTTP
            # disconnect cannot cancel the independently tracked job.
            store.fail(assignment_id, rid, code="upstream_timeout")
            if payload.submission is not None and grading_repository is not None:
                await asyncio.to_thread(
                    persist_failure, assignment_id, assignment, payload, target, "upstream_timeout",
                )
            raise
        except TimeoutError:
            code = "upstream_timeout"
        except ProviderError as exc:
            LOG.warning("feedback_provider_failed request_id=%s code=%s message=%s",
                        rid, exc.code, exc.message)
            code = ("upstream_timeout" if exc.code == "timeout" else
                    "backend_auth_unavailable" if exc.code == "backend_auth_unavailable" else "model_error")
        except Exception:
            code = "model_error"
        failed_to_save_request = False
        try:
            store.fail(assignment_id, rid, code=code)
        except Exception:
            failed_to_save_request = True
        if payload.submission is not None and grading_repository is not None:
            await asyncio.to_thread(
                persist_failure, assignment_id, assignment, payload, target, code,
            )
        if failed_to_save_request:
            return error("backend_error", 500, rid)
        LOG.warning("feedback_request_failed code=%s request_id=%s", code, rid)
        return error(code, 504 if code == "upstream_timeout" else 502, rid,
                     accounting=store.get_request(assignment_id, rid).accounting)

    async def handle_feedback(request: Request):
        store = runtime.store
        config = store.config
        respond_async = "respond-async" in {
            preference.strip().lower()
            for preference in request.headers.get("prefer", "").split(",")
        }
        try:
            assignment_id = authenticate(request)
        except StoreError as exc:
            return error(exc.code, exc.status_code)

        try:
            raw = await asyncio.wait_for(read_body(request, config.max_body_bytes),
                                         timeout=config.timeout_seconds)
            payload = FeedbackInput.model_validate_json(raw)
        except OverflowError:
            return error("input_too_large", 413)
        except ValueError:
            return error("invalid_request", 422)
        except TimeoutError:
            return error("invalid_request", 408)
        except Exception:
            return error("invalid_request", 400)
        return await execute_target(assignment_id, payload, respond_async=respond_async)

    async def execute_target(assignment_id, payload, *, assignment=None, respond_async=False, deadline=None,
                             cached_only=False):
        store = runtime.store
        config = store.config
        rid = payload.request_id
        if (len(payload.code or "") + sum(len(source.content) for source in payload.files) > config.max_code_chars
                or len(payload.guidance or "") > config.max_guidance_chars):
            return error("input_too_large", 413, rid)
        try:
            if assignment is None:
                assignment = await load_assignment(assignment_id)
            existing = store.get_request(assignment_id, rid)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
        if cached_only and existing is None:
            return None
        # A new stale request must refresh even if its target was removed.
        if (existing is None and payload.config_revision is not None
                and payload.config_revision != assignment_revision(assignment)):
            return error("config_changed", 409, rid)
        target = assignment.targets.get(payload.target_id)
        if target is None:
            return error("target_forbidden", 403, rid)
        if assignment.grading_mode == "NIGHTLY":
            if payload.submission is None:
                return error("invalid_request", 422, rid)
            if grading_repository is None:
                return error("backend_error", 503, rid)
        submission_id = None
        if existing is None and payload.submission is not None and grading_repository is not None:
            submission_id, skipped = await asyncio.to_thread(
                submission_for_target, assignment_id, payload.submission, target.identifier,
            )
            if skipped:
                return JSONResponse({"request_id": rid, **skipped}, headers={"Cache-Control": "no-store"})
        content = payload.model_dump(mode="json", exclude={"request_id", "run_id"})
        if payload.config_revision is None:
            content.pop("config_revision")
        content["guidance"] = payload.guidance or ""
        # Keep legacy code requests byte-for-byte compatible with stored digests.
        for field in ("files", "evidence"):
            if not content[field]:
                content.pop(field)
        if payload.code is None:
            content.pop("code")
        if not payload.functional_test_feedback:
            content.pop("functional_test_feedback")
        digest = json_digest(content)
        # An accepted request retains its configuration snapshot while polling.
        # Current assignment/target authorization above still applies to replays.
        pitt_api_key = None
        cache_digest = None
        cached_result = None
        if existing is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            if target.analysis.behavior is not None and payload.config_revision is None:
                return error("invalid_request", 422, rid)
            guidance = target.rubric.feedback_guidance
            if payload.guidance:
                guidance += "\nOptional client feedback guidance:\n" + payload.guidance
            try:
                resolved = resolve_input(payload, target)
                grading_request = FeedbackRequest(
                    target=GradingTarget(identifier=target.identifier, kind=TargetKind(target.kind),
                                         id=payload.target_id, max_score=target.max_score),
                    rubric=Rubric(criteria_text=target.rubric.criteria_text, feedback_guidance=guidance,
                                  id=target.rubric.id, version=target.rubric.version,
                                  grading_target_id=payload.target_id),
                    resolved=resolved, analysis=target.analysis, evidence=tuple(payload.evidence),
                    functional_test_feedback=tuple(payload.functional_test_feedback),
                )
            except ValueError:
                return error("invalid_request", 422, rid)
            try:
                PromptBuilder().prepare(grading_request)
            except ValueError:
                return error("input_too_large", 413, rid)
            if (submission_id is not None and payload.run_id and payload.config_revision
                    and target.analysis.behavior is not None):
                # The SQL lookup already identifies the submission. Its metadata may vary between runs.
                cache_digest = json_digest({key: value for key, value in content.items() if key != "submission"})
                cached_result = await asyncio.to_thread(
                    grading_repository.get_cached_feedback, submission_id, target.identifier, cache_digest,
                )
            if cached_result is None and assignment.engine == "pitt":
                try:
                    if pitt_key_resolver is not None:
                        pitt_api_key = await asyncio.to_thread(pitt_key_resolver, assignment)
                    elif assignment.pitt_key_source == "custom":
                        return error("backend_auth_unavailable", 503, rid)
                    elif provider_factory is None:
                        pitt_api_key = os.environ.get("PITT_API_KEY", "").strip()
                        if not pitt_api_key:
                            return error("backend_auth_unavailable", 503, rid)
                except StoreError as exc:
                    return error(exc.code, exc.status_code, rid)
                except ValueError:
                    return error("backend_auth_unavailable", 503, rid)
        # No await between checking capacity, reserving quota and occupying it.
        # Replays must still work at full capacity and after quota exhaustion.
        if existing is None and deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError
        try:
            if existing is None and cached_result is None and runtime.inflight >= config.max_concurrency:
                return error("busy", 429, rid)
            record, created = store.reserve(assignment_id, rid, digest, payload.target_id,
                                            assignment.model, engine=assignment.engine,
                                            provider=assignment.provider, run_id=payload.run_id,
                                            cached_result=cached_result)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
        except Exception:
            return error("backend_error", 500, rid)
        if not created:
            if record.status == "processing" and deadline is not None:
                job = runtime.jobs.get((assignment_id, rid))
                if job is not None:
                    return await asyncio.wait_for(asyncio.shield(job), max(0, deadline - time.monotonic()))
            if record.status == "completed":
                result = {"engine": record.engine, "provider": record.provider,
                          "requested_model": record.model, **record.result}
                if record.accounting is not None:
                    result["accounting"] = record.accounting.model_dump(mode="json")
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if record.status == "processing" and respond_async:
                return processing_response(rid)
            if record.status in ("processing", "interrupted"):
                return error(record.status, 409, rid, accounting=record.accounting)
            code = record.error_code or "model_error"
            return error(code, 504 if code == "upstream_timeout" else 502, rid,
                         accounting=record.accounting)

        runtime.inflight += 1
        future = asyncio.get_running_loop().run_in_executor(
            runtime.pool, invoke, assignment_id, rid, assignment, grading_request,
            time.monotonic() + config.timeout_seconds, pitt_api_key,
        )
        runtime.tasks.add(future)

        def release(done):
            runtime.inflight -= 1
            runtime.tasks.discard(done)
            # Observe exceptions even when the HTTP request has timed out.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(release)
        job = asyncio.create_task(finish_feedback(
            future, assignment, target, payload, submission_id, cache_digest, config.timeout_seconds,
        ))
        runtime.jobs[assignment_id, rid] = job

        def forget_job(done):
            runtime.jobs.pop((assignment_id, rid), None)
            if not done.cancelled() and done.exception() is not None:
                LOG.error("feedback_job_failed")

        job.add_done_callback(forget_job)
        if respond_async:
            return processing_response(rid)
        if deadline is not None:
            return await asyncio.wait_for(asyncio.shield(job), max(0, deadline - time.monotonic()))
        return await asyncio.shield(job)

    @router.post("/v1/feedback")
    async def feedback(request: Request):
        # Keep unexpected exception details out of responses and server logs,
        # without changing exception handling for existing web routes.
        try:
            return await handle_feedback(request)
        except Exception:
            LOG.error("backend_error")
            return error("backend_error", 500)

    async def record_outcome(assignment, payload):
        if payload.completion_reason == "not_needed" and any(
            target.rubric is not None
            and target.analysis_config.get("behavior") != "functional_test_feedback"
            for target in assignment.targets
        ):
            raise StoreError("invalid_request", 422)
        submission_id = await asyncio.to_thread(db_submission, str(assignment.id), payload.submission)
        if (payload.ai_status == "SUCCEEDED" and payload.completion_reason == "processed"
                and assignment.grading_mode == "NIGHTLY"):
            action = await asyncio.to_thread(grading_repository.get_ai_execution_action, submission_id)
            if action == "deferred":
                raise StoreError("ai_not_authorized", 409)
        await asyncio.to_thread(grading_repository.set_submission_ai_status, submission_id, payload.ai_status)

    async def record_tests(assignment, payload):
        submission_id = await asyncio.to_thread(db_submission, str(assignment.id), payload.submission)
        outcomes = map_outcomes(assignment.targets, [row.model_dump() for row in payload.tests], payload.visibility)
        recorded = await asyncio.to_thread(grading_repository.record_test_run, submission_id, payload.run_id, outcomes)
        return recorded, len(outcomes)

    def submission_for_target(assignment_id, submission, target_name):
        submission_id = db_submission(assignment_id, submission)
        action = grading_repository.get_ai_execution_action(submission_id)
        if action == "deferred":
            action = grading_repository.defer_submission_target(submission_id, target_name)
        if action != "execute":
            return submission_id, {
                "status": action,
                "reason": "awaiting_nightly_authorization" if action == "deferred" else "inactive_submission",
            }
        return submission_id, None

    async def wait_for_target(assignment, payload, deadline, *, cached_only=False):
        # The legacy hook retries only temporary capacity errors. Keep this wait
        # inside the run so Gradescope receives a single final response.
        while True:
            response = await execute_target(assignment.id, payload, assignment=assignment,
                                            deadline=deadline, cached_only=cached_only)
            if response is None:
                return None
            if response.status_code == 200:
                return local_client.read_feedback_response(response.body, payload.request_id, None)
            data = json.loads(response.body)
            if data["error"]["code"] != "busy":
                raise local_client._safe_http_error(
                    response.status_code, accounting=data.get("accounting"),
                    cost_unknown=data["error"]["code"] in {
                        "model_error", "upstream_timeout", "processing", "interrupted", "backend_error",
                    },
                )
            if time.monotonic() >= deadline:
                raise TimeoutError
            if runtime.tasks:
                await asyncio.wait(tuple(runtime.tasks), timeout=max(0, deadline - time.monotonic()),
                                   return_when=asyncio.FIRST_COMPLETED)

    def prepare_target_input(run, base, blocked, target_id, analysis, revision, submission):
        evidence = gradescope.collect_evidence(None, analysis, base, blocked, reports=run.reports)
        files = gradescope.collect_uploaded_files(run.files, analysis, symlink_dirs=run.symlink_dirs,
                                                 glob_star_in_segment=run.glob_star_in_segment)
        rid = local_client.stable_request_id(
            target_id, files=files, evidence=evidence,
            namespace=f"{revision}:{run.submission_id or ''}:{run.run_id}",
        )
        # Apply the same JSON size and per-target validation as the original client request.
        fields = {"request_id": rid, "run_id": run.run_id, "target_id": target_id,
                  "guidance": "", "config_revision": revision, "files": files}
        if evidence:
            fields["evidence"] = evidence
        if submission is not None:
            fields["submission"] = submission
        raw = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        return rid, raw

    async def grade_targets(run, assignment, base, results, blocked, records, deadline, order_outcome):
        revision = assignment_revision(assignment)
        failed = False
        deferred = None
        submission = run.submission.model_dump(mode="json") if run.submission else None
        verification_deadline = deadline + GRADING_REPLAY_GRACE_SECONDS
        attempts = {}
        checked_action = False

        async def prepare(target_id, attempt):
            remaining = verification_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                rid, raw = await asyncio.to_thread(
                    prepare_target_input, run, base, blocked, target_id, targets[target_id]["analysis"],
                    revision, submission,
                )
            attempt.record["request_id"] = rid
            if len(raw) > runtime.store.config.max_body_bytes:
                raise local_client._safe_http_error(413)
            try:
                attempt.payload = FeedbackInput.model_validate_json(raw)
            except ValueError:
                raise local_client._safe_http_error(422) from None

        async def check_skip(target_id):
            nonlocal checked_action
            if checked_action or run.submission is None:
                return None
            remaining = verification_deadline - time.monotonic()
            if remaining <= 0:
                raise ReplayVerificationTimeout
            try:
                async with asyncio.timeout(remaining):
                    _, skipped = await asyncio.to_thread(submission_for_target, assignment.id, run.submission,
                                                         targets[target_id]["identifier"])
            except TimeoutError:
                raise ReplayVerificationTimeout from None
            checked_action = True
            return skipped

        async def recover(retries):
            nonlocal room
            # SQL or collection can fail while a sibling completes this target. Order
            # the last cache check with aggregate writes, after all normal model work.
            try:
                async with asyncio.timeout(max(0, verification_deadline - time.monotonic())):
                    await order_outcome()
            except TimeoutError:
                for attempt in retries.values():
                    attempt.error = ReplayVerificationTimeout()
                return
            accepted = runtime.store.accepted_run_targets(assignment.id, run.run_id)
            for target_id, attempt in retries.items():
                try:
                    if attempt.payload is None and target_id in accepted:
                        try:
                            await prepare(target_id, attempt)
                        except TimeoutError:
                            raise ReplayVerificationTimeout from None
                    if attempt.payload is not None:
                        response = await wait_for_target(assignment, attempt.payload, 0, cached_only=True)
                        if response is not None:
                            if measure_responses:
                                size = 2 * len(JSONResponse(response).body)
                                if size > room:
                                    raise local_client.ClientError("The remaining AI results exceed the Grading response limit.")
                                room -= size
                            attempt.response, attempt.error = response, None
                            continue
                    if isinstance(attempt.error, TimeoutError):
                        skipped = await check_skip(target_id)
                        if skipped:
                            attempt.response, attempt.error = skipped, None
                            break
                except Exception as exc:
                    attempt.error = exc.with_traceback(None)

        targets = collection_targets(assignment)
        room = grading_response_room(records, targets, revision)
        if room < 0:
            raise StoreError("input_too_large", 413)
        # Provider text can appear in both a public grading row and private evidence.
        # Only large configurations need to track the actual space used after each call.
        max_target_bytes = 2 * local_client.MAX_RESPONSE_BYTES
        measure_responses = not run.collection_error and len(targets) * max_target_bytes > room
        retries = {}
        for target_id in targets:
            attempt = attempts[target_id] = GradingTargetAttempt({"target_id": target_id})
            try:
                if measure_responses and room < max_target_bytes:
                    raise local_client.ClientError("The remaining AI results exceed the Grading response limit.")
                if run.collection_error:
                    attempt.response = await check_skip(target_id)
                    if not attempt.response:
                        raise local_client.ClientError("The submission could not be uploaded for AI grading.")
                else:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    await prepare(target_id, attempt)
                    attempt.response = await wait_for_target(
                        assignment, attempt.payload, min(deadline, time.monotonic() + 270),
                    )
                    attempt.payload = None
                if measure_responses:
                    room -= 2 * len(JSONResponse(attempt.response).body)
                if attempt.response.get("status") in {"deferred", "inactive"}:
                    break
            except Exception as exc:
                attempt.error = exc.with_traceback(None)
                if not run.collection_error and (attempt.payload is not None or isinstance(exc, TimeoutError)):
                    retries[target_id] = attempt
        if retries:
            await recover(retries)

        unverified = False
        for target_id, target in targets.items():
            if deferred:
                results["tests"].extend(gradescope.grading_rows(target_id, target, revision, error=deferred))
                continue
            attempt = attempts[target_id]
            record, response = attempt.record, attempt.response
            try:
                if attempt.error is not None:
                    raise attempt.error
                if response.get("status") in {"deferred", "inactive"}:
                    deferred = ("AI grading is awaiting scheduled authorization."
                                if response["status"] == "deferred" else
                                "This submission is inactive; AI grading was not run.")
                    rows = gradescope.grading_rows(target_id, target, revision, error=deferred)
                else:
                    rows = gradescope.grading_rows(target_id, target, revision, response)
                record.update(response)
            except Exception as exc:
                if isinstance(exc, ReplayVerificationTimeout):
                    unverified = True
                else:
                    failed = True
                if isinstance(exc, ReplayVerificationTimeout):
                    detail = "The time budget expired before the target's existing grading outcome could be verified."
                elif isinstance(exc, TimeoutError):
                    detail = "The AI time budget was exhausted."
                    if "request_id" in record:
                        accepted = runtime.store.get_request(assignment.id, record["request_id"])
                        if accepted is not None:
                            record["accounting"] = (accepted.accounting.model_dump(mode="json")
                                                    if accepted.accounting else None)
                elif isinstance(exc, local_client.ClientError):
                    detail = str(exc)
                    if exc.accounting is not None or exc.cost_unknown:
                        record["accounting"] = exc.accounting
                else:
                    detail = "Could not collect or process the configured AI inputs."
                record["error"] = detail
                if response and "accounting" in response:
                    record["accounting"] = response["accounting"]
                rows = gradescope.grading_rows(target_id, target, revision, error=detail)
            results["tests"].extend(rows)
            records.append(record)
        return failed, deferred, revision, unverified

    @router.post("/v1/grading/run")
    async def grading_run(request: Request):
        started = time.monotonic()
        try:
            assignment_id = authenticate(request)
            raw = await asyncio.wait_for(read_body(request, local_client.MAX_GRADING_BODY_BYTES),
                                         timeout=runtime.store.config.timeout_seconds)
            run = await asyncio.to_thread(GradingRunInput.model_validate_json, raw)
            del raw
            base, previous_ai, blocked, records = await asyncio.to_thread(gradescope.clean_results, run.results)
        except StoreError as exc:
            return error(exc.code, exc.status_code)
        except OverflowError:
            return error("input_too_large", 413)
        except TimeoutError:
            return error("invalid_request", 408)
        except (TypeError, ValueError, KeyError, AttributeError):
            return error("invalid_request", 422)
        if grading_repository is None:
            return error("backend_error", 503)
        try:
            snapshot = await asyncio.to_thread(read_active_assignment, grading_repository, assignment_id)
        except StoreError as exc:
            return error(exc.code, exc.status_code)
        if snapshot.ai_mode != "GRADING":
            return error("assignment_forbidden", 403)
        if snapshot.grading_mode == "NIGHTLY" and run.submission is None:
            return error("invalid_request", 422)
        deadline = started + run.budget_seconds
        job = asyncio.create_task(ordered_grading_run(run, snapshot, assignment_id, base, previous_ai,
                                                      blocked, records, deadline))
        # The HTTP stream can disconnect while later targets and the aggregate DB write still need to run.
        runtime.grading_runs.add(job)
        job.add_done_callback(runtime.grading_runs.discard)
        # A normal upload reserves five seconds in the hook. Leave time to write
        # results after replay checks and delivery. Collection failures have no reserve.
        send_by = deadline if run.collection_error else deadline + GRADING_REPLAY_GRACE_SECONDS + 1
        first_heartbeat = min(started + GRADING_HEARTBEAT_SECONDS, send_by)
        await asyncio.wait({job}, timeout=max(0, first_heartbeat - time.monotonic()))
        if job.done():
            return job.result()
        if time.monotonic() >= send_by:
            return error("grading_response_timeout", 504)

        async def stream_result():
            while not job.done():
                remaining = send_by - time.monotonic()
                if remaining <= 0:
                    yield error("grading_response_timeout", 504).body
                    return
                # Newlines are valid leading JSON whitespace. The stream contains one final JSON object.
                yield b"\n"
                await asyncio.wait({job}, timeout=min(GRADING_HEARTBEAT_SECONDS, remaining))
            yield job.result().body

        # cloudflared flushes this media type when the origin omits Content-Length.
        return StreamingResponse(stream_result(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store, no-transform"})

    async def ordered_grading_run(run, snapshot, assignment_id, base, previous_ai, blocked, records, deadline):
        key = assignment_id, run.submission_id, run.run_id
        lock = runtime.grading_outcome_locks.setdefault(key, asyncio.Lock())
        async with AsyncExitStack() as scope:
            ordered = False

            async def order_outcome():
                nonlocal ordered
                if not ordered:
                    await scope.enter_async_context(lock)
                    ordered = True

            try:
                return await finish_grading_run(run, snapshot, assignment_id, base, previous_ai, blocked, records,
                                                deadline, order_outcome)
            except Exception:
                LOG.error("grading_run_failed run_id=%s", run.run_id)
                return error("backend_error", 500)

    async def finish_grading_run(run, snapshot, assignment_id, base, previous_ai, blocked, records,
                                 deadline, order_outcome):
        failed = bool(records)
        if run.submission is not None and (not run.collection_error or "tests" in run.results):
            try:
                report = local_client.grading_test_payload(run.run_id, run.submission.model_dump(mode="json"), base)
                await record_tests(snapshot, TestResultsInput.model_validate({**report, "submission": run.submission}))
            except Exception:
                # Functional results remain available in Gradescope if dashboard recording fails.
                records.append({"error": "Test results were not recorded in the grading database."})
                LOG.warning("grading_run_test_results_failed run_id=%s", run.run_id)

        revision = None
        deferred = None
        unverified = False
        try:
            assignment = check_assignment(feedback_assignment_config(snapshot), assignment_id)
            if any(target.analysis.behavior != "ai_grading" for target in assignment.targets.values()):
                raise StoreError("assignment_config_unavailable", 503)
            # Evidence always sees the original functional rows while the result
            # accumulates new grading rows in its own list.
            results = {**base, "tests": list(base["tests"])}
            target_failed, deferred, revision, unverified = await grade_targets(
                run, assignment, base, results, blocked, records, deadline, order_outcome,
            )
            failed |= target_failed
        except StoreError as exc:
            failed = True
            results = base
            detail = ("The configured grading results exceed the response limit." if exc.code == "input_too_large"
                      else "Could not load a valid assignment configuration.")
            # A short failure response still has to fit if the input supplied
            # many stale inline markers whose cleanup generated diagnostics.
            if grading_response_room(records, {}, None) < 0:
                records[:] = [{"error": f"Could not return all {len(records)} cleanup diagnostics."}]
            records.append({"error": detail})
            gradescope.restore_pending_rows(results, previous_ai, detail)
        gradescope.finalize_grading(results, revision, unknown=revision is None)
        for record in records:
            record.update(run_id=run.run_id, submission_id=run.submission_id)
        body = {"results": results, "ai_evidence": records}
        if run.submission is not None and (failed or (not deferred and not unverified)):
            no_work = not any(target.rubric is not None for target in snapshot.targets)
            completion = AiCompleteInput(submission=run.submission,
                                         ai_status="FAILED" if failed else "SUCCEEDED",
                                         completion_reason="not_needed" if no_work and not failed else "processed")
            try:
                await order_outcome()
                await record_outcome(snapshot, completion)
            except Exception:
                LOG.error("grading_run_completion_failed run_id=%s", run.run_id)
                records.append({"run_id": run.run_id, "submission_id": run.submission_id,
                                "error": "AI outcome was not recorded in the grading database."})
        response = JSONResponse(body, headers={"Cache-Control": "no-store"})
        if revision is None and len(response.body) + GRADING_HEARTBEAT_BYTES > local_client.MAX_GRADING_RESPONSE_BYTES:
            return error("input_too_large", 413)
        return response

    @router.post("/v1/submissions/{platform_submission_id}/ai-complete")
    async def ai_complete(platform_submission_id: str, request: Request):
        """Record the connector's aggregate outcome for this submission execution."""
        store = runtime.store
        try:
            assignment_id = authenticate(request)
            raw = await asyncio.wait_for(read_body(request, store.config.max_body_bytes),
                                         timeout=store.config.timeout_seconds)
            payload = AiCompleteInput.model_validate_json(raw)
        except StoreError as exc:
            return error(exc.code, exc.status_code)
        except Exception:
            return error("invalid_request", 422)
        if payload.submission.platform_submission_id != platform_submission_id:
            return error("invalid_request", 422)
        if grading_repository is None:
            return error("backend_error", 503)
        try:
            # Outcome reporting must still work when model or rubric configuration is invalid.
            assignment = await asyncio.to_thread(read_active_assignment, grading_repository, assignment_id)
            await record_outcome(assignment, payload)
        except StoreError as exc:
            return error(exc.code, exc.status_code)
        except Exception:
            LOG.error("ai_completion_failed", exc_info=True)
            return error("backend_error", 503)
        return JSONResponse(
            {"status": "completed", "submission_id": platform_submission_id,
             "ai_status": payload.ai_status},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/v1/submissions/{platform_submission_id}/test-results")
    async def test_results(platform_submission_id: str, request: Request):
        """Record one run's test results and opt-outs for the instructor dashboard."""
        store = runtime.store
        try:
            assignment_id = authenticate(request)
            raw = await asyncio.wait_for(read_body(request, TEST_RESULTS_MAX_BYTES),
                                         timeout=store.config.timeout_seconds)
            payload = TestResultsInput.model_validate_json(raw)
        except StoreError as exc:
            return error(exc.code, exc.status_code)
        except OverflowError:
            return error("input_too_large", 413)
        except Exception:
            return error("invalid_request", 422)
        if payload.submission.platform_submission_id != platform_submission_id:
            return error("invalid_request", 422)
        if grading_repository is None:
            return error("backend_error", 503)
        try:
            assignment = await asyncio.to_thread(read_active_assignment, grading_repository, assignment_id)
            recorded, count = await record_tests(assignment, payload)
        except StoreError as exc:
            return error(exc.code, exc.status_code)
        except Exception:
            LOG.error("test_results_failed", exc_info=True)
            return error("backend_error", 503)
        return JSONResponse(
            {"status": "recorded", "submission_id": platform_submission_id,
             "recorded": recorded, "targets": count},
            headers={"Cache-Control": "no-store"},
        )

    return router


def create_app(state_path: str | Path, *, assignment_loader, provider_factory=None) -> FastAPI:
    """Local entry point using the same router, without importing web.main/db/ui."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_feedback_router(
        state_path, assignment_loader=assignment_loader, provider_factory=provider_factory,
    ))

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def main():
    import argparse
    import uvicorn
    from .feedback_assignment import HttpAssignmentConfigReader

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("ai_grading/.local-vertex/backend/state.json"))
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--assignment-api-url", default=os.environ.get("ASSIGNMENT_API_URL"),
                        help="Web API HTTPS or loopback origin; defaults to ASSIGNMENT_API_URL")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        reader = HttpAssignmentConfigReader(
            args.assignment_api_url, os.environ.get("INTEGRATION_API_TOKEN"),
        )
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(create_app(args.state, assignment_loader=reader), host="127.0.0.1", port=args.port,
                workers=1, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()

"""Assignment-token feedback using DB configuration and local runtime state."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from types import SimpleNamespace
import threading
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_grading.models import (
    CodeFile, Evidence, FeedbackRequest, GradingTarget, ResolvedSource, Rubric, TargetKind,
)
from ai_grading.FileResolver import FileResolver
from ai_grading.PromptBuilder import PromptBuilder
from ai_grading.ILLMProvider.ILLMProvider import ProviderError
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


TEST_RESULTS_MAX_BYTES = 2_097_152


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


def assignment_revision(assignment) -> str:
    """Bind collected inputs and replay IDs to the grading configuration."""
    content = {"assignment": assignment.model_dump(mode="json"),
               "prompts": [PromptBuilder.version, PromptBuilder.extended_version,
                           PromptBuilder.per_test_version, PromptBuilder.grading_version,
                           PromptBuilder.functional_version]}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        runtime.jobs = set()
        pool = ThreadPoolExecutor(max_workers=store.config.max_concurrency,
                                  thread_name_prefix="feedback")
        runtime.pool = pool
        try:
            if grading_repository is not None:
                store.allow_pitt_models()
            yield
        finally:
            if runtime.jobs:
                await asyncio.gather(*list(runtime.jobs), return_exceptions=True)
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

    def persist_feedback(assignment_id, assignment, payload, target, result):
        """Write one successful provider response to the grading repository."""
        if grading_repository is None or payload.submission is None:
            return None
        submission_id = db_submission(assignment_id, payload.submission)
        usage = result.get("usage") or {}
        response = ProviderFeedback(
            provider=result.get("provider", assignment.provider),
            model_name=result["model_name"],
            body=result["feedback_text"],
            prompt_version="v1",
            suggested_score=result.get("suggested_score"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
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
        return submission_id

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

    async def load_assignment(assignment_id):
        try:
            assignment = await asyncio.to_thread(assignment_loader, assignment_id)
            if assignment.id != assignment_id or not assignment.enabled:
                raise StoreError("assignment_forbidden", 403)
            if assignment.model not in runtime.store.config.allowed_models.get(assignment.engine, []):
                raise StoreError("assignment_config_unavailable", 503)
            return assignment
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
        targets = {}
        for target_id, target in assignment.targets.items():
            analysis = asdict(target.analysis)
            targets[target_id] = {
                "identifier": target.identifier, "kind": target.kind,
                "source_path": target.source_path, "max_score": target.max_score,
                "analysis": {key: analysis[key] for key in (
                    "mode", "behavior", "functional_result_source", "source_globs", "context_globs",
                    "evidence_sources", "max_source_files",
                )},
            }
            targets[target_id]["analysis"]["grading_items"] = [
                {"id": item.id, "points": item.points,
                 "label": (f"Criterion {index}" if re.fullmatch(
                     r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", item.id,
                 ) else item.id)}
                for index, item in enumerate(target.analysis.grading_items, 1)
            ]
        return JSONResponse({"assignment_id": assignment.id,
                             "revision": assignment_revision(assignment), "targets": targets},
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

    async def finish_feedback(future, assignment_id, rid, assignment, target, payload, timeout):
        store = runtime.store
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
            if payload.submission is not None and grading_repository is not None:
                await asyncio.to_thread(
                    persist_feedback, assignment_id, assignment, payload, target, result,
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
        rid = payload.request_id
        if (len(payload.code or "") + sum(len(source.content) for source in payload.files) > config.max_code_chars
                or len(payload.guidance or "") > config.max_guidance_chars):
            return error("input_too_large", 413, rid)
        try:
            assignment = await load_assignment(assignment_id)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
        try:
            existing = store.get_request(assignment_id, rid)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
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
        if existing is None and payload.submission is not None and grading_repository is not None:
            submission_id = await asyncio.to_thread(db_submission, assignment_id, payload.submission)
            action = await asyncio.to_thread(grading_repository.get_ai_execution_action, submission_id)
            if action == "deferred":
                action = await asyncio.to_thread(
                    grading_repository.defer_submission_target, submission_id, target.identifier,
                )
            if action != "execute":
                return JSONResponse(
                    {"request_id": rid, "status": action,
                     "reason": "awaiting_nightly_authorization" if action == "deferred" else "inactive_submission"},
                    headers={"Cache-Control": "no-store"},
                )
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
        canonical = json.dumps(content,
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # An accepted request retains its configuration snapshot while polling.
        # Current assignment/target authorization above still applies to replays.
        pitt_api_key = None
        if existing is None:
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
            if assignment.engine == "pitt":
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
        try:
            if existing is None and runtime.inflight >= config.max_concurrency:
                return error("busy", 429, rid)
            record, created = store.reserve(assignment_id, rid, digest, payload.target_id,
                                            assignment.model, engine=assignment.engine,
                                            provider=assignment.provider, run_id=payload.run_id)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
        except Exception:
            return error("backend_error", 500, rid)
        if not created:
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
            future, assignment_id, rid, assignment, target, payload, config.timeout_seconds,
        ))
        runtime.jobs.add(job)

        def forget_job(done):
            runtime.jobs.discard(done)
            if not done.cancelled() and done.exception() is not None:
                LOG.error("feedback_job_failed")

        job.add_done_callback(forget_job)
        if respond_async:
            return processing_response(rid)
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

    @router.post("/v1/submissions/{platform_submission_id}/ai-complete")
    async def ai_complete(platform_submission_id: str, request: Request):
        """Record the connector's aggregate outcome for this submission execution."""
        store = runtime.store
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            return error("unauthorized", 401)
        scheme, _, token = headers[0].partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256 or not token.isascii():
            return error("unauthorized", 401)
        try:
            assignment_id = store.authenticate(token)
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
            assignment = await asyncio.to_thread(
                grading_repository.get_assignment_config, UUID(assignment_id),
            )
            if str(assignment.id) != assignment_id or not assignment.is_active:
                return error("assignment_forbidden", 403)
            if payload.completion_reason == "not_needed" and any(
                target.rubric is not None
                and target.analysis_config.get("behavior") != "functional_test_feedback"
                for target in assignment.targets
            ):
                return error("invalid_request", 422)
            submission_id = await asyncio.to_thread(db_submission, assignment_id, payload.submission)
            if (payload.ai_status == "SUCCEEDED" and payload.completion_reason == "processed"
                    and assignment.grading_mode == "NIGHTLY"):
                action = await asyncio.to_thread(grading_repository.get_ai_execution_action, submission_id)
                if action == "deferred":
                    return error("ai_not_authorized", 409)
            await asyncio.to_thread(
                grading_repository.set_submission_ai_status, submission_id, payload.ai_status,
            )
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
        """Record one run's test results and opt-outs for the instructor dashboard.

        The hook sends results.json's rows once per run, before any AI call;
        app.run_results maps them to targets. Repeats of a run_id add nothing.
        """
        store = runtime.store
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            return error("unauthorized", 401)
        scheme, _, token = headers[0].partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256 or not token.isascii():
            return error("unauthorized", 401)
        try:
            assignment_id = store.authenticate(token)
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
            assignment = await asyncio.to_thread(
                grading_repository.get_assignment_config, UUID(assignment_id),
            )
            if str(assignment.id) != assignment_id or not assignment.is_active:
                return error("assignment_forbidden", 403)
            submission_id = await asyncio.to_thread(db_submission, assignment_id, payload.submission)
            outcomes = map_outcomes(
                assignment.targets, [row.model_dump() for row in payload.tests], payload.visibility,
            )
            recorded = await asyncio.to_thread(
                grading_repository.record_test_run, submission_id, payload.run_id, outcomes,
            )
        except StoreError as exc:
            return error(exc.code, exc.status_code)
        except Exception:
            LOG.error("test_results_failed", exc_info=True)
            return error("backend_error", 503)
        return JSONResponse(
            {"status": "recorded", "submission_id": platform_submission_id,
             "recorded": recorded, "targets": len(outcomes)},
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

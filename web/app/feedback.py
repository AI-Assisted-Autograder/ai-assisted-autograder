"""Assignment-token feedback using DB configuration and local runtime state."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from types import SimpleNamespace
import threading
import time

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
}


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    target_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    config_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    code: str | None = Field(default=None, min_length=1)
    files: list[CodeFile] = Field(default_factory=list, max_length=64)
    evidence: list[Evidence] = Field(default_factory=list, max_length=256)
    guidance: str | None = ""
    functional_test_feedback: list[str] = Field(default_factory=list)

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


def matches_path(path: str, patterns: tuple[str, ...]) -> bool:
    return any(FileResolver._matches(path, pattern) for pattern in patterns)


def assignment_revision(assignment) -> str:
    """Bind collected inputs and replay IDs to the grading configuration."""
    content = {"assignment": assignment.model_dump(mode="json"),
               "prompts": [PromptBuilder.version, PromptBuilder.extended_version,
                           PromptBuilder.per_test_version]}
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
    kinds = {"gradescope": "runtime", "junit": "runtime", "jacoco": "coverage", "text": "diagnostic"}
    for item in payload.evidence:
        source = sources.get(item.source)
        if source is None:
            raise ValueError("evidence source is outside the configured target")
        if (item.kind == "diagnostic" and item.status == "unknown"
                and item.test_id is None and item.scope == source.label):
            continue
        selector = "\n".join((item.test_id or "", item.scope))
        if (item.kind != kinds[source.format]
                or (source.include and not any(re.search(pattern, selector) for pattern in source.include))):
            raise ValueError("evidence is outside the configured target")
    return resolved


def error(code: str, status: int, request_id: str | None = None):
    body = {"error": {"code": code, "message": MESSAGES.get(code, MESSAGES["backend_error"])}}
    if request_id is not None:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status,
                        headers={"Cache-Control": "no-store"})


def default_provider_factory(config, assignment):
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
        return PittProvider(**options, provider=assignment.provider)
    if assignment.engine == "gemini_api":
        from ai_grading.ILLMProvider.GeminiProvider import GeminiProvider
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key or not key.strip():
            raise ProviderError("backend_auth_unavailable", MESSAGES["backend_auth_unavailable"])
        return GeminiProvider(**options, api_key=key)
    raise ValueError("Unsupported feedback engine")


def create_feedback_router(state_path: str | Path, *, assignment_loader,
                           provider_factory=None) -> APIRouter:
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
        runtime.providers = {}
        runtime.provider_lock = threading.Lock()
        runtime.inflight = 0
        runtime.tasks = set()
        runtime.jobs = set()
        pool = ThreadPoolExecutor(max_workers=store.config.max_concurrency,
                                  thread_name_prefix="feedback")
        runtime.pool = pool
        try:
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
                    "mode", "source_globs", "context_globs", "evidence_sources", "max_source_files",
                )},
            }
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

    def invoke(assignment_id, assignment, grading_request, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError
        with runtime.provider_lock:
            key = (assignment_id, assignment.engine, assignment.provider, assignment.model)
            if key not in runtime.providers:
                runtime.providers[key] = factory(runtime.store.config, assignment)
            provider = runtime.providers[key]
        if time.monotonic() >= deadline:
            raise TimeoutError
        return provider.generate_feedback(grading_request)

    def processing_response(request_id):
        return JSONResponse(
            {"request_id": request_id, "status": "processing"}, status_code=202,
            headers={"Cache-Control": "no-store", "Retry-After": "2",
                     "Preference-Applied": "respond-async"},
        )

    async def finish_feedback(future, assignment_id, rid, assignment, target, timeout):
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
            }
            if response.items:
                result["items"] = [asdict(item) for item in response.items]
            if target.max_score is not None:
                result["suggested_score"] = response.suggested_score
                result["max_score"] = target.max_score
            store.complete(assignment_id, rid, result)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except asyncio.CancelledError:
            # Only cancellation of the job itself interrupts generation; an HTTP
            # disconnect cannot cancel the independently tracked job.
            store.fail(assignment_id, rid, code="upstream_timeout")
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
        try:
            store.fail(assignment_id, rid, code=code)
        except Exception:
            return error("backend_error", 500, rid)
        LOG.warning("feedback_request_failed code=%s request_id=%s", code, rid)
        return error(code, 504 if code == "upstream_timeout" else 502, rid)

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
        target = assignment.targets.get(payload.target_id)
        if target is None:
            return error("target_forbidden", 403, rid)
        content = payload.model_dump(mode="json", exclude={"request_id"})
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
        try:
            existing = store.get_request(assignment_id, rid)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
        # An accepted request retains its configuration snapshot while polling.
        # Current assignment/target authorization above still applies to replays.
        if existing is None:
            if payload.config_revision is not None and payload.config_revision != assignment_revision(assignment):
                return error("config_changed", 409, rid)
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
        # No await between checking capacity, reserving quota and occupying it.
        # Replays must still work at full capacity and after quota exhaustion.
        try:
            if existing is None and runtime.inflight >= config.max_concurrency:
                return error("busy", 429, rid)
            record, created = store.reserve(assignment_id, rid, digest, payload.target_id,
                                            assignment.model, engine=assignment.engine,
                                            provider=assignment.provider)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
        except Exception:
            return error("backend_error", 500, rid)
        if not created:
            if record.status == "completed":
                result = {"engine": record.engine, "provider": record.provider,
                          "requested_model": record.model, **record.result}
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if record.status == "processing" and respond_async:
                return processing_response(rid)
            if record.status in ("processing", "interrupted"):
                return error(record.status, 409, rid)
            code = record.error_code or "model_error"
            return error(code, 504 if code == "upstream_timeout" else 502, rid)

        runtime.inflight += 1
        future = asyncio.get_running_loop().run_in_executor(
            runtime.pool, invoke, assignment_id, assignment, grading_request,
            time.monotonic() + config.timeout_seconds,
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
            future, assignment_id, rid, assignment, target, config.timeout_seconds,
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

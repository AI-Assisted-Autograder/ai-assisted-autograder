"""Assignment-token feedback using DB configuration and local runtime state."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_grading.models import (
    CodeFile, FeedbackRequest, GradingTarget, ResolvedSource, Rubric, TargetKind,
)
from ai_grading.PromptBuilder import PromptBuilder
from ai_grading.ILLMProvider.ILLMProvider import ProviderError
from .feedback_store import JsonStore, StoreError

LOG = logging.getLogger(__name__)
MESSAGES = {
    "unauthorized": "A valid assignment token is required.",
    "assignment_forbidden": "Assignment is unavailable.",
    "target_forbidden": "Target is not available for this assignment.",
    "assignment_config_unavailable": "Database assignment configuration is unavailable.",
    "invalid_request": "Request fields or JSON are invalid.",
    "input_too_large": "Input exceeds the backend limit.",
    "quota_exceeded": "Assignment call quota is exhausted.",
    "busy": "Backend concurrency limit reached. Try this request later.",
    "request_conflict": "This request_id was used with different content.",
    "processing": "This request is already processing; no new call was made.",
    "interrupted": "This request was interrupted; its reserved call remains consumed.",
    "model_error": "Vertex did not return usable feedback; the reserved call remains consumed.",
    "upstream_timeout": "Vertex timed out; the reserved call remains consumed.",
    "backend_auth_unavailable": "Backend Vertex authentication is unavailable.",
    "backend_error": "Backend could not safely finish this request.",
}


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    target_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    code: str = Field(min_length=1)
    guidance: str | None = ""
    functional_test_feedback: list[str] = Field(default_factory=list)


def error(code: str, status: int, request_id: str | None = None):
    body = {"error": {"code": code, "message": MESSAGES.get(code, MESSAGES["backend_error"])}}
    if request_id is not None:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status,
                        headers={"Cache-Control": "no-store"})


def default_provider_factory(config, assignment):
    from .vertex import VertexProvider
    return VertexProvider(config, assignment)


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
            raise RuntimeError("Local backend state is unavailable or locked.") from None
        runtime.store = store
        runtime.providers = {}
        runtime.provider_lock = threading.Lock()
        runtime.inflight = 0
        runtime.tasks = set()
        pool = ThreadPoolExecutor(max_workers=store.config.max_concurrency,
                                  thread_name_prefix="vertex")
        runtime.pool = pool
        try:
            yield
        finally:
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
            key = (assignment_id, assignment.model)
            if key not in runtime.providers:
                runtime.providers[key] = factory(runtime.store.config, assignment)
            provider = runtime.providers[key]
        if time.monotonic() >= deadline:
            raise TimeoutError
        return provider.generate_feedback(grading_request)

    async def handle_feedback(request: Request):
        store = runtime.store
        config = store.config
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            return error("unauthorized", 401)
        scheme, _, token = headers[0].partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256 or not token.isascii():
            return error("unauthorized", 401)
        try:
            assignment_id = store.authenticate(token)
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
        if (len(payload.code) > config.max_code_chars
                or len(payload.guidance or "") > config.max_guidance_chars):
            return error("input_too_large", 413, rid)
        try:
            assignment = await asyncio.to_thread(assignment_loader, assignment_id)
            if assignment.id != assignment_id or not assignment.enabled:
                return error("assignment_forbidden", 403, rid)
            if assignment.model not in config.allowed_models:
                return error("assignment_config_unavailable", 503, rid)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
        except Exception:
            return error("assignment_config_unavailable", 503, rid)
        target = assignment.targets.get(payload.target_id)
        if target is None:
            return error("target_forbidden", 403, rid)
        guidance = target.rubric.feedback_guidance
        if payload.guidance:
            guidance += "\nOptional client feedback guidance:\n" + payload.guidance
        grading_request = FeedbackRequest(
            target=GradingTarget(identifier=target.identifier, kind=TargetKind(target.kind),
                                 id=payload.target_id),
            rubric=Rubric(criteria_text=target.rubric.criteria_text, feedback_guidance=guidance,
                          id=target.rubric.id, version=target.rubric.version,
                          grading_target_id=payload.target_id),
            resolved=ResolvedSource(source_file=CodeFile(path=target.source_path, content=payload.code)),
            functional_test_feedback=tuple(payload.functional_test_feedback),
        )
        try:
            PromptBuilder().prepare(grading_request)
        except ValueError:
            return error("input_too_large", 413, rid)
        content = payload.model_dump(exclude={"request_id"})
        content["guidance"] = payload.guidance or ""
        if not payload.functional_test_feedback:
            content.pop("functional_test_feedback")
        canonical = json.dumps(content,
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # No await between checking capacity, reserving quota and occupying it.
        # Replays must still work at full capacity and after quota exhaustion.
        try:
            existing = store.get_request(assignment_id, rid)
            if existing is None and runtime.inflight >= config.max_concurrency:
                return error("busy", 429, rid)
            record, created = store.reserve(assignment_id, rid, digest, payload.target_id,
                                            assignment.model)
        except StoreError as exc:
            return error(exc.code, exc.status_code, rid)
        except Exception:
            return error("backend_error", 500, rid)
        if not created:
            if record.status == "completed":
                return JSONResponse(record.result, headers={"Cache-Control": "no-store"})
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
        try:
            response = await asyncio.wait_for(asyncio.shield(future), config.timeout_seconds)
            result = {
                "request_id": rid, "status": "completed",
                "feedback_text": response.feedback_text, "model_name": response.model_name,
                "usage": {"input_tokens": response.input_tokens,
                          "output_tokens": response.output_tokens},
            }
            store.complete(assignment_id, rid, result)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except asyncio.CancelledError:
            # A disconnect/shutdown cannot release quota or cancel the SDK call.
            store.fail(assignment_id, rid, code="upstream_timeout")
            raise
        except TimeoutError:
            code = "upstream_timeout"
        except ProviderError as exc:
            code = ("upstream_timeout" if exc.code == "timeout" else
                    "backend_auth_unavailable" if exc.code == "backend_auth_unavailable" else "model_error")
        except Exception:
            code = "model_error"
        try:
            store.fail(assignment_id, rid, code=code)
        except Exception:
            return error("backend_error", 500, rid)
        LOG.warning("feedback_request_failed code=%s", code)
        return error(code, 504 if code == "upstream_timeout" else 502, rid)

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
    parser.add_argument("--state", type=Path, default=Path(".local-vertex/backend/state.json"))
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

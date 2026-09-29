"""Local HTTP test harness. Run: uvicorn ai_grading.api:create_app --factory."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, JsonValue

from .AIGradingComponent import AIGradingComponent, RetryExhaustedError, RetryPolicy
from .models import FeedbackRequest, GradingInputError, GradingResult
from .PromptBuilder import PromptBuilder
from .ILLMProvider import FakeProvider
from .ILLMProvider.ILLMProvider import ProviderError


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    attempts: int = 1
    details: list[dict[str, JsonValue]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


def engine_from_env() -> AIGradingComponent:
    """Local configuration only; production credentials stay in the LMS container."""
    retry_policy = RetryPolicy(
        max_attempts=int(os.getenv("LLM_MAX_ATTEMPTS", "3")),
        initial_delay=float(os.getenv("LLM_RETRY_INITIAL_DELAY", "0.5")),
        multiplier=float(os.getenv("LLM_RETRY_MULTIPLIER", "2")),
        max_delay=float(os.getenv("LLM_RETRY_MAX_DELAY", "4")),
    )
    provider_name = os.getenv("LLM_PROVIDER", "fake").lower()
    if provider_name == "fake":
        provider = FakeProvider()
    else:
        if provider_name == "gemini":
            from .ILLMProvider.GeminiProvider import GeminiProvider as provider_class
        elif provider_name == "openai":
            from .ILLMProvider.OpenAIProvider import OpenAIProvider as provider_class
        elif provider_name == "claude":
            from .ILLMProvider.ClaudeProvider import ClaudeProvider as provider_class
        else:
            raise ValueError("LLM_PROVIDER must be fake, gemini, openai or claude")
        provider = provider_class(
            timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "4096")),
            builder=PromptBuilder(
                max_context_chars=int(os.getenv("LLM_MAX_CONTEXT_CHARS", "120000")),
                max_feedback_chars=int(os.getenv("LLM_MAX_FEEDBACK_CHARS", "4000")),
                max_target_feedback_chars=int(os.getenv("LLM_MAX_TARGET_FEEDBACK_CHARS", "16000")),
            ),
        )
    return AIGradingComponent([provider], retry_policy=retry_policy)


def create_app(engine: AIGradingComponent | None = None) -> FastAPI:
    """Local harness for FeedbackRequest; B/C integration lives outside this app."""
    owns_engine = engine is None
    grader = engine if engine is not None else engine_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if owns_engine:
            for provider in grader.providers:
                close = getattr(provider, "close", None)
                if close is not None:
                    close()

    app = FastAPI(title="Local AI Grading", lifespan=lifespan)

    def error(status: int, code: str, message: str, *, retryable=False, attempts=1, details=None):
        body = {"code": code, "message": message, "retryable": retryable, "attempts": attempts}
        if details is not None:
            body["details"] = details
        return JSONResponse(status_code=status, content={"error": body})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Don't echo the request's source code or arbitrary input values.
        details = [{"path": list(e["loc"]), "message": e["msg"], "type": e["type"]}
                   for e in exc.errors()]
        return error(422, "invalid_request", "Request validation failed.", details=details)

    @app.exception_handler(GradingInputError)
    async def invalid_context(request: Request, exc: GradingInputError):
        return error(422, "invalid_request", str(exc))

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError):
        underlying_code = exc.last_error_code if isinstance(exc, RetryExhaustedError) else exc.code
        status = (504 if underlying_code == "timeout" else
                  503 if isinstance(exc, RetryExhaustedError) or exc.retryable else 502)
        return error(status, exc.code, exc.message, retryable=exc.retryable, attempts=exc.attempts)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return error(500, "internal_error", "Grading failed unexpectedly.")

    @app.get("/health")
    def health():
        return {"status": "ok", "providers": [p.name for p in grader.providers]}

    @app.post(
        "/test/grade", response_model=GradingResult,
        responses={code: {"model": ErrorResponse} for code in (422, 500, 502, 503, 504)},
    )
    def grade(request: FeedbackRequest) -> GradingResult:
        """Synchronous grading. The caller handles persistence and task state."""
        return grader.grade(request)

    return app

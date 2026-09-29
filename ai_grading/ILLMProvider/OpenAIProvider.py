"""OpenAI Responses adapter; retries belong to AIGradingComponent."""

from math import isfinite
import os

from openai import (
    APIConnectionError, APIResponseValidationError, APIStatusError, APITimeoutError,
    OpenAI,
)

from ..models import FeedbackRequest, FeedbackResponse
from ..PromptBuilder import PromptBuilder
from .FeedbackParser import parse_feedback
from .ILLMProvider import ProviderError


class OpenAIProvider:
    name = "OPENAI"

    def __init__(
        self,
        model: str | None = None,
        *,
        client: OpenAI | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 4096,
        builder: PromptBuilder | None = None,
    ) -> None:
        model = model if model is not None else os.getenv("OPENAI_MODEL")
        api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        if not model or not model.strip():
            raise ValueError("model must be configured explicitly")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if client is None and (not api_key or not api_key.strip()):
            raise ValueError("OpenAI API key must be configured")
        self.model = model.strip()
        self._builder = builder if builder is not None else PromptBuilder()
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._owns_client = client is None
        self._client = client if client is not None else OpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=0,
        )

    def generate_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        prompt = self._builder.prepare(req)
        try:
            # Apply options to injected clients too, without changing their defaults.
            response = self._client.with_options(
                timeout=self._timeout_seconds, max_retries=0,
            ).responses.create(
                model=self.model,
                instructions=prompt.system_instruction,
                input=prompt.context,
                max_output_tokens=self._max_output_tokens,
                store=False,
                stream=False,
            )
            refusal = any(
                part.type == "refusal"
                for item in response.output if item.type == "message"
                for part in item.content
            )
            if refusal or getattr(response.incomplete_details, "reason", None) == "content_filter":
                raise ProviderError("provider_refusal", "OpenAI declined this request.")
            if response.error:
                raise ProviderError(
                    "provider_error", "OpenAI could not generate feedback.",
                    retryable=response.error.code in ("server_error", "rate_limit_exceeded"),
                )
            if response.status != "completed":
                raise ProviderError(
                    "incomplete_provider_response", "OpenAI did not finish generating feedback.",
                )
            usage = response.usage
            # output_tokens already includes reasoning_tokens; do not add it twice.
            return parse_feedback(
                response.output_text,
                request=req,
                provider=self.name,
                model_name=response.model or self.model,
                prompt_version=prompt.version,
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            )
        except APITimeoutError:
            raise ProviderError(
                "timeout", "OpenAI request timed out.", retryable=True,
            ) from None
        except APIConnectionError:
            raise ProviderError(
                "provider_unavailable", "Could not reach OpenAI.", retryable=True,
            ) from None
        except APIStatusError as exc:
            retryable = exc.status_code in (408, 409, 429) or 500 <= exc.status_code < 600
            raise ProviderError(
                "provider_error", f"OpenAI rejected the request (HTTP {exc.status_code}).",
                retryable=retryable,
            ) from None
        except APIResponseValidationError:
            raise ProviderError(
                "invalid_provider_response", "OpenAI returned an invalid response.",
            ) from None

    def estimated_cost(self, req: FeedbackRequest) -> float | None:
        """Unknown until the caller supplies a pricing policy."""
        return None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

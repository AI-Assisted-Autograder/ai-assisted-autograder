"""Claude Messages adapter using instructor credentials from the environment."""

from math import isfinite
import os

import anthropic

from ..models import FeedbackRequest, FeedbackResponse
from ..PromptBuilder import PromptBuilder
from .FeedbackParser import parse_feedback
from .ILLMProvider import ProviderError


class ClaudeProvider:
    name = "ANTHROPIC"

    def __init__(
        self,
        model: str | None = None,
        *,
        client: anthropic.Anthropic | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 4096,
        builder: PromptBuilder | None = None,
    ) -> None:
        model = model if model is not None else os.getenv("ANTHROPIC_MODEL")
        api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY")
        if not model or not model.strip():
            raise ValueError("model must be configured explicitly")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if client is None and (not api_key or not api_key.strip()):
            raise ValueError("Claude API key must be configured")
        self.model = model.strip()
        self._builder = builder if builder is not None else PromptBuilder()
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._owns_client = client is None
        self._client = client if client is not None else anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )

    def generate_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        """Return completed text feedback; retries belong to the grading component."""
        prompt = self._builder.prepare(req)
        try:
            # Override injected clients as well, without changing their defaults.
            response = self._client.with_options(
                max_retries=0, timeout=self._timeout_seconds,
            ).messages.create(
                model=self.model,
                max_tokens=self._max_output_tokens,
                system=prompt.system_instruction,
                messages=[{"role": "user", "content": prompt.context}],
            )
            if response.stop_reason == "refusal":
                raise ProviderError("provider_refusal", "Claude declined this request.")
            if response.stop_reason != "end_turn":
                raise ProviderError(
                    "incomplete_provider_response", "Claude did not finish generating feedback.",
                )
            output = "".join(block.text for block in response.content if block.type == "text")
            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "input_tokens", None)
            if input_tokens is not None:
                # Anthropic reports cache reads/writes separately from uncached input.
                input_tokens += sum(getattr(usage, field, None) or 0 for field in (
                    "cache_creation_input_tokens", "cache_read_input_tokens",
                ))
            return parse_feedback(
                output, request=req, provider=self.name,
                model_name=response.model or self.model,
                prompt_version=prompt.version,
                input_tokens=input_tokens, output_tokens=getattr(usage, "output_tokens", None),
            )
        except anthropic.APITimeoutError:
            raise ProviderError(
                "timeout", "Claude request timed out.", retryable=True,
            ) from None
        except anthropic.APIConnectionError:
            raise ProviderError(
                "provider_unavailable", "Could not reach Claude.", retryable=True,
            ) from None
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code in (408, 409, 429) or 500 <= exc.status_code < 600
            raise ProviderError(
                "provider_error", f"Claude rejected the request (HTTP {exc.status_code}).",
                retryable=retryable,
            ) from None
        except anthropic.APIResponseValidationError:
            raise ProviderError(
                "invalid_provider_response", "Claude returned invalid grading feedback.",
            ) from None
        except anthropic.APIError:
            raise ProviderError("provider_error", "Claude request failed.") from None

    def estimated_cost(self, req: FeedbackRequest) -> float | None:
        """Unknown until the caller supplies a pricing policy."""
        return None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

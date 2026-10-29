"""Pitt AI Connect adapter using the Portkey Chat Completions endpoint."""

from math import isfinite
import os

import httpx

from ..models import FeedbackRequest, FeedbackResponse
from ..PromptBuilder import PromptBuilder
from .FeedbackParser import parse_feedback
from .ILLMProvider import ProviderError


class PittProvider:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        provider: str = "PITT",
        timeout_seconds: float = 240.0,
        max_output_tokens: int = 16_384,
        builder: PromptBuilder | None = None,
    ) -> None:
        if not model or not model.strip():
            raise ValueError("model must be configured explicitly")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        api_key = api_key if api_key is not None else os.getenv("PITT_API_KEY")
        if not api_key or not api_key.strip():
            raise ProviderError(
                "backend_auth_unavailable", "Backend Pitt authentication is unavailable.",
            )
        self.name = provider
        self.model = model.strip()
        self._builder = builder if builder is not None else PromptBuilder()
        self._max_output_tokens = max_output_tokens
        self._client = httpx.Client(
            timeout=timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            headers={
                "x-portkey-api-key": api_key.strip(),
                "x-portkey-cache-force-refresh": "true",
            },
        )

    def generate_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        prompt = self._builder.prepare(req)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt.system_instruction},
                {"role": "user", "content": prompt.context},
            ],
            "max_tokens": self._max_output_tokens,
            "stream": False,
        }
        if req.analysis.temperature is not None:
            payload["temperature"] = req.analysis.temperature
        try:
            response = self._client.post(
                "https://api.portkey.ai/v1/chat/completions",
                json=payload,
            )
        except httpx.TimeoutException:
            raise ProviderError(
                "timeout", "Pitt request timed out.", retryable=True,
            ) from None
        except httpx.TransportError:
            raise ProviderError(
                "provider_unavailable", "Could not reach Pitt AI Connect.", retryable=True,
            ) from None
        if response.status_code in (401, 403):
            raise ProviderError(
                "backend_auth_unavailable", "Backend Pitt authentication is unavailable.",
            )
        if not response.is_success:
            raise ProviderError(
                "provider_error",
                f"Pitt rejected the request (HTTP {response.status_code}).",
                retryable=response.status_code in (408, 429) or response.status_code >= 500,
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("response must be an object")
            choices = data.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("response must contain one choice")
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("response must contain a message")
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise ProviderError("provider_refusal", "Pitt declined this request.")
            if choice.get("finish_reason") != "stop" or message.get("tool_calls"):
                raise ProviderError(
                    "incomplete_provider_response", "Pitt did not finish generating feedback.",
                )
            output = message.get("content")
            if not isinstance(output, str):
                raise ValueError("feedback must be text")
            usage = data.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("usage must be an object")
            return parse_feedback(
                output,
                request=req,
                provider=self.name,
                model_name=data.get("model") or self.model,
                prompt_version=prompt.version,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        except ValueError:
            raise ProviderError(
                "invalid_provider_response", "Pitt returned an invalid response.",
            ) from None

    def estimated_cost(self, req: FeedbackRequest) -> float | None:
        """Unknown until the caller supplies a pricing policy."""
        return None

    def close(self) -> None:
        self._client.close()

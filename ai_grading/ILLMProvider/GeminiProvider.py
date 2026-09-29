"""Gemini adapter for the container, with instructor credentials and local config."""

import json
from math import isfinite
import os
from pathlib import Path
import re

import httpx
from google import genai
from google.genai import errors, types

from ..models import FeedbackRequest, FeedbackResponse
from ..PromptBuilder import PromptBuilder
from .ILLMProvider import ProviderError


LOCAL_CONFIG_PATH = Path(__file__).with_name(".gemini.json")


class GeminiProvider:
    name = "GOOGLE"

    def __init__(
        self,
        model: str | None = None,
        *,
        client: genai.Client | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 4096,
        builder: PromptBuilder | None = None,
    ) -> None:
        model = model if model is not None else os.getenv("GEMINI_MODEL")
        api_key = api_key if api_key is not None else (
            os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        )
        if (model is None or (client is None and api_key is None)) and LOCAL_CONFIG_PATH.is_file():
            config = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
            if model is None:
                model = config.get("model")
            if api_key is None:
                api_key = config.get("api_key")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be configured explicitly")
        if (isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be a positive finite number")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if client is None and (not isinstance(api_key, str) or not api_key.strip()):
            raise ValueError("Gemini API key must be configured")
        self.model = model.strip()
        self._builder = builder if builder is not None else PromptBuilder()
        self._max_output_tokens = max_output_tokens
        self._http_options = types.HttpOptions(
            timeout=max(1, int(timeout_seconds * 1000)),
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        self._owns_client = client is None
        self._client = client if client is not None else genai.Client(
            api_key=api_key,
            http_options=self._http_options,
        )

    def generate_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        """Return validated feedback; output token usage includes Gemini thoughts."""
        prompt = self._builder.prepare(req)
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt.context,
                config=types.GenerateContentConfig(
                    system_instruction=prompt.system_instruction,
                    http_options=self._http_options,
                    response_mime_type="text/plain",
                    max_output_tokens=self._max_output_tokens,
                    candidate_count=1,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True,
                    ),
                ),
            )
        except httpx.TimeoutException:
            raise ProviderError(
                "timeout", "Gemini request timed out.", retryable=True,
            ) from None
        except httpx.TransportError:
            raise ProviderError(
                "provider_unavailable", "Could not reach Gemini.", retryable=True,
            ) from None
        except errors.APIError as exc:
            retryable = exc.code in (408, 429) or 500 <= exc.code < 600
            raise ProviderError(
                "provider_error", f"Gemini rejected the request (HTTP {exc.code}).",
                retryable=retryable,
            ) from None
        except (ValueError, TypeError):
            raise ProviderError(
                "invalid_provider_response", "Gemini returned an invalid response.",
            ) from None

        prompt_feedback = response.prompt_feedback
        if prompt_feedback and prompt_feedback.block_reason not in (
            None, types.BlockedReason.BLOCKED_REASON_UNSPECIFIED,
        ):
            raise ProviderError("provider_refusal", "Gemini declined this request.")
        if not response.candidates or len(response.candidates) != 1:
            raise ProviderError(
                "invalid_provider_response", "Gemini returned no unique feedback candidate.",
            )
        candidate = response.candidates[0]
        if candidate.finish_reason in (
            types.FinishReason.SAFETY, types.FinishReason.RECITATION,
            types.FinishReason.BLOCKLIST, types.FinishReason.PROHIBITED_CONTENT,
            types.FinishReason.SPII,
        ):
            raise ProviderError("provider_refusal", "Gemini declined this request.")
        if candidate.finish_reason != types.FinishReason.STOP:
            raise ProviderError(
                "incomplete_provider_response", "Gemini did not finish generating feedback.",
            )
        # Exclude thinking parts; neither tool calls nor thoughts are feedback.
        parts = candidate.content.parts if candidate.content else []
        output = "".join(part.text or "" for part in parts or [] if not part.thought)
        try:
            # AD-6: the model supplies text and a score; code owns serialization.
            score_line, separator, feedback_text = output.partition("\n")
            score_match = re.fullmatch(r"SCORE: (NONE|[0-9]+(?:\.[0-9]+)?)\r?", score_line)
            if not separator or score_match is None:
                raise ValueError("missing or invalid score line")
            value = score_match.group(1)
            suggested_score = None if value == "NONE" else float(value)
            if (suggested_score is not None
                    and req.target.max_score is not None
                    and suggested_score > req.target.max_score):
                raise ValueError("score exceeds maximum")
            usage = response.usage_metadata
            generated_counts = (
                [usage.candidates_token_count, usage.thoughts_token_count]
                if usage else []
            )
            known_counts = [count for count in generated_counts if count is not None]
            return FeedbackResponse(
                rubric_id=req.rubric.id,
                provider=self.name,
                model_name=response.model_version or self.model,
                prompt_version=prompt.version,
                feedback_text=feedback_text,
                suggested_score=suggested_score,
                input_tokens=usage.prompt_token_count if usage else None,
                output_tokens=sum(known_counts) if known_counts else None,
            )
        except ValueError:
            raise ProviderError(
                "invalid_provider_response", "Gemini returned invalid grading feedback.",
            ) from None

    def estimated_cost(self, req: FeedbackRequest) -> float | None:
        """Unknown until the caller supplies a pricing policy."""
        return None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

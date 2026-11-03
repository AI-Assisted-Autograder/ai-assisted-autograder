"""Gemini adapter for an injected Vertex client or a Gemini API-key client."""

from math import isfinite
import os

import httpx
from google import genai
from google.genai import errors, types

from ..models import FeedbackRequest, FeedbackResponse
from ..PromptBuilder import PromptBuilder
from .FeedbackParser import parse_feedback
from .ILLMProvider import ProviderError


class GeminiProvider:
    name = "GOOGLE"

    def __init__(
        self,
        model: str | None = None,
        *,
        client: genai.Client | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 240.0,
        max_output_tokens: int = 16_384,
        builder: PromptBuilder | None = None,
    ) -> None:
        # An injected backend client must never consult legacy caller credentials.
        model = model if model is not None else (os.getenv("GEMINI_MODEL") if client is None else None)
        api_key = api_key if api_key is not None else (
            (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
            if client is None else None
        )
        if not model or not model.strip():
            raise ValueError("model must be configured explicitly")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if client is None and (not api_key or not api_key.strip()):
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
            vertexai=False,
            api_key=api_key,
            http_options=self._http_options,
        )

    def generate_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        """Return validated feedback; output token usage includes Gemini thoughts."""
        prompt = self._builder.prepare(req)
        response_json_schema = None
        if req.analysis.behavior == "functional_test_feedback":
            expected = [item.id for item in req.evidence
                        if item.source == req.analysis.functional_result_source
                        and item.kind == "runtime" and item.status == "fail"]
            response_json_schema = {
                "type": "object",
                "properties": {
                    "failure_feedback": {
                        "type": "array", "minItems": len(expected), "maxItems": len(expected),
                        "items": {
                            "type": "object",
                            "properties": {
                                "evidence_id": {"type": "string", "enum": expected},
                                "feedback": {"type": "string"},
                                "evidence": {"type": "string"},
                            },
                            "required": ["evidence_id", "feedback", "evidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["failure_feedback"], "additionalProperties": False,
            }
        elif (req.analysis.behavior == "ai_grading" or req.analysis.points_per_test is not None
                or (req.analysis.mode == "test_quality" and req.target.max_score is None)):
            expected_tests = ([item.id for item in req.analysis.grading_items]
                              if req.analysis.behavior == "ai_grading"
                              else list(req.analysis.expected_tests))
            # Scores belong to the backend, not the model's response contract.
            response_json_schema = {
                "type": "object",
                "properties": {
                    "feedback_text": {"type": "string"},
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "test_id": {"type": "string"},
                                "verdict": {"type": "string", "enum": [
                                    "pass", "fail", "insufficient_evidence",
                                ]},
                                "feedback": {"type": "string"},
                                "evidence": {"type": "string"},
                            },
                            "required": ["test_id", "verdict", "feedback", "evidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["feedback_text", "items"],
                "additionalProperties": False,
            }
            if req.analysis.behavior == "ai_grading":
                del response_json_schema["properties"]["feedback_text"]
                response_json_schema["required"] = ["items"]
            if expected_tests:
                items_schema = response_json_schema["properties"]["items"]
                items_schema["minItems"] = items_schema["maxItems"] = len(expected_tests)
                items_schema["items"]["properties"]["test_id"]["enum"] = expected_tests
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt.context,
                config=types.GenerateContentConfig(
                    system_instruction=prompt.system_instruction,
                    http_options=self._http_options,
                    response_mime_type=(
                        "text/plain" if req.analysis.behavior is None and req.analysis.mode == "code_feedback"
                        else "application/json"
                    ),
                    response_json_schema=response_json_schema,
                    max_output_tokens=self._max_output_tokens,
                    temperature=(
                        None if self.model.startswith("gemini-3.8-")
                        else req.analysis.temperature
                    ),
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
            if exc.code in (408, 504):
                raise ProviderError(
                    "timeout", f"Gemini request timed out (HTTP {exc.code}).", retryable=True,
                ) from None
            retryable = exc.code in (408, 429) or 500 <= exc.code < 600
            raise ProviderError(
                "provider_error", f"Gemini rejected the request (HTTP {exc.code}).",
                retryable=retryable,
            ) from None
        except ValueError:
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
                "incomplete_provider_response",
                f"Gemini did not finish generating feedback ({candidate.finish_reason}).",
            )
        # Exclude thinking parts; neither tool calls nor thoughts are feedback.
        parts = candidate.content.parts if candidate.content else []
        output = "".join(part.text or "" for part in parts or [] if not part.thought)
        usage = response.usage_metadata
        generated_counts = (
            [usage.candidates_token_count, usage.thoughts_token_count]
            if usage else []
        )
        known_counts = [count for count in generated_counts if count is not None]
        return parse_feedback(
            output,
            request=req,
            provider=self.name,
            model_name=response.model_version or self.model,
            prompt_version=prompt.version,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=sum(known_counts) if known_counts else None,
        )

    def estimated_cost(self, req: FeedbackRequest) -> float | None:
        """Unknown until the caller supplies a pricing policy."""
        return None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

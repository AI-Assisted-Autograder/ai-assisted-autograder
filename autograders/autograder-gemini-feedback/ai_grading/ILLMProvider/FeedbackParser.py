"""Parse the shared plain-text grading format into the provider contract."""

import re

from ..models import FeedbackRequest, FeedbackResponse
from .ILLMProvider import ProviderError


def parse_feedback(
    output: str,
    *,
    request: FeedbackRequest,
    provider: str,
    model_name: str,
    prompt_version: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> FeedbackResponse:
    """Keep feedback as text; code owns score validation and serialization."""
    try:
        score_line, separator, feedback_text = output.partition("\n")
        match = re.fullmatch(r"SCORE: (NONE|[0-9]+(?:\.[0-9]+)?)\r?", score_line)
        if not separator or match is None:
            raise ValueError("missing or invalid score line")
        value = match.group(1)
        suggested_score = None if value == "NONE" else float(value)
        if (suggested_score is not None and request.target.max_score is not None
                and suggested_score > request.target.max_score):
            raise ValueError("score exceeds maximum")
        return FeedbackResponse(
            rubric_id=request.rubric.id,
            provider=provider,
            model_name=model_name,
            prompt_version=prompt_version,
            feedback_text=feedback_text,
            suggested_score=suggested_score,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except ValueError:
        raise ProviderError(
            "invalid_provider_response", f"{provider} returned invalid grading feedback.",
        ) from None

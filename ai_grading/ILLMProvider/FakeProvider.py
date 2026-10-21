"""Deterministic canned feedback, without network calls or model keys."""

import json

from ..models import FeedbackRequest, FeedbackResponse
from .FeedbackParser import parse_feedback


class FakeProvider:
    name = "FAKE"
    model = "canned-v1"

    def __init__(
        self,
        feedback_text: str = "[FAKE] Placeholder feedback; no code evaluation was performed.",
        suggested_score: float | None = None,
    ) -> None:
        self._feedback_text = feedback_text
        self._suggested_score = suggested_score

    def generate_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        if req.analysis.mode != "code_feedback":
            identifiers = req.analysis.expected_tests or (req.target.identifier,)
            return parse_feedback(
                json.dumps({
                    "feedback_text": self._feedback_text,
                    "suggested_score": (
                        self._suggested_score if req.target.max_score is not None
                        and req.analysis.points_per_test is None else None
                    ),
                    "items": [{
                        "test_id": identifier, "verdict": "insufficient_evidence",
                        "feedback": self._feedback_text,
                        "evidence": "[FAKE] No source or execution evidence was evaluated.",
                    } for identifier in identifiers],
                }),
                request=req, provider=self.name, model_name=self.model,
                prompt_version="fake-v2", input_tokens=0, output_tokens=0,
            )
        return FeedbackResponse(
            rubric_id=req.rubric.id,
            provider=self.name,
            model_name=self.model,
            prompt_version="fake-v1",
            feedback_text=self._feedback_text,
            suggested_score=self._suggested_score,
            input_tokens=0,
            output_tokens=0,
        )

    def estimated_cost(self, req: FeedbackRequest) -> float:
        return 0.0

"""Deterministic canned feedback, without network calls or model keys."""

from ..models import FeedbackRequest, FeedbackResponse


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

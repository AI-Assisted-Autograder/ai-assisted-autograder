"""Deterministic canned feedback, without network calls or model keys."""

from ..models import FeedbackRequest, FeedbackResponse
from ..prompt_builder import PromptBuilder


class FakeProvider:
    name = "FAKE"
    model = "canned-v1"

    def __init__(
        self,
        feedback_text: str = "[FAKE] Placeholder feedback; no code evaluation was performed.",
        suggested_score: float | None = None,
        *,
        builder: PromptBuilder | None = None,
    ) -> None:
        self._feedback_text = feedback_text
        self._suggested_score = suggested_score
        self._builder = builder if builder is not None else PromptBuilder()
        self.requests: list[FeedbackRequest] = []
        self.prompts: list[str] = []

    def generate_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        prompt = self._builder.prepare(req)
        response = FeedbackResponse(
            rubric_id=req.rubric.id,
            provider=self.name,
            model_name=self.model,
            prompt_version=prompt.version,
            feedback_text=self._feedback_text,
            suggested_score=self._suggested_score,
            input_tokens=0,
            output_tokens=0,
        )
        self.requests.append(req)
        self.prompts.append(prompt.text)
        return response

    def estimated_cost(self, req: FeedbackRequest) -> float:
        return 0.0

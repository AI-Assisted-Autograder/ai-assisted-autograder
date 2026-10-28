from typing import Protocol

from ..models import FeedbackRequest, FeedbackResponse


class ProviderError(Exception):
    """Public-safe failure; retryable classifies the cause within this run."""

    def __init__(
        self, code: str, message: str, *, retryable: bool = False, attempts: int = 1,
    ):
        if type(attempts) is not int or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempts = attempts


class ILLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def generate_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        """Return feedback or raise an exception; a failure is not a zero score."""
        ...

    def estimated_cost(self, req: FeedbackRequest) -> float | None:
        """None means pricing is not configured; it must not imply zero cost."""
        ...

"""Request -> grading result. No HTTP, database, scheduler, or file IO."""

from dataclasses import dataclass
from math import isfinite
import time
from typing import Callable, Sequence

from .models import Comparison, FeedbackRequest, FeedbackResponse, GradingResult
from .providers.base import ILLMProvider, ProviderError


@dataclass(frozen=True)
class RetryPolicy:

    max_attempts: int = 3
    initial_delay: float = 0.5
    multiplier: float = 2.0
    max_delay: float = 4.0

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        for name in ("initial_delay", "multiplier", "max_delay"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not isfinite(value) or value < 0):
                raise ValueError(f"{name} must be a finite nonnegative number")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")


class RetryExhaustedError(ProviderError):
    """Terminal for this grading run, even if the last error was transient."""

    def __init__(self, last_error: ProviderError, attempts: int):
        super().__init__(
            "retry_exhausted",
            f"Provider failed after {attempts} attempt(s): {last_error.message}",
            retryable=False,
            attempts=attempts,
        )
        self.last_error_code = last_error.code


class GradingEngine:
    def __init__(
        self,
        providers: Sequence[ILLMProvider],
        *,
        selector: Callable[[tuple[FeedbackResponse, ...]], int] | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not providers:
            raise ValueError("configure at least one provider")
        self.providers = tuple(providers)
        self.selector = selector
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._sleep = sleep

    def _generate(self, provider: ILLMProvider, request: FeedbackRequest) -> FeedbackResponse:
        delay = min(self.retry_policy.initial_delay, self.retry_policy.max_delay)
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                result = provider.generate_feedback(request)
                if result.rubric_id != request.rubric.id:
                    raise ProviderError("invalid_response", "Provider returned a mismatched rubric identifier.")
                if (result.suggested_score is not None and request.target.max_score is not None
                        and result.suggested_score > request.target.max_score):
                    raise ProviderError("invalid_response", "Provider score exceeds the target maximum.")
                return result
            except ProviderError as error:
                if not error.retryable:
                    error.attempts = attempt
                    raise
                if attempt == self.retry_policy.max_attempts:
                    raise RetryExhaustedError(error, attempt) from error
                self._sleep(delay)
                delay = min(delay * self.retry_policy.multiplier, self.retry_policy.max_delay)
        raise AssertionError("retry policy allowed no attempts")

    def grade(self, request: FeedbackRequest) -> GradingResult:
        results = []
        # Retry only the failing provider, keeping prior successful responses in memory.
        # Persistence and submission completion belong to the B/C integration layer.
        for provider in self.providers:
            result = self._generate(provider, request)
            results.append(result)

        responses = tuple(results)
        index = self.selector(responses) if self.selector else 0
        if type(index) is not int or not 0 <= index < len(responses):
            raise RuntimeError("selector must return a valid response index")
        scores = [r.suggested_score for r in responses if r.suggested_score is not None]
        comparison = Comparison(
            selection_reason=("Selected by the configured selector." if self.selector else
                              "First configured provider; no quality ranking is performed."),
            score_spread=max(scores) - min(scores) if len(scores) >= 2 else None,
            confidence=None,
        )
        return GradingResult(
            responses=responses, selected=responses[index], comparison=comparison,
        )

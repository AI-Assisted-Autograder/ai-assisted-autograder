"""The grading module's contract, independent of database tables and task state."""

from dataclasses import field
from enum import Enum
from typing import Annotated

from pydantic import Field
from pydantic.dataclasses import dataclass


Score = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1)]
Version = Annotated[int, Field(strict=True, ge=1)]
Count = Annotated[int, Field(strict=True, ge=0)]
CONTRACT_CONFIG = {"extra": "forbid", "allow_inf_nan": False}
MAX_FEEDBACK_CHARS = 16_000
FeedbackText = Annotated[str, Field(min_length=1, max_length=MAX_FEEDBACK_CHARS)]


class GradingInputError(ValueError):
    """A request or source-file configuration that cannot be graded."""


class TargetKind(str, Enum):
    METHOD = "METHOD"
    CLASS = "CLASS"
    FILE = "FILE"


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class GradingTarget:
    identifier: Identifier
    kind: TargetKind
    max_score: Score | None = None
    id: Identifier | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("target identifier must not be blank")
        if self.max_score is not None and self.max_score < 0:
            raise ValueError("max_score must be nonnegative")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class Rubric:
    criteria_text: Identifier
    feedback_guidance: str = ""
    version: Version | None = None
    id: Identifier | None = field(default=None, kw_only=True)
    grading_target_id: Identifier | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not self.criteria_text.strip():
            raise ValueError("rubric criteria_text must not be blank")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class CodeFile:
    path: Identifier
    content: str

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("file path must not be blank")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class ResolvedSource:
    """Exactly one source body, plus names of files the model cannot inspect."""

    source_file: CodeFile
    other_file_paths: tuple[Identifier, ...] = ()

    def __post_init__(self) -> None:
        paths = self.other_file_paths
        if any(not path.strip() for path in paths):
            raise ValueError("other file paths must not be blank")
        if len(paths) != len(set(paths)):
            raise ValueError("other file paths must be distinct")
        if self.source_file.path in paths:
            raise ValueError("other file paths must exclude the resolved source file")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class FeedbackRequest:
    """One resolved target with diagnostic strings ordered by the caller."""

    target: GradingTarget
    rubric: Rubric
    resolved: ResolvedSource
    functional_test_feedback: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.rubric.grading_target_id is not None and self.target.id is not None
                and self.rubric.grading_target_id != self.target.id):
            raise ValueError("rubric must belong to the request's grading target")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class FeedbackResponse:
    """One provider's result, before comparison, selection, and persistence.

    confidence is absent until OQ-2 is resolved. IDs/timestamps for stored
    feedback and is_selected belong to later orchestration/persistence.
    """

    provider: Identifier
    model_name: Identifier
    prompt_version: Identifier
    feedback_text: FeedbackText
    suggested_score: Score | None = None
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    rubric_id: Identifier | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not self.feedback_text.strip():
            raise ValueError("feedback_text must not be blank")
        if self.suggested_score is not None and self.suggested_score < 0:
            raise ValueError("suggested_score must be nonnegative")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class Comparison:
    selection_reason: str
    score_spread: Score | None = None
    confidence: Score | None = None


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class GradingResult:
    responses: tuple[FeedbackResponse, ...]
    selected: FeedbackResponse
    comparison: Comparison

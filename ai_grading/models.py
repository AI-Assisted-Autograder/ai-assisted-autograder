"""The grading module's contract, independent of database tables and task state."""

from dataclasses import field
from decimal import Decimal
from enum import Enum
from math import isclose
from typing import Annotated, Literal

from pydantic import Field
from pydantic.dataclasses import dataclass


Score = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Identifier = Annotated[str, Field(pattern=r"\S")]
Version = Annotated[int, Field(strict=True, ge=1)]
Count = Annotated[int, Field(strict=True, ge=0)]
CONTRACT_CONFIG = {"extra": "forbid", "allow_inf_nan": False}
MAX_FEEDBACK_CHARS = 16_000
FeedbackText = Annotated[str, Field(pattern=r"\S", max_length=MAX_FEEDBACK_CHARS)]


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


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class Rubric:
    criteria_text: Identifier
    feedback_guidance: str = ""
    version: Version | None = None
    id: Identifier | None = field(default=None, kw_only=True)
    grading_target_id: Identifier | None = field(default=None, kw_only=True)


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class CodeFile:
    path: Identifier
    content: str


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class EvidenceSource:
    """Instructor-selected runtime artifact, relative to the autograder root."""

    format: Literal["gradescope", "junit", "jacoco", "text"]
    path: Identifier
    label: Identifier
    include: tuple[Identifier, ...] = ()


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class GradingItem:
    """One instructor-owned requirement and its configured maximum points."""

    id: Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
    requirement: FeedbackText
    points: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]

    def __post_init__(self) -> None:
        if Decimal(str(self.points)).normalize().as_tuple().exponent < -2:
            raise ValueError("grading item points support at most two decimal places")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class AnalysisConfig:
    """Backend-owned analysis instructions, artifact selection and generation options."""

    mode: Literal["code_feedback", "testplan", "test_quality"] = "code_feedback"
    source_globs: tuple[Identifier, ...] = ()
    context_globs: tuple[Identifier, ...] = ()
    test_plan: str = ""
    expected_tests: tuple[Identifier, ...] = ()
    evidence_sources: tuple[EvidenceSource, ...] = ()
    points_per_test: Score | None = None
    max_source_files: Annotated[int, Field(strict=True, ge=1, le=64)] | None = None
    temperature: Annotated[float, Field(strict=True, ge=0, le=2)] | None = None
    behavior: Literal["ai_grading", "functional_test_feedback"] | None = None
    grading_items: tuple[GradingItem, ...] = ()
    functional_result_source: Identifier | None = None
    attempt_id: Annotated[str, Field(
        min_length=36, max_length=36,
        pattern=r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$",
    )] | None = None
    shared_grading_rules: Annotated[str, Field(max_length=16_000)] = ""

    def __post_init__(self) -> None:
        if len(self.expected_tests) != len(set(self.expected_tests)):
            raise ValueError("expected test identifiers must be distinct")
        labels = [source.label for source in self.evidence_sources]
        if len(labels) != len(set(labels)):
            raise ValueError("evidence source labels must be distinct")
        if self.points_per_test is not None and (self.mode != "testplan" or not self.expected_tests):
            raise ValueError("per-test points require testplan mode and expected tests")
        ids = [item.id for item in self.grading_items]
        if len(ids) != len(set(ids)):
            raise ValueError("grading item identifiers must be distinct")
        if self.grading_items and self.behavior != "ai_grading":
            raise ValueError("grading items require AI grading behavior")
        if self.shared_grading_rules and self.behavior != "ai_grading":
            raise ValueError("shared grading rules require AI grading behavior")
        if self.behavior is not None and (self.points_per_test is not None or self.expected_tests):
            raise ValueError("explicit behaviors cannot use legacy per-test scoring configuration")
        if self.functional_result_source is not None and self.behavior != "functional_test_feedback":
            raise ValueError("a functional result source requires functional-test feedback behavior")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class Evidence:
    """An identified observation from a configured execution artifact."""

    id: Identifier
    kind: Literal["runtime", "coverage", "diagnostic"]
    source: Identifier
    summary: Identifier
    test_id: Identifier | None = None
    scope: str = ""
    status: Literal["pass", "fail", "error", "skipped", "unknown"] = "unknown"


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class ResolvedSource:
    """A primary source, supplied context files, and names of unread files."""

    source_file: CodeFile
    other_file_paths: tuple[Identifier, ...] = ()
    context_files: tuple[CodeFile, ...] = ()

    def __post_init__(self) -> None:
        paths = self.other_file_paths
        if len(paths) != len(set(paths)):
            raise ValueError("other file paths must be distinct")
        supplied = [self.source_file.path, *(source.path for source in self.context_files)]
        if len(supplied) != len(set(supplied)):
            raise ValueError("supplied source file paths must be distinct")
        if set(supplied).intersection(paths):
            raise ValueError("other file paths must exclude supplied source files")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class FeedbackRequest:
    """One target with backend instructions, source context and execution evidence."""

    target: GradingTarget
    rubric: Rubric
    resolved: ResolvedSource
    functional_test_feedback: tuple[str, ...] = ()
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        if (self.rubric.grading_target_id is not None and self.target.id is not None
                and self.rubric.grading_target_id != self.target.id):
            raise ValueError("rubric must belong to the request's grading target")
        evidence_ids = [item.id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence identifiers must be distinct")
        if self.analysis.points_per_test is not None:
            total = self.analysis.points_per_test * len(self.analysis.expected_tests)
            if self.target.max_score is None or not isclose(self.target.max_score, total):
                raise ValueError("target maximum must equal the configured per-test total")
        if self.analysis.behavior is not None and self.functional_test_feedback:
            raise ValueError("explicit behaviors require scoped execution evidence")
        if self.analysis.behavior == "ai_grading":
            if not self.analysis.grading_items:
                raise ValueError("AI grading requires configured grading items")
            total = sum((Decimal(str(item.points)) for item in self.analysis.grading_items), Decimal(0))
            if self.target.max_score is None or Decimal(str(self.target.max_score)) != total:
                raise ValueError("target maximum must equal the configured grading item total")
        if self.analysis.behavior == "functional_test_feedback":
            if self.target.max_score is not None:
                raise ValueError("functional-test feedback cannot assign points")
            source = next((source for source in self.analysis.evidence_sources
                           if source.label == self.analysis.functional_result_source), None)
            if source is None or source.format != "gradescope":
                raise ValueError("functional-test feedback requires a Gradescope result source")
            if not any(item.kind == "runtime" and item.status == "fail"
                       and item.source == source.label for item in self.evidence):
                raise ValueError("functional-test feedback requires failed functional rows")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class FeedbackItem:
    """One analysis verdict, with separate student feedback and instructor evidence."""

    test_id: Identifier
    verdict: Literal["pass", "fail", "insufficient_evidence"]
    feedback: Annotated[str, Field(max_length=MAX_FEEDBACK_CHARS)]
    evidence: FeedbackText
    suggested_score: Score | None = None
    max_score: Score | None = None

    def __post_init__(self) -> None:
        if self.verdict != "pass" and not self.feedback.strip():
            raise ValueError("non-passing verdicts require student feedback")
        if self.suggested_score is not None:
            if self.max_score is None or self.suggested_score > self.max_score:
                raise ValueError("item score requires and must not exceed its maximum")


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class FailureFeedbackItem:
    """An explanation for one existing failed row, without a new grade or verdict."""

    evidence_id: Identifier
    feedback: FeedbackText
    evidence: FeedbackText


@dataclass(frozen=True, config=CONTRACT_CONFIG)
class TokenUsage:
    """Provider usage; cache reads/writes and reasoning are subsets of their totals."""

    input_tokens: Count | None = None
    output_tokens: Count | None = None
    cached_input_tokens: Count | None = None
    reasoning_tokens: Count | None = None
    cache_write_tokens: Count | None = None


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
    cached_input_tokens: Count | None = field(default=None, kw_only=True)
    reasoning_tokens: Count | None = field(default=None, kw_only=True)
    cache_write_tokens: Count | None = field(default=None, kw_only=True)
    rubric_id: Identifier | None = field(default=None, kw_only=True)
    items: tuple[FeedbackItem, ...] = ()
    failure_feedback: tuple[FailureFeedbackItem, ...] = ()
    grading_status: Literal["complete", "pending_regrade"] = "complete"

    @property
    def usage(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cached_input_tokens,
            reasoning_tokens=self.reasoning_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )


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

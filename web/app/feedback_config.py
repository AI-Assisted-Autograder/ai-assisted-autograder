"""Validated database configuration and backend-owned runtime state records."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath
import re
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from ai_grading.FileResolver import FileResolver
from ai_grading.models import AnalysisConfig, Score, TargetKind


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Engine = Literal["vertex", "pitt", "gemini_api"]
ModelName = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9@][A-Za-z0-9_./:@+-]*$")]
ModelList = Annotated[list[ModelName], Field(min_length=1, max_length=64)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class BackendConfig(StrictModel):
    project: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")] | None = None
    location: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")] | None = None
    service_account: Annotated[str, Field(pattern=r"^[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$")] | None = None
    allowed_models: Annotated[dict[Engine, ModelList], Field(min_length=1)]
    max_code_chars: int = Field(default=12_000, ge=1, le=120_000)
    max_guidance_chars: int = Field(default=2_000, ge=0, le=16_000)
    max_output_tokens: int = Field(default=16_384, ge=1, le=65_536)
    timeout_seconds: float = Field(default=240.0, gt=0, le=300)
    max_concurrency: int = Field(default=2, ge=1, le=64)
    max_body_bytes: int = Field(default=65_536, ge=1, le=1_048_576)

    @field_validator("allowed_models")
    @classmethod
    def unique_models(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for engine, models in value.items():
            if len(models) != len(set(models)):
                raise ValueError("allowed models must be unique within each engine")
            for model in models:
                validate_engine_model(engine, model)
        return value

    @model_validator(mode="after")
    def vertex_configuration(self) -> "BackendConfig":
        if "vertex" in self.allowed_models and not all((self.project, self.location, self.service_account)):
            raise ValueError("Vertex requires project, location and service_account")
        return self


def validate_engine_model(engine: str, model: str) -> None:
    if engine == "pitt":
        route, separator, name = model.partition("/")
        if not route.startswith("@") or len(route) < 2 or not separator or not name:
            raise ValueError("Pitt requires the full @provider/model ID")
    elif not model.startswith("gemini-"):
        raise ValueError("Google engines require a Gemini model ID")


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or not path.parts or len(value) > 256 or path.is_absolute() or ".." in path.parts
            or "\\" in value or "\x00" in value or str(path) != value):
        raise ValueError("paths must be normalized relative POSIX paths")
    return value


def validate_analysis(value: AnalysisConfig) -> AnalysisConfig:
    if value.mode == "testplan" and not value.expected_tests:
        raise ValueError("testplan analysis requires expected test identifiers")
    if (len(value.source_globs) + len(value.context_globs) > 64
            or len(value.evidence_sources) > 64 or len(value.expected_tests) > 256
            or len(value.test_plan) > 64_000):
        raise ValueError("analysis configuration exceeds its limits")
    for pattern in (*value.source_globs, *value.context_globs):
        validate_relative_path(pattern)
    for source in value.evidence_sources:
        validate_relative_path(source.path)
        if len(source.label) > 256 or len(source.include) > 64:
            raise ValueError("evidence source exceeds its limits")
        for pattern in source.include:
            if len(pattern) > 512:
                raise ValueError("evidence selector is too long")
            try:
                re.compile(pattern)
            except re.error:
                raise ValueError("invalid evidence selector") from None
    if any(len(identifier) > 256 for identifier in value.expected_tests):
        raise ValueError("expected test identifier is too long")
    return value


Analysis = Annotated[AnalysisConfig, AfterValidator(validate_analysis)]


def resolve_target_analysis(identifier: str, kind: str, language: str,
                            analysis: dict | AnalysisConfig) -> AnalysisConfig:
    """Use explicit collection rules, or resolve a basic Web target's source file."""
    value = TypeAdapter(Analysis).validate_python(analysis)
    if value.source_globs:
        return value
    if kind == "FILE":
        patterns = (FileResolver._relative_pattern(identifier),)
    elif language.casefold() == "java":
        patterns = tuple("**/" + name for name in sorted(
            FileResolver._java_source_names(identifier, TargetKind(kind))))
    elif language.casefold() == "python":
        patterns = (FileResolver._python_source_path(identifier),)
    else:
        raise ValueError("Use a FILE target or specify source patterns for this language")
    return validate_analysis(replace(value, source_globs=patterns,
                                     max_source_files=value.max_source_files or 1))


def validate_target_scoring(target):
    points = target.analysis.points_per_test
    if points is not None:
        expected = Decimal(str(points)) * len(target.analysis.expected_tests)
        if target.max_score is None or Decimal(str(target.max_score)) != expected:
            raise ValueError("max_score must equal points_per_test times the expected test count")
    return target


class RubricInput(StrictModel):
    criteria_text: str = Field(min_length=1, max_length=16_000, pattern=r"\S")
    feedback_guidance: str = Field(default="", max_length=8_000)


class RubricConfig(RubricInput):
    version: int = Field(default=1, ge=1)
    id: Identifier


class TargetConfig(StrictModel):
    identifier: str = Field(min_length=1, max_length=256, pattern=r"\S")
    kind: Literal["FILE", "METHOD", "CLASS"]
    source_path: str = Field(min_length=1, max_length=256)
    rubric: RubricConfig
    analysis: Analysis = Field(default_factory=AnalysisConfig)
    max_score: Score | None = None

    @model_validator(mode="after")
    def scoring(self):
        return validate_target_scoring(self)


class AssignmentConfig(StrictModel):
    id: Identifier
    enabled: bool = True
    engine: Engine = "vertex"
    provider: Literal["GOOGLE", "OPENAI", "ANTHROPIC"] = "GOOGLE"
    model: ModelName
    # An assignment may not have any targets with a current rubric yet.
    targets: dict[Identifier, TargetConfig]

    @model_validator(mode="after")
    def selected_model(self) -> "AssignmentConfig":
        validate_engine_model(self.engine, self.model)
        if self.engine != "pitt" and self.provider != "GOOGLE":
            raise ValueError("Google engines require provider GOOGLE")
        return self


class CourseInput(StrictModel):
    name: str = Field(min_length=1, max_length=256, pattern=r"\S")
    term: str | None = Field(default=None, max_length=128)


class TargetInput(StrictModel):
    identifier: str = Field(min_length=1, max_length=256, pattern=r"\S")
    kind: Literal["FILE", "METHOD", "CLASS"] = "FILE"
    rubric: RubricInput
    analysis: Analysis = Field(default_factory=AnalysisConfig)
    max_score: Score | None = None

    @model_validator(mode="after")
    def scoring(self):
        return validate_target_scoring(self)


class AssignmentCreateInput(StrictModel):
    course_id: UUID | None = Field(default=None, strict=False)
    course: CourseInput | None = None
    platform: Literal["GRADESCOPE"] = "GRADESCOPE"
    platform_assignment_id: str = Field(min_length=1, max_length=256, pattern=r"\S")
    name: str = Field(min_length=1, max_length=256, pattern=r"\S")
    language: str = Field(min_length=1, max_length=64, pattern=r"\S")
    grading_mode: Literal["IMMEDIATE", "NIGHTLY"] = "IMMEDIATE"
    targets: list[TargetInput] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_configuration(self) -> "AssignmentCreateInput":
        if (self.course_id is None) == (self.course is None):
            raise ValueError("Provide either course_id or a new course, not both")
        identifiers = [target.identifier for target in self.targets]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Target identifiers must be unique within the assignment")
        return self


class CreatedAssignmentConfig(AssignmentConfig):
    course_id: UUID


class AssignmentAccess(StrictModel):
    enabled: bool = True
    max_calls: int = Field(default=5, ge=0, le=100_000)
    used_calls: int = Field(default=0, ge=0)


class TokenRecord(StrictModel):
    assignment_id: Identifier
    expires_at: datetime
    enabled: bool = True
    revoked: bool = False

    @field_validator("expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiration must include a timezone")
        return value


class RequestRecord(StrictModel):
    request_id: Identifier
    target_id: Identifier
    engine: Engine = "vertex"
    provider: Literal["GOOGLE", "OPENAI", "ANTHROPIC"] = "GOOGLE"
    model: ModelName
    digest: Digest
    status: Literal["processing", "completed", "failed", "interrupted"]
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    error_code: Literal[
        "model_error", "upstream_timeout", "backend_auth_unavailable", "interrupted",
    ] | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def terminal_fields(self) -> "RequestRecord":
        if self.status == "completed":
            if self.result is None or self.error_code is not None:
                raise ValueError("completed requests require only a result")
        elif self.result is not None:
            raise ValueError("unfinished requests cannot have a result")
        if self.status in ("failed", "interrupted") and self.error_code is None:
            raise ValueError("unsuccessful requests require a safe error code")
        return self


class State(StrictModel):
    schema_version: Literal[3] = 3
    config: BackendConfig
    assignments: dict[Identifier, AssignmentAccess]
    tokens: dict[Digest, TokenRecord]
    requests: dict[Identifier, dict[Identifier, RequestRecord]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_relationships(self) -> "State":
        for assignment_id, assignment in self.assignments.items():
            if assignment.used_calls < len(self.requests.get(assignment_id, {})):
                raise ValueError("quota counter cannot be below reserved request count")
        for token in self.tokens.values():
            if token.assignment_id not in self.assignments:
                raise ValueError("token assignment does not exist")
        for assignment_id, requests in self.requests.items():
            if assignment_id not in self.assignments:
                raise ValueError("request assignment does not exist")
            for request_id, record in requests.items():
                if request_id != record.request_id:
                    raise ValueError("request key must match request id")
        return self

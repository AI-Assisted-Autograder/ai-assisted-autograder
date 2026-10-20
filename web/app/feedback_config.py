"""Validated database configuration and backend-owned runtime state records."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
ModelName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^gemini-[a-z0-9.-]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class BackendConfig(StrictModel):
    project: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")]
    location: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
    service_account: Annotated[str, Field(pattern=r"^[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$")]
    allowed_models: Annotated[list[ModelName], Field(min_length=1, max_length=16)]
    max_code_chars: int = Field(default=12_000, ge=1, le=120_000)
    max_guidance_chars: int = Field(default=2_000, ge=0, le=16_000)
    max_output_tokens: int = Field(default=1_024, ge=1, le=8_192)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_concurrency: int = Field(default=2, ge=1, le=64)
    max_body_bytes: int = Field(default=65_536, ge=1, le=1_048_576)

    @field_validator("allowed_models")
    @classmethod
    def unique_models(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("allowed models must be unique")
        return value


class RubricInput(StrictModel):
    criteria_text: str = Field(min_length=1, max_length=16_000, pattern=r"\S")
    feedback_guidance: str = Field(default="", max_length=8_000)


class RubricConfig(RubricInput):
    version: int = Field(default=1, ge=1)
    id: Identifier


class TargetConfig(StrictModel):
    identifier: str = Field(min_length=1, max_length=256)
    kind: Literal["FILE", "METHOD", "CLASS"]
    source_path: str = Field(min_length=1, max_length=256)
    rubric: RubricConfig


class AssignmentConfig(StrictModel):
    id: Identifier
    enabled: bool = True
    model: ModelName
    targets: Annotated[dict[Identifier, TargetConfig], Field(min_length=1)]


class CourseInput(StrictModel):
    name: str = Field(min_length=1, max_length=256, pattern=r"\S")
    term: str | None = Field(default=None, max_length=128)


class TargetInput(StrictModel):
    identifier: str = Field(min_length=1, max_length=256, pattern=r"\S")
    kind: Literal["FILE", "METHOD", "CLASS"] = "FILE"
    rubric: RubricInput


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
    schema_version: Literal[2] = 2
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

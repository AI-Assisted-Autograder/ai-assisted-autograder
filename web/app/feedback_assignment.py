"""Read feedback assignment configuration from the existing grading database."""

from http.client import HTTPException
import math
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from pydantic import TypeAdapter

from .feedback_config import (
    AssignmentConfig, Engine, ModelName, RubricConfig, TargetConfig, validate_engine_model,
)
from .feedback_store import StoreError


MAX_CONFIG_BYTES = 1_048_576


def primary_model_config(providers) -> dict[str, str]:
    """Select the enabled primary engine and model without changing global policy."""
    primary = [provider for provider in providers if provider.is_primary]
    try:
        if len(primary) != 1:
            raise ValueError("Exactly one enabled primary provider is required")
        selected = primary[0]
        engine = TypeAdapter(Engine).validate_python(selected.engine)
        model = TypeAdapter(ModelName).validate_python(selected.model_name)
        if engine in ("vertex", "gemini_api") and selected.provider != "GOOGLE":
            raise ValueError("Google engines require a GOOGLE provider")
        validate_engine_model(engine, model)
        return {"engine": engine, "provider": selected.provider, "model": model}
    except ValueError:
        raise StoreError("assignment_config_unavailable", 503) from None


def read_assignment_config(repository, assignment_id: str) -> AssignmentConfig:
    """Adapt the repository's current configuration without writing grading data."""
    # Only the direct database path imports the SQL-backed repository package.
    from app.repository.base import NotFoundError

    try:
        key = UUID(assignment_id)
    except ValueError:
        raise StoreError("assignment_forbidden", 403) from None
    try:
        assignment = repository.get_assignment_config(key)
    except NotFoundError:
        raise StoreError("assignment_forbidden", 403) from None
    except Exception:
        raise StoreError("assignment_config_unavailable", 503) from None
    if not assignment.is_active:
        raise StoreError("assignment_forbidden", 403)
    try:
        model_config = primary_model_config(assignment.providers)
        targets = {}
        for target in assignment.targets:
            rubric = target.rubric
            if rubric is None:
                raise ValueError("Each target requires a current rubric")
            targets[str(target.id)] = TargetConfig(
                identifier=target.identifier,
                kind=target.kind,
                source_path=target.identifier if target.kind == "FILE" else "submitted-source",
                rubric=RubricConfig(
                    id=str(rubric.id), version=rubric.version,
                    criteria_text=rubric.criteria_text,
                    feedback_guidance=rubric.feedback_guidance or "",
                ),
            )
        return AssignmentConfig(
            id=str(assignment.id), enabled=assignment.is_active,
            **model_config, targets=targets,
        )
    except (TypeError, ValueError):
        raise StoreError("assignment_config_unavailable", 503) from None


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpAssignmentConfigReader:
    """Use the protected Web API when the AI backend runs on another host."""

    def __init__(self, api_url: str, internal_token: str, timeout: float = 10):
        try:
            address = urlsplit(api_url)
            valid = (
                bool(address.hostname)
                and (address.scheme == "https" or (
                    address.scheme == "http" and address.hostname in {"127.0.0.1", "::1"}
                ))
                and address.username is None and address.password is None
                and address.path in ("", "/") and not address.query and not address.fragment
                and (address.port is None or 1 <= address.port <= 65535)
                and not any(character.isspace() for character in api_url)
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Assignment API URL must be an HTTPS or loopback HTTP origin")
        if (not internal_token or len(internal_token) > 4096
                or any(not 33 <= ord(character) <= 126 for character in internal_token)):
            raise ValueError("A valid internal API token is required")
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("Assignment API timeout must be between zero and 300 seconds")
        self._origin = api_url.rstrip("/")
        self._token = internal_token
        self._timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())

    def __call__(self, assignment_id: str) -> AssignmentConfig:
        try:
            key = str(UUID(assignment_id))
        except ValueError:
            raise StoreError("assignment_forbidden", 403) from None
        request = Request(
            f"{self._origin}/internal/assignments/{key}/config",
            headers={"X-Internal-Token": self._token, "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError("Assignment configuration exceeds the response limit")
            assignment = AssignmentConfig.model_validate_json(raw)
            if assignment.id != key:
                raise ValueError("Assignment configuration does not match the request")
        except HTTPError as error:
            status = error.code
            error.close()
            if status in (403, 404):
                raise StoreError("assignment_forbidden", 403) from None
            raise StoreError("assignment_config_unavailable", 503) from None
        except (HTTPException, OSError, ValueError):
            raise StoreError("assignment_config_unavailable", 503) from None
        if not assignment.enabled:
            raise StoreError("assignment_forbidden", 403)
        return assignment

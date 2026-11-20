"""Read feedback configuration and explicitly migrate reviewed assignments."""

from dataclasses import asdict
from decimal import Decimal
import hashlib
from http.client import HTTPException
import math
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from pydantic import TypeAdapter

from .assignment_context import with_assignment_context
from .feedback_config import (
    AssignmentConfig, Engine, ModelName, RubricConfig, TargetConfig, TargetSettings, resolve_target_analysis,
    validate_engine_model,
)
from .feedback_store import StoreError


MAX_CONFIG_BYTES = 1_048_576


def connector_config(origin: str, assignment) -> dict[str, str | list[str]]:
    """Package the transport mode and any extra local Gradescope report paths."""
    config: dict[str, str | list[str]] = {"backend_url": origin}
    if assignment.ai_mode is not None:
        config["ai_mode"] = assignment.ai_mode
    if assignment.ai_mode == "GRADING":
        paths = {
            source["path"]
            for target in assignment.targets if target.rubric is not None
            for source in target.analysis_config.get("evidence_sources", ())
            if source["format"] == "gradescope" and source["path"] != "results/results.json"
        }
        if paths:
            config["grading_report_paths"] = sorted(paths)
    return config


def _playwright_guidance(guidance: str) -> tuple[str, str]:
    """Move the prototype-aligned policy without rewriting its scoring language."""
    guidance = guidance.replace("\r\n", "\n").strip()
    fingerprint = hashlib.sha256(guidance.encode("utf-8")).hexdigest()
    if fingerprint != "e40f8bb2547eac7c8556708be4ec887feb505e7f72c9c0d9a12cf88f24f96185":
        raise ValueError("Playwright activation requires the prototype-aligned guidance")
    paragraphs = guidance.split("\n\n")
    # Paragraphs 1 and 7 describe the superseded fields and response protocol.
    return "\n\n".join((paragraphs[2], paragraphs[6])), "\n\n".join((paragraphs[0], *paragraphs[3:6]))


def _legacy_behavior(platform_id, target, rubric):
    """Prepare one known assignment's conversion without writing database state."""
    analysis = dict(target.analysis_config)
    maximum = None
    if platform_id == "8904668":
        plan = analysis.get("test_plan", "")
        for text, expected in (
            (rubric.criteria_text or "", "5f8fbf0fc62fd89c259400736252bc973341650458cfa76688917becd4483e50"),
            (plan, "9e839bf889ce20dc1d495bfbdce648b5674a1190947e1f7367f59e7a53a2c697"),
        ):
            if hashlib.sha256(text.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest() != expected:
                raise ValueError("Playwright rubric and test plan must match the original prototype before activation")
        sections = re.split(r"(?m)^IDENTIFIER:\s*([^\n]+)\n", plan)
        fixture = sections[0].split("TEST CASES:", 1)[0].strip()
        cases = {sections[i].strip(): re.sub(r"(?m)^\s*```\s*$", "", sections[i + 1]).strip()
                 for i in range(1, len(sections), 2)}
        identifiers = analysis.get("expected_tests", [])
        points = Decimal(str(analysis.get("points_per_test")))
        if len(identifiers) != 11 or identifiers != list(cases) or points != 5:
            raise ValueError("Playwright requires the prototype's ordered 11 tests at 5 points each")
        maximum = points * len(identifiers)
        if maximum != target.max_score:
            raise ValueError("The Playwright configured total does not match its grading items")
        grading_rules, guidance = _playwright_guidance(rubric.feedback_guidance or "")
        analysis.update(
            behavior="ai_grading", expected_tests=[], points_per_test=None, test_plan="",
            shared_grading_rules="\n\n".join((fixture, rubric.criteria_text or "", grading_rules)),
            evidence_sources=[source for source in analysis.get("evidence_sources", [])
                              if source.get("format") == "gradescope"],
            grading_items=[{
                "id": identifier, "points": float(points),
                "requirement": "IDENTIFIER: " + identifier + "\n" + cases[identifier],
            } for identifier in identifiers],
        )
        background = "Assess the submitted Playwright tests against their configured grading items."
    else:
        sources = [dict(source) for source in analysis.get("evidence_sources", [])]
        final_sources = [source for source in sources if source.get("format") == "gradescope"
                         and source.get("path") == "results/results.json"]
        if len(final_sources) != 1:
            raise ValueError("The Java configuration requires one final Gradescope result source")
        final_sources[0]["include"] = []
        analysis.update(
            behavior="functional_test_feedback", mode="code_feedback", grading_items=[],
            expected_tests=[], points_per_test=None, evidence_sources=final_sources,
            functional_result_source=final_sources[0]["label"],
        )
        background = (
            "This Java assignment includes student implementations and unit, integration and "
            "system tests. Explain only the supplied failed final functional-grading rows. "
            "A row can grade whether a student test detects a buggy implementation; a raw "
            "failure on that implementation is not itself a student defect. Preserve the "
            "reported grading outcome and use the public assignment context to explain it."
        )
        guidance = (
            "Give a concise explanation for each selected failed row, tied to that row's "
            "diagnostic and the supplied student source. Distinguish observed facts from possible "
            "causes, and acknowledge missing evidence. Do not offer replacement code or "
            "step-by-step solutions, change scores, assess unrelated tests, or give an overall report."
        )
    settings = TargetSettings(identifier=target.identifier, kind=target.kind,
                              analysis=analysis, max_score=maximum)
    return asdict(settings.analysis), settings.max_score, background, guidance


def migrate_assignment_behavior(session_factory, assignment_id: UUID) -> bool:
    """Explicitly activate one reviewed configuration after its container is upgraded."""
    from sqlalchemy import func, select
    from app.models import Assignment, GradingTarget, Rubric

    known_targets = {"8904668": "tests/rentacat.spec.ts", "8904669": "Exercise2OverallFeedback"}
    with session_factory.begin() as session:
        assignment = session.get(Assignment, assignment_id, with_for_update=True)
        if (assignment is None or assignment.platform != "GRADESCOPE"
                or assignment.platform_assignment_id not in known_targets):
            raise ValueError("This command only activates the two reviewed RentACat assignments")
        target = session.scalar(select(GradingTarget).where(
            GradingTarget.assignment_id == assignment_id,
            GradingTarget.identifier == known_targets[assignment.platform_assignment_id],
        ).with_for_update())
        if target is None:
            raise ValueError("The reviewed grading target is missing")
        if target.analysis_config.get("behavior") is not None:
            return False
        rubric = session.scalar(select(Rubric).where(
            Rubric.grading_target_id == target.id, Rubric.is_current,
        ).with_for_update())
        if rubric is None:
            raise ValueError("The reviewed target has no current rubric")
        analysis, maximum, background, guidance = _legacy_behavior(
            assignment.platform_assignment_id, target, rubric,
        )
        if analysis["behavior"] == "functional_test_feedback":
            others = session.scalars(select(GradingTarget).join(Rubric).where(
                GradingTarget.assignment_id == assignment_id, GradingTarget.id != target.id, Rubric.is_current,
            ))
            if any(other.analysis_config.get("behavior") == "functional_test_feedback" for other in others):
                raise ValueError("The assignment already has an active Functional-test feedback target")
        version = session.scalar(select(func.max(Rubric.version)).where(Rubric.grading_target_id == target.id))
        rubric.is_current = False
        session.flush()
        target.analysis_config, target.max_score = analysis, maximum
        session.add(Rubric(grading_target_id=target.id, version=version + 1, is_current=True,
                           criteria_text=background, feedback_guidance=guidance))
    return True


def _shared_playwright_analysis(target) -> dict | None:
    """Extract the reviewed common rules, preserving every item and its points."""
    analysis = dict(target.analysis_config)
    if analysis.get("shared_grading_rules"):
        return None
    items = analysis.get("grading_items", [])
    if (analysis.get("behavior") != "ai_grading" or len(items) != 11
            or target.max_score != 55 or any(item["points"] != 5 for item in items)):
        raise ValueError("Shared rubric migration requires the reviewed 11 grading items at 5 points each")
    cases = []
    shared = None
    for item in items:
        marker = "IDENTIFIER: " + item["id"] + "\n"
        prefix, separator, case = item["requirement"].partition(marker)
        if (not separator or not case.strip()
                or hashlib.sha256(prefix.encode("utf-8")).hexdigest()
                != "7ec5762b919cce071426ff62baf66d9211c4c5d9e44079930de4a3da6861b69d"):
            raise ValueError("Each item must contain the reviewed common rubric followed by its own test case")
        # These two newlines separate the unchanged shared rules from the case.
        shared = prefix[:-2]
        cases.append({**item, "requirement": separator + case})
    analysis.update(shared_grading_rules=shared, grading_items=cases)
    settings = TargetSettings(identifier=target.identifier, kind=target.kind,
                              analysis=analysis, max_score=target.max_score)
    return asdict(settings.analysis)


def migrate_shared_rubric(session_factory, assignment_id: UUID) -> bool:
    """Explicitly deduplicate the activated Exercise 4 configuration, once."""
    from sqlalchemy import select
    from app.models import Assignment, GradingTarget

    with session_factory.begin() as session:
        assignment = session.get(Assignment, assignment_id, with_for_update=True)
        if (assignment is None or assignment.platform != "GRADESCOPE"
                or assignment.platform_assignment_id != "8904668"):
            raise ValueError("This command only migrates the reviewed Exercise 4 assignment")
        target = session.scalar(select(GradingTarget).where(
            GradingTarget.assignment_id == assignment_id,
            GradingTarget.identifier == "tests/rentacat.spec.ts",
        ).with_for_update())
        if target is None:
            raise ValueError("The reviewed grading target is missing")
        analysis = _shared_playwright_analysis(target)
        if analysis is None:
            return False
        target.analysis_config = analysis
    return True


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


def read_active_assignment(repository, assignment_id: str):
    """Read one active assignment snapshot, including targets without a rubric."""
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
    if not assignment.is_active or str(assignment.id) != assignment_id:
        raise StoreError("assignment_forbidden", 403)
    return assignment


def feedback_assignment_config(assignment) -> AssignmentConfig:
    """Derive the selected model and currently gradable targets from one snapshot."""
    try:
        model_config = primary_model_config(assignment.providers)
        targets = {}
        for target in assignment.targets:
            rubric = target.rubric
            if rubric is None:
                continue
            targets[str(target.id)] = TargetConfig(
                identifier=target.identifier,
                kind=target.kind,
                source_path=target.identifier if target.kind == "FILE" else "submitted-source",
                analysis=resolve_target_analysis(target.identifier, target.kind,
                                                 assignment.language, target.analysis_config),
                max_score=float(target.max_score) if target.max_score is not None else None,
                rubric=RubricConfig(
                    id=str(rubric.id), version=rubric.version,
                    # The instructor's notes and uploaded documents ride along in
                    # each target's background (app.assignment_context).
                    criteria_text=with_assignment_context(
                        rubric.criteria_text, assignment.context_notes, assignment.context_files,
                    ),
                    feedback_guidance=rubric.feedback_guidance or "",
                ),
            )
        return AssignmentConfig(
            id=str(assignment.id), enabled=assignment.is_active,
            grading_mode=assignment.grading_mode,
            pitt_key_source=assignment.pitt_key_source if model_config["engine"] == "pitt" else "server",
            pitt_key_version=assignment.pitt_key_version if model_config["engine"] == "pitt" else 0,
            **model_config, targets=targets,
        )
    except (TypeError, ValueError):
        raise StoreError("assignment_config_unavailable", 503) from None


def read_assignment_config(repository, assignment_id: str) -> AssignmentConfig:
    return feedback_assignment_config(read_active_assignment(repository, assignment_id))


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


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Explicitly migrate reviewed AI assignment configuration.")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--migrate-behavior", action="store_true",
                         help="Activate a reviewed behavior after updating its autograder ZIP; never run by Web startup or Alembic")
    actions.add_argument("--migrate-shared-rubric", action="store_true",
                         help="Extract Exercise 4's repeated rubric without changing its rules, test cases or points")
    parser.add_argument("--assignment-id", required=True, type=UUID)
    args = parser.parse_args()
    from .db import SessionLocal

    try:
        migrate = migrate_shared_rubric if args.migrate_shared_rubric else migrate_assignment_behavior
        changed = migrate(SessionLocal, args.assignment_id)
    except ValueError as exc:
        parser.exit(2, f"Migration refused: {exc}.\n")
    except Exception:
        parser.exit(2, "Migration failed; check the database connection and current assignment configuration.\n")
    if args.migrate_shared_rubric:
        message = "shared rubric extracted; all test cases and points preserved." if changed else "shared rubric already configured; unchanged."
    else:
        message = ("behavior activated; only Gradescope evidence retained (Java: the selected final report)."
                   if changed else "behavior already explicit; unchanged.")
    print(f"Assignment {args.assignment_id}: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

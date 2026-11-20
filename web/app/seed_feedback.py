"""Install an existing assignment's fixed token and initialize runtime state.

No overwrite/reset mode exists. Stop Web before adding an assignment to existing
state. This tool never prints a token or loads provider credentials.
"""

import argparse
import json
from pathlib import Path
from uuid import UUID

from ai_grading.local_client import validate_backend_url

from .feedback_assignment import connector_config
from .feedback_config import (
    AssignmentAccess, BackendConfig, State, TokenRecord,
)
from .feedback_store import JsonStore, StoreError, atomic_write, token_hash


DEFAULT_LOCATION = "global"
DEFAULT_BACKEND_URL = "http://127.0.0.1:8001"


def initialize(
    state_path: str | Path = "ai_grading/.local-vertex/backend/state.json",
    token_path: str | Path = "ai_grading/.local-vertex/client/token",
    client_config_path: str | Path = "ai_grading/.local-vertex/client/config.json",
    *,
    assignment_id: str,
    max_calls: int = 5,
    backend_url: str | None = None,
    project: str | None = None,
    location: str = DEFAULT_LOCATION,
    service_account: str | None = None,
    allowed_models: list[str] | None = None,
    engine_models: dict[str, list[str]] | None = None,
    add_assignment: bool = False,
    max_code_chars: int | None = None,
    max_output_tokens: int | None = None,
    max_body_bytes: int | None = None,
    timeout_seconds: float | None = None,
) -> None:
    """Export the database token and initialize or append assignment access."""
    from app.db import SessionLocal
    from app.repository import SqlGradingRepository

    assignment_uuid = UUID(assignment_id)
    assignment_id = str(assignment_uuid)
    paths = tuple(Path(path).expanduser().absolute()
                  for path in (state_path, token_path, client_config_path))
    outputs = paths[1:] if add_assignment else paths
    if (len(set(paths)) != 3 or paths[0].is_symlink()
            or any(path.exists() or path.is_symlink() for path in outputs)):
        raise FileExistsError("outputs must be distinct new files")
    policy = {key: value for key, value in {
        "max_code_chars": max_code_chars, "max_output_tokens": max_output_tokens,
        "max_body_bytes": max_body_bytes, "timeout_seconds": timeout_seconds,
    }.items() if value is not None}
    if add_assignment and (engine_models or allowed_models or policy):
        raise ValueError("adding an assignment cannot replace backend policy")
    repository = SqlGradingRepository(SessionLocal)
    assignment = repository.get_assignment_config(assignment_uuid)
    if backend_url is None:
        # The single-run endpoint needs the combined Web service's grading repository.
        backend_url = "http://127.0.0.1:8000" if assignment.ai_mode == "GRADING" else DEFAULT_BACKEND_URL
    client_config = connector_config(validate_backend_url(backend_url), assignment)
    raw_token = repository.get_assignment_token(assignment_uuid)
    models = {key: list(value) for key, value in (engine_models or {}).items()}
    if allowed_models:
        models.setdefault("vertex", []).extend(allowed_models)
    if not models and not add_assignment:
        raise ValueError("Initial state requires at least one --allow-model ENGINE:MODEL")
    vertex = "vertex" in models
    client_payloads = (
        (raw_token + "\n").encode("utf-8"),
        (json.dumps(client_config, indent=2) + "\n").encode("utf-8"),
    )
    if add_assignment:
        store = JsonStore(paths[0])
        try:
            store.ensure_assignment_token(assignment_id, raw_token, max_calls=max_calls)
        finally:
            store.close()
        payloads = client_payloads
    else:
        state = State(
            config=BackendConfig(project=project if vertex else None,
                                 location=location if vertex else None,
                                 service_account=service_account if vertex else None,
                                 allowed_models=models, **policy),
            assignments={assignment_id: AssignmentAccess(max_calls=max_calls)},
            tokens={token_hash(raw_token): TokenRecord(assignment_id=assignment_id)},
        )
        payloads = (state.model_dump_json(indent=2).encode("utf-8") + b"\n", *client_payloads)
    created: list[Path] = []
    try:
        for path, payload in zip(outputs, payloads):
            atomic_write(path, payload, overwrite=False)
            created.append(path)
    except Exception:
        # Only roll back files created by this invocation, never preexisting files.
        for path in created:
            path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="ai_grading/.local-vertex/backend/state.json")
    parser.add_argument("--token-file", default="ai_grading/.local-vertex/client/token")
    parser.add_argument("--client-config", default="ai_grading/.local-vertex/client/config.json")
    parser.add_argument("--assignment-id", required=True, type=UUID,
                        help="Existing database assignment UUID; does not create database rows")
    parser.add_argument("--add-assignment", action="store_true",
                        help="Use existing state; stop Web first and choose new client file paths")
    parser.add_argument("--max-calls", type=int, default=5)
    # Older provisioning commands supplied this option for rotating credentials.
    parser.add_argument("--expires-in-hours", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--backend-url", help="Backend origin reachable from the autograder; Grading uses the combined Web service")
    parser.add_argument("--project", help="Vertex project ID; required when enabling Vertex")
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--service-account", help="Impersonated service account; required when enabling Vertex")
    parser.add_argument("--allowed-model", action="append", dest="allowed_models",
                        help="Allowed Vertex model (legacy option); repeat for multiple models")
    parser.add_argument("--allow-model", action="append", default=[], metavar="ENGINE:MODEL",
                        help="Allow an engine/model pair; repeat for multiple models or engines")
    parser.add_argument("--max-code-chars", type=int, help="Initial state: total source/context characters")
    parser.add_argument("--max-output-tokens", type=int, help="Initial state: provider output token limit")
    parser.add_argument("--max-body-bytes", type=int, help="Initial state: HTTP request size limit")
    parser.add_argument("--timeout-seconds", type=float, help="Initial state: provider request timeout")
    args = parser.parse_args()
    engine_models = {}
    for item in args.allow_model:
        engine, separator, model = item.partition(":")
        if not separator or engine not in ("vertex", "pitt", "gemini_api") or not model:
            parser.error("--allow-model requires vertex:MODEL, pitt:MODEL or gemini_api:MODEL")
        engine_models.setdefault(engine, []).append(model)
    try:
        initialize(args.state, args.token_file, args.client_config,
                   assignment_id=str(args.assignment_id),
                   max_calls=args.max_calls,
                   backend_url=args.backend_url, project=args.project, location=args.location,
                   service_account=args.service_account, allowed_models=args.allowed_models,
                   engine_models=engine_models, add_assignment=args.add_assignment,
                   max_code_chars=args.max_code_chars, max_output_tokens=args.max_output_tokens,
                   max_body_bytes=args.max_body_bytes, timeout_seconds=args.timeout_seconds)
    except FileExistsError:
        parser.exit(2, "Initialization refused: an output already exists or paths overlap.\n")
    except StoreError as error:
        message = ("Stop Web before adding an assignment." if error.code == "worker_already_running"
                   else f"Assignment access could not be prepared: {error.code}.")
        parser.exit(2, message + "\n")
    except Exception:
        parser.exit(2, "Initialization failed: check paths and non-sensitive settings.\n")
    action = "Prepared" if args.add_assignment else "Initialized"
    print(f"{action} runtime access for assignment {args.assignment_id}; private token written to the token file.")
    print("No token or Google credentials are included in this output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

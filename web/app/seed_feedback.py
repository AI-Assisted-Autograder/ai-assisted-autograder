"""Issue a private token and initialize runtime state for an existing DB assignment.

No overwrite/reset mode exists. This tool never prints a token or loads Google
credentials. Initialize once before enabling the feedback router.
"""

import argparse
import json
import math
from datetime import timedelta
from pathlib import Path
import secrets
from uuid import UUID

from ai_grading.local_client import validate_backend_url

from .feedback_config import (
    AssignmentAccess, BackendConfig, State, TokenRecord,
)
from .feedback_store import atomic_write, token_hash, utc_now


DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_PROJECT = "project-bcedce55-e972-4a7b-bef"
DEFAULT_LOCATION = "global"
DEFAULT_SERVICE_ACCOUNT = (
    "gradescope-vertex-test@project-bcedce55-e972-4a7b-bef.iam.gserviceaccount.com"
)
DEFAULT_BACKEND_URL = "http://127.0.0.1:8001"


def initialize(
    state_path: str | Path = "ai_grading/.local-vertex/backend/state.json",
    token_path: str | Path = "ai_grading/.local-vertex/client/token",
    client_config_path: str | Path = "ai_grading/.local-vertex/client/config.json",
    *,
    assignment_id: str,
    max_calls: int = 5,
    expires_in_hours: float = 24.0,
    backend_url: str = DEFAULT_BACKEND_URL,
    project: str = DEFAULT_PROJECT,
    location: str = DEFAULT_LOCATION,
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
    allowed_models: list[str] | None = None,
    engine_models: dict[str, list[str]] | None = None,
) -> None:
    """Create config/state and issue a random token; refuse every existing output."""
    assignment_id = str(UUID(assignment_id))
    backend_url = validate_backend_url(backend_url)
    if not math.isfinite(expires_in_hours) or not 0 < expires_in_hours <= 24 * 365:
        raise ValueError("expiration must be between zero and one year")
    paths = tuple(Path(path).expanduser().absolute()
                  for path in (state_path, token_path, client_config_path))
    if len(set(paths)) != 3 or any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("initialization outputs must be distinct new files")
    raw_token = secrets.token_urlsafe(32)
    models = {key: list(value) for key, value in (engine_models or {}).items()}
    if allowed_models:
        models.setdefault("vertex", []).extend(allowed_models)
    if not models:
        models = {"vertex": [DEFAULT_MODEL]}
    vertex = "vertex" in models
    state = State(
        config=BackendConfig(project=project if vertex else None,
                             location=location if vertex else None,
                             service_account=service_account if vertex else None,
                             allowed_models=models),
        assignments={assignment_id: AssignmentAccess(max_calls=max_calls)},
        tokens={token_hash(raw_token): TokenRecord(
            assignment_id=assignment_id, expires_at=utc_now() + timedelta(hours=expires_in_hours),
        )},
    )
    payloads = (
        state.model_dump_json(indent=2).encode("utf-8") + b"\n",
        (raw_token + "\n").encode("utf-8"),
        (json.dumps({"backend_url": backend_url.rstrip("/")}, indent=2) + "\n").encode("utf-8"),
    )
    created: list[Path] = []
    try:
        for path, payload in zip(paths, payloads):
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
    parser.add_argument("--max-calls", type=int, default=5)
    parser.add_argument("--expires-in-hours", type=float, default=24.0)
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--service-account", default=DEFAULT_SERVICE_ACCOUNT)
    parser.add_argument("--allowed-model", action="append", dest="allowed_models",
                        help="Allowed Vertex model (legacy option); repeat for multiple models")
    parser.add_argument("--allow-model", action="append", default=[], metavar="ENGINE:MODEL",
                        help="Allow an engine/model pair; repeat for multiple models or engines")
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
                   max_calls=args.max_calls, expires_in_hours=args.expires_in_hours,
                   backend_url=args.backend_url, project=args.project, location=args.location,
                   service_account=args.service_account, allowed_models=args.allowed_models,
                   engine_models=engine_models)
    except FileExistsError:
        parser.exit(2, "Initialization refused: an output already exists or paths overlap.\n")
    except Exception:
        parser.exit(2, "Initialization failed: check paths and non-sensitive settings.\n")
    print(f"Initialized runtime state for assignment {args.assignment_id}; private token written to the token file.")
    print("No token or Google credentials are included in this output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

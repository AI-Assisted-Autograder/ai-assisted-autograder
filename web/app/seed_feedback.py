"""Initialize local JSON state and issue one private assignment token.

No overwrite/reset mode exists. This tool never prints a token or loads Google
credentials. Initialization is intended for a new local demonstration directory.
"""

import argparse
import json
import math
from datetime import timedelta
from pathlib import Path
import secrets

from .feedback_config import (
    AssignmentConfig, BackendConfig, RubricConfig, State, TargetConfig, TokenRecord,
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
    state_path: str | Path = ".local-vertex/backend/state.json",
    token_path: str | Path = ".local-vertex/client/token",
    client_config_path: str | Path = ".local-vertex/client/config.json",
    *,
    max_calls: int = 5,
    expires_in_hours: float = 24.0,
    backend_url: str = DEFAULT_BACKEND_URL,
) -> None:
    """Create config/state and issue a random token; refuse every existing output."""
    from urllib.parse import urlsplit

    address = urlsplit(backend_url)
    if (address.scheme != "http" or address.hostname not in ("127.0.0.1", "::1")
            or address.username is not None or address.password is not None
            or address.path not in ("", "/") or address.query or address.fragment
            or any(character.isspace() for character in backend_url)
            or address.port is not None and not 1 <= address.port <= 65535):
        raise ValueError("backend URL must be a local HTTP origin")
    if not math.isfinite(expires_in_hours) or not 0 < expires_in_hours <= 24 * 365:
        raise ValueError("expiration must be between zero and one year")
    paths = tuple(Path(path).expanduser().absolute()
                  for path in (state_path, token_path, client_config_path))
    if len(set(paths)) != 3 or any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("initialization outputs must be distinct new files")
    raw_token = secrets.token_urlsafe(32)
    state = State(
        config=BackendConfig(project=DEFAULT_PROJECT, location=DEFAULT_LOCATION,
                             service_account=DEFAULT_SERVICE_ACCOUNT,
                             allowed_models=[DEFAULT_MODEL]),
        assignments={"demo": AssignmentConfig(
            model=DEFAULT_MODEL, max_calls=max_calls,
            targets={"example": TargetConfig(rubric=RubricConfig(
                criteria_text=("Review whether the function correctly computes an average, "
                               "including its behavior for empty input. "
                               "Give concise, constructive feedback on correctness and clarity. "
                               "Do not assign a numerical score."),
            ))},
        )},
        tokens={token_hash(raw_token): TokenRecord(
            assignment_id="demo", expires_at=utc_now() + timedelta(hours=expires_in_hours),
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
    parser.add_argument("--state", default=".local-vertex/backend/state.json")
    parser.add_argument("--token-file", default=".local-vertex/client/token")
    parser.add_argument("--client-config", default=".local-vertex/client/config.json")
    parser.add_argument("--max-calls", type=int, default=5)
    parser.add_argument("--expires-in-hours", type=float, default=24.0)
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL)
    args = parser.parse_args()
    try:
        initialize(args.state, args.token_file, args.client_config,
                   max_calls=args.max_calls, expires_in_hours=args.expires_in_hours,
                   backend_url=args.backend_url)
    except FileExistsError:
        parser.exit(2, "Initialization refused: an output already exists or paths overlap.\n")
    except Exception:
        parser.exit(2, "Initialization failed: check paths and non-sensitive settings.\n")
    print("Initialized assignment demo, target example; private token written to the token file.")
    print("No token or Google credentials are included in this output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

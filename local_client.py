#!/usr/bin/env python3
"""HTTP feedback client. Requires only the Python standard library.

The assignment token file is the client's only credential. This module never
imports a Google SDK, discovers ADC, or falls back to a model endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_BACKEND_URL = "http://127.0.0.1:8001"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CODE_BYTES = 64 * 1024


class ClientError(Exception):
    """An error with a deliberately safe, user-facing message."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_backend_url(value: str) -> str:
    """Accept HTTPS origins, with plain HTTP allowed only on loopback."""
    try:
        parsed = urlsplit(value)
        valid = (
            bool(parsed.hostname)
            and (parsed.scheme == "https" or (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
            ))
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and not any(c.isspace() for c in value)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ClientError("Backend URL must be an HTTPS origin or HTTP loopback origin.")
    return value.rstrip("/")


def read_assignment_token(path: str | Path) -> str:
    """Read a private, regular token file without following its symlink."""
    descriptor = None
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode):
            raise ClientError("Assignment token file must not be a symlink.")
        if not stat.S_ISREG(before.st_mode):
            raise ClientError("Assignment token file must be a regular file.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
            or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ClientError("Assignment token file must be owned by you with mode 600 (or 400).")
        if info.st_size > 1024:
            raise ClientError("Assignment token file is invalid.")
        token = os.read(descriptor, 1025).decode("ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]{32,512}=*", token):
            raise ClientError("Assignment token file is invalid.")
        return token
    except (OSError, UnicodeError):
        raise ClientError("Cannot read a private assignment token file; run the local initializer.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def stable_request_id(
    target_id: str, code: str, guidance: str | None = None,
    functional_test_feedback: list[str] | None = None,
) -> str:
    content = {"target_id": target_id, "code": code, "guidance": guidance or ""}
    if functional_test_feedback:
        content["functional_test_feedback"] = functional_test_feedback
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "local-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_http_error(status: int) -> ClientError:
    messages = {
        400: "Backend rejected the request.",
        401: "Assignment token is missing, invalid, expired, or revoked.",
        403: "This assignment token cannot access the requested target.",
        404: "Backend endpoint or target was not found.",
        409: "Request ID conflicts with existing content or has an unfinished/failed attempt.",
        413: "Request exceeds the backend input limit.",
        422: "Request parameters do not meet the backend limits.",
        429: "Assignment quota or backend concurrency limit reached.",
        502: "Backend could not obtain model feedback; the reserved call remains consumed.",
        503: "Backend is unavailable or busy.",
        504: "Model request timed out; the reserved call remains consumed.",
    }
    return ClientError(messages.get(status, "Backend request failed."))


def request_feedback(
    *,
    backend_url: str,
    token_file: str | Path,
    code: str,
    target_id: str = "example",
    guidance: str | None = None,
    functional_test_feedback: list[str] | None = None,
    request_id: str | None = None,
    timeout: float = 90,
) -> dict:
    """Make exactly one request, with no proxy, redirect, or upstream fallback."""
    origin = validate_backend_url(backend_url)
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ClientError("Client timeout must be between 0 and 300 seconds.")
    if not code.strip() or len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ClientError("Code must be nonempty and at most 64 KiB; backend limits may be lower.")
    token = read_assignment_token(token_file)
    identifier = request_id or stable_request_id(target_id, code, guidance, functional_test_feedback)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", identifier):
        raise ClientError("Request ID must contain 1–128 letters, digits, dots, underscores, colons, or hyphens.")
    payload = {"request_id": identifier, "target_id": target_id, "code": code, "guidance": guidance or ""}
    if functional_test_feedback:
        payload["functional_test_feedback"] = functional_test_feedback
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        origin + "/v1/feedback",
        data=data,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES or token.encode("ascii") in raw:
                raise ClientError("Backend returned an invalid response.")
    except HTTPError as error:
        error.close()
        raise _safe_http_error(error.code) from None
    except (TimeoutError, socket.timeout):
        raise ClientError("Backend request timed out. Reuse this request ID to check its saved state.") from None
    except (URLError, OSError, ValueError, HTTPException):
        raise ClientError("Cannot reach the backend. Check its address and running process.") from None
    try:
        result = json.loads(raw)
        if (
            not isinstance(result, dict)
            or result.get("request_id") != identifier
            or not isinstance(result.get("feedback_text"), str)
            or not isinstance(result.get("model_name"), str)
            or not isinstance(result.get("usage"), (dict, type(None)))
        ):
            raise ValueError
        usage = result.get("usage")
        if usage is not None:
            usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens") if key in usage}
            if any(value is not None and (type(value) is not int or value < 0) for value in usage.values()):
                raise ValueError
        # Do not expose unknown response fields, headers, or diagnostic payloads.
        return {"request_id": identifier, "feedback_text": result["feedback_text"],
                "model_name": result["model_name"], "usage": usage}
    except (UnicodeError, ValueError, TypeError):
        raise ClientError("Backend returned an invalid response.") from None


def _backend_from_config(path: Path) -> str:
    if not path.exists():
        return DEFAULT_BACKEND_URL
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or set(config) != {"backend_url"}:
            raise ValueError
        return validate_backend_url(config["backend_url"])
    except (OSError, UnicodeError, TypeError, ValueError):
        raise ClientError("Client config must contain only a valid backend_url.") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(".local-vertex/client/config.json"))
    parser.add_argument("--backend-url", help="HTTPS or HTTP loopback origin; overrides the non-secret config file")
    parser.add_argument("--token-file", type=Path, default=Path(".local-vertex/client/token"))
    parser.add_argument("--code-file", type=Path, default=Path(__file__).parent / "examples/vertex_feedback_example.py")
    parser.add_argument("--target-id", default="example")
    parser.add_argument("--guidance", help="Optional feedback preference, subject to backend limits")
    parser.add_argument("--request-id", help="Default: stable SHA-256 identifier of target, code, and guidance")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--json", action="store_true", help="Print the safe response fields as JSON")
    args = parser.parse_args(argv)
    try:
        if args.code_file.stat().st_size > MAX_CODE_BYTES:
            raise ClientError("Code file exceeds the 64 KiB client limit.")
        code = args.code_file.read_text(encoding="utf-8")
        result = request_feedback(
            backend_url=args.backend_url or _backend_from_config(args.config),
            token_file=args.token_file,
            code=code,
            target_id=args.target_id,
            guidance=args.guidance,
            request_id=args.request_id,
            timeout=args.timeout,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("Request:", result["request_id"])
            print("Model:", result["model_name"])
            print(result["feedback_text"])
            if result["usage"] is not None:
                print("Usage:", json.dumps(result["usage"], sort_keys=True))
        return 0
    except ClientError as error:
        print("Error:", str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Error: Cannot read the UTF-8 example code file.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

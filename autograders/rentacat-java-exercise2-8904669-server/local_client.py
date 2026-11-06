#!/usr/bin/env python3
"""HTTP feedback client. Requires only the Python standard library.

The assignment token file is the client's only credential. This module never
imports a Google SDK, discovers ADC, or falls back to a model endpoint.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
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
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_BACKEND_URL = "http://127.0.0.1:8001"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CODE_BYTES = 64 * 1024
MAX_FILES_BYTES = 256 * 1024


class ClientError(Exception):
    """An error with a deliberately safe, user-facing message."""


class ConfigChangedError(ClientError):
    """Inputs must be collected again using the current assignment revision."""

    code = "config_changed"


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
    target_id: str, code: str | None = None, guidance: str | None = None,
    functional_test_feedback: list[str] | None = None,
    *, files: list[dict] | None = None, evidence: list[dict] | None = None,
    namespace: str = "",
) -> str:
    content = {"target_id": target_id, "guidance": guidance or ""}
    if files is not None:
        content["files"] = files
    else:
        content["code"] = code
    if functional_test_feedback:
        content["functional_test_feedback"] = functional_test_feedback
    if evidence:
        content["evidence"] = evidence
    if namespace:
        content["namespace"] = namespace
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "local-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_http_error(status: int) -> ClientError:
    messages = {
        400: "Backend rejected the request.",
        401: "Assignment token is missing, invalid, expired, or revoked.",
        403: "This assignment token cannot access the requested target.",
        404: "Backend endpoint or target was not found.",
        409: "Assignment configuration changed, or this request ID conflicts with an existing attempt.",
        413: "Request exceeds the backend input limit.",
        422: "Request parameters do not meet the backend limits.",
        429: "Assignment quota or backend concurrency limit reached.",
        502: "Backend could not obtain model feedback; the reserved call remains consumed.",
        503: "Backend is unavailable or busy.",
        504: "Model request timed out; the reserved call remains consumed.",
    }
    return ClientError(messages.get(status, "Backend request failed."))


def get_assignment_config(
    *, backend_url: str, token_file: str | Path, timeout: float = 30,
) -> dict:
    """Read this token's current assignment configuration without reserving a call."""
    origin = validate_backend_url(backend_url)
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ClientError("Client timeout must be between 0 and 300 seconds.")
    token = read_assignment_token(token_file)
    request = Request(
        origin + "/v1/assignment/config",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirects()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if response.getcode() != 200 or len(raw) > MAX_RESPONSE_BYTES or token.encode("ascii") in raw:
                raise ClientError("Backend returned an invalid assignment configuration.")
    except HTTPError as error:
        error.close()
        raise _safe_http_error(error.code) from None
    except (TimeoutError, socket.timeout):
        raise ClientError("Assignment configuration request timed out.") from None
    except (URLError, OSError, ValueError, HTTPException):
        raise ClientError("Cannot reach the backend. Check its address and running process.") from None
    try:
        config = json.loads(raw)
        if (not isinstance(config, dict) or not isinstance(config.get("assignment_id"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", config.get("revision", ""))
                or not isinstance(config.get("targets"), dict)):
            raise ValueError
        for target in config["targets"].values():
            if (not isinstance(target, dict) or not isinstance(target.get("identifier"), str)
                    or not isinstance(target.get("analysis"), dict)):
                raise ValueError
        return config
    except (UnicodeError, ValueError, TypeError):
        raise ClientError("Backend returned an invalid assignment configuration.") from None


def request_feedback(
    *,
    backend_url: str,
    token_file: str | Path,
    code: str | None = None,
    target_id: str = "example",
    guidance: str | None = None,
    functional_test_feedback: list[str] | None = None,
    files: list[dict] | None = None,
    evidence: list[dict] | None = None,
    request_id: str | None = None,
    platform_submission_id: str | None = None,
    submitted_at: str | None = None,
    student_identifier: str | None = None,
    config_revision: str | None = None,
    timeout: float = 270,
) -> dict:
    """Submit and poll one request, with no proxy, redirect, or upstream fallback."""
    origin = validate_backend_url(backend_url)
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ClientError("Client timeout must be between 0 and 300 seconds.")
    if files is None:
        if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise ClientError("Code must be nonempty and at most 64 KiB; backend limits may be lower.")
    else:
        if code is not None or not isinstance(files, list) or not files:
            raise ClientError("Supply either code or a nonempty files list.")
        if any(not isinstance(item, dict) or set(item) != {"path", "content"}
               or not isinstance(item["path"], str) or not item["path"]
               or not isinstance(item["content"], str) for item in files):
            raise ClientError("Each file must contain a path and UTF-8 content.")
        if sum(len(item["content"].encode("utf-8")) for item in files) > MAX_FILES_BYTES:
            raise ClientError("Files exceed the 256 KiB client limit; backend limits may be lower.")
    token = read_assignment_token(token_file)
    identifier = request_id or stable_request_id(
        target_id, code, guidance, functional_test_feedback, files=files, evidence=evidence,
        namespace=config_revision or "",
    )
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", identifier):
        raise ClientError("Request ID must contain 1–128 letters, digits, dots, underscores, colons, or hyphens.")
    payload = {"request_id": identifier, "target_id": target_id, "guidance": guidance or ""}
    if config_revision is not None:
        payload["config_revision"] = config_revision
    if files is None:
        payload["code"] = code
    else:
        payload["files"] = files
    if functional_test_feedback:
        payload["functional_test_feedback"] = functional_test_feedback
    if evidence:
        payload["evidence"] = evidence
    if (platform_submission_id is None) != (submitted_at is None):
        raise ClientError("Submission context requires both platform_submission_id and submitted_at.")
    if platform_submission_id is not None:
        if not isinstance(platform_submission_id, str) or not platform_submission_id.strip():
            raise ClientError("Platform submission ID must be nonempty.")
        if not isinstance(submitted_at, str) or not submitted_at.strip():
            raise ClientError("Submission timestamp must be a nonempty ISO string.")
        if student_identifier is not None and not isinstance(student_identifier, str):
            raise ClientError("Student identifier must be a string or null.")
        payload["submission"] = {
            "platform_submission_id": platform_submission_id,
            "submitted_at": submitted_at,
            "student_identifier": student_identifier,
        }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        origin + "/v1/feedback",
        data=data,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Accept": "application/json", "Prefer": "respond-async"},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClientError("Backend request timed out. Reuse this request ID to check its saved state.")
        try:
            try:
                response = opener.open(request, timeout=min(remaining, 30))
            except HTTPError as error:
                if error.code == 409:
                    try:
                        raw_error = error.read(MAX_RESPONSE_BYTES + 1)
                        problem = json.loads(raw_error) if len(raw_error) <= MAX_RESPONSE_BYTES else None
                    except (ValueError, OSError):
                        problem = None
                    finally:
                        error.close()
                    if (isinstance(problem, dict) and problem.get("request_id") == identifier
                            and isinstance(problem.get("error"), dict)
                            and problem["error"].get("code") == "config_changed"):
                        raise ConfigChangedError("Assignment configuration changed; collect inputs again.") from None
                    raise _safe_http_error(409) from None
                if error.code != 429:
                    error.close()
                    raise _safe_http_error(error.code) from None
                response = error
            with response:
                status = response.getcode()
                retry_after = response.headers.get("Retry-After", "2")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES or token.encode("ascii") in raw:
                    raise ClientError("Backend returned an invalid response.")
        except (TimeoutError, socket.timeout):
            raise ClientError("Backend request timed out. Reuse this request ID to check its saved state.") from None
        except (URLError, OSError, ValueError, HTTPException):
            raise ClientError("Cannot reach the backend. Check its address and running process.") from None
        if status == 200:
            break
        try:
            pending = json.loads(raw)
        except (UnicodeError, ValueError, TypeError):
            pending = None
        if status == 429:
            if (not isinstance(pending, dict) or pending.get("request_id") != identifier
                    or not isinstance(pending.get("error"), dict)
                    or pending["error"].get("code") != "busy"):
                raise _safe_http_error(429)
        elif (status != 202 or not isinstance(pending, dict)
                or pending.get("request_id") != identifier or pending.get("status") != "processing"):
            raise ClientError("Backend returned an invalid response.")
        try:
            delay = float(retry_after)
            if not math.isfinite(delay):
                delay = 2
        except (TypeError, ValueError):
            delay = 2
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClientError("Backend request timed out. Reuse this request ID to check its saved state.")
        time.sleep(min(max(1, min(delay, 5)), remaining))
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
        engine = result.get("engine", "vertex")
        provider = result.get("provider", "GOOGLE")
        requested_model = result.get("requested_model", result["model_name"])
        if (
            engine not in {"vertex", "pitt", "gemini_api"}
            or provider not in {"GOOGLE", "OPENAI", "ANTHROPIC"}
            or not isinstance(requested_model, str)
            or not requested_model.strip()
        ):
            raise ValueError
        usage = result.get("usage")
        if usage is not None:
            usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens") if key in usage}
            if any(value is not None and (type(value) is not int or value < 0) for value in usage.values()):
                raise ValueError
        items = result.get("items", [])
        if not isinstance(items, list):
            raise ValueError
        clean_items = []
        for item in items:
            if (not isinstance(item, dict)
                    or any(not isinstance(item.get(key), str)
                           for key in ("test_id", "verdict", "feedback", "evidence"))
                    or not item["test_id"].strip()
                    or item["verdict"] not in {"pass", "fail", "insufficient_evidence"}):
                raise ValueError
            clean_items.append({key: item[key] for key in ("test_id", "verdict", "feedback", "evidence")})
            for key in ("suggested_score", "max_score"):
                if key in item:
                    value = item[key]
                    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                        raise ValueError
                    clean_items[-1][key] = value
            score, maximum = item.get("suggested_score"), item.get("max_score")
            if score is not None and (maximum is None or score > maximum):
                raise ValueError
        if len({item["test_id"] for item in clean_items}) != len(clean_items):
            raise ValueError
        failures = result.get("failure_feedback", [])
        if not isinstance(failures, list):
            raise ValueError
        clean_failures = []
        for item in failures:
            if (not isinstance(item, dict) or set(item) != {"evidence_id", "feedback", "evidence"}
                    or any(not isinstance(item[key], str) or not item[key].strip() for key in item)):
                raise ValueError
            clean_failures.append(dict(item))
        if len({item["evidence_id"] for item in clean_failures}) != len(clean_failures):
            raise ValueError
        # Only explicitly supported fields cross the client boundary.
        safe = {"request_id": identifier, "feedback_text": result["feedback_text"],
                "engine": engine, "provider": provider, "requested_model": requested_model,
                "model_name": result["model_name"], "usage": usage}
        if "items" in result:
            safe["items"] = clean_items
        if "failure_feedback" in result:
            if clean_items or "max_score" in result:
                raise ValueError
            safe["failure_feedback"] = clean_failures
        if "behavior" in result:
            if result["behavior"] not in {"ai_grading", "functional_test_feedback"}:
                raise ValueError
            safe["behavior"] = result["behavior"]
        if "grading_status" in result:
            if result["grading_status"] not in {"complete", "pending_regrade"}:
                raise ValueError
            safe["grading_status"] = result["grading_status"]
        if "max_score" in result:
            maximum, score = result["max_score"], result.get("suggested_score")
            if (type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum < 0
                    or (score is not None and (type(score) not in (int, float)
                        or not math.isfinite(score) or not 0 <= score <= maximum))):
                raise ValueError
            safe.update(max_score=maximum, suggested_score=score)
        behavior = safe.get("behavior")
        if behavior == "functional_test_feedback":
            expected = {item["id"] for item in (evidence or [])
                        if item.get("kind") == "runtime" and item.get("status") == "fail"}
            if (not clean_failures or "items" in result or "suggested_score" in result
                    or "max_score" in result or safe.get("grading_status") != "complete"
                    or {item["evidence_id"] for item in clean_failures} != expected):
                raise ValueError
        elif behavior == "ai_grading":
            if not clean_items or "failure_feedback" in result or "max_score" not in safe:
                raise ValueError
            for item in clean_items:
                maximum = item.get("max_score")
                if maximum is None or maximum <= 0:
                    raise ValueError
                expected_score = (maximum if item["verdict"] == "pass" else
                                  0 if item["verdict"] == "fail" else None)
                if item.get("suggested_score") != expected_score:
                    raise ValueError
            pending = any(item["verdict"] == "insufficient_evidence" for item in clean_items)
            maximum = sum((Decimal(str(item["max_score"])) for item in clean_items), Decimal(0))
            total = None if pending else sum(
                (Decimal(str(item["suggested_score"])) for item in clean_items), Decimal(0))
            if (safe.get("grading_status") != ("pending_regrade" if pending else "complete")
                    or Decimal(str(safe["max_score"])) != maximum
                    or (safe["suggested_score"] is None) != pending
                    or (not pending and Decimal(str(safe["suggested_score"])) != total)):
                raise ValueError
        elif "failure_feedback" in result or "grading_status" in result:
            raise ValueError
        return safe
    except (UnicodeError, ValueError, TypeError):
        raise ClientError("Backend returned an invalid response.") from None


def complete_submission(
    *, backend_url: str, token_file: str | Path, platform_submission_id: str,
    submitted_at: str, ai_status: str, student_identifier: str | None = None, timeout: float = 30,
) -> dict:
    """Report the final AI outcome of this submission's current execution."""
    if ai_status not in {"SUCCEEDED", "FAILED"}:
        raise ClientError("AI outcome must be SUCCEEDED or FAILED.")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ClientError("Client timeout must be between 0 and 300 seconds.")
    origin = validate_backend_url(backend_url)
    token = read_assignment_token(token_file)
    payload = json.dumps({
        "submission": {
            "platform_submission_id": platform_submission_id,
            "submitted_at": submitted_at,
            "student_identifier": student_identifier,
        },
        "ai_status": ai_status,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(
        origin + "/v1/submissions/" + quote(platform_submission_id, safe="") + "/ai-complete",
        data=payload,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES or token.encode("ascii") in raw:
                raise ClientError("Backend returned an invalid response.")
    except HTTPError as error:
        status = error.code
        error.close()
        raise _safe_http_error(status) from None
    except (TimeoutError, socket.timeout):
        raise ClientError("Backend request timed out.") from None
    except (URLError, OSError, ValueError, HTTPException):
        raise ClientError("Cannot reach the backend. Check its address and running process.") from None
    try:
        result = json.loads(raw)
        if (not isinstance(result, dict) or result.get("submission_id") != platform_submission_id
                or result.get("status") != "completed" or result.get("ai_status") != ai_status):
            raise ValueError
        return result
    except (UnicodeError, ValueError, TypeError):
        raise ClientError("Backend returned an invalid completion response.") from None


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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--code-file", type=Path, help="UTF-8 source file to review (legacy input)")
    source.add_argument("--files-json", type=Path, help="JSON array of {path, content} source files")
    parser.add_argument("--evidence-json", type=Path, help="JSON array of runtime or coverage evidence")
    parser.add_argument("--target-id", default="example")
    parser.add_argument("--guidance", help="Optional feedback preference, subject to backend limits")
    parser.add_argument("--request-id", help="Explicit attempt ID; requires --config-revision. Reuse both unchanged for retries")
    parser.add_argument("--config-revision", help="Revision used to prepare inputs; required with --request-id, otherwise fetched before reading files")
    parser.add_argument("--timeout", type=float, default=270)
    parser.add_argument("--json", action="store_true", help="Print the safe response fields as JSON")
    args = parser.parse_args(argv)
    if args.request_id is not None and args.config_revision is None:
        parser.error("--request-id requires the original --config-revision; reuse the pair printed before the first call")
    if args.config_revision is not None and not re.fullmatch(r"[0-9a-f]{64}", args.config_revision):
        parser.error("--config-revision must be the 64-character revision returned by the backend")
    if args.request_id is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", args.request_id):
        parser.error("--request-id must contain 1-128 letters, digits, dots, underscores, colons, or hyphens")
    try:
        backend_url = args.backend_url or _backend_from_config(args.config)
        revision = args.config_revision
        if revision is None:
            manifest = get_assignment_config(backend_url=backend_url, token_file=args.token_file)
            if args.target_id not in manifest["targets"]:
                raise ClientError("Target is not available in this assignment configuration.")
            revision = manifest["revision"]
        code = None
        files = None
        if args.code_file:
            if args.code_file.stat().st_size > MAX_CODE_BYTES:
                raise ClientError("Code file exceeds the 64 KiB client limit.")
            code = args.code_file.read_text(encoding="utf-8")
        else:
            files = json.loads(args.files_json.read_text(encoding="utf-8"))
        evidence = json.loads(args.evidence_json.read_text(encoding="utf-8")) if args.evidence_json else None
        identifier = args.request_id or stable_request_id(
            args.target_id, code, args.guidance, files=files, evidence=evidence, namespace=revision,
        )
        print(f"Retry with --request-id {identifier} --config-revision {revision}", file=sys.stderr, flush=True)
        result = request_feedback(
            backend_url=backend_url,
            token_file=args.token_file,
            code=code,
            files=files,
            evidence=evidence,
            target_id=args.target_id,
            guidance=args.guidance,
            request_id=identifier,
            config_revision=revision,
            timeout=args.timeout,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("Request:", result["request_id"])
            print("Engine:", result["engine"])
            print("Requested model:", result["requested_model"])
            print("Model:", result["model_name"])
            print(result["feedback_text"])
            for item in result.get("items", []):
                score = item.get("suggested_score")
                label = "Pending regrade" if score is None else str(score)
                print(f"{item['test_id']}: {label} / {item.get('max_score')}")
                if item["feedback"]:
                    print(item["feedback"])
            for item in result.get("failure_feedback", []):
                print(f"{item['evidence_id']}: {item['feedback']}")
            if result["usage"] is not None:
                print("Usage:", json.dumps(result["usage"], sort_keys=True))
        return 0
    except ClientError as error:
        print("Error:", str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError):
        print("Error: Cannot read the UTF-8 source or JSON input file.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

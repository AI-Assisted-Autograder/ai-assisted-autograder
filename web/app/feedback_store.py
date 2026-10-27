"""Single-worker JSON storage with durable quota reservations and idempotency.

All records remain backend-owned. Once a reservation is persisted, it consumes
one call even if the upstream fails, times out, or the process is interrupted.
The lifetime file lock deliberately rejects a second worker. It complements the
in-process mutex; it is not a database or a multi-host coordination mechanism.
"""

import fcntl
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

from .feedback_config import AssignmentAccess, BackendConfig, RequestRecord, State, TokenRecord


class StoreError(Exception):
    """Safe errors only: underlying filesystem/configuration details stay private."""

    def __init__(self, code: str, status_code: int = 503):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def secure_parent(path: Path) -> None:
    """Create missing private directories without changing unrelated parents."""
    missing: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise


def atomic_write(path: Path, content: bytes, *, overwrite: bool = True) -> None:
    """Write mode 0600, fsync the file, then atomically install and fsync parent."""
    secure_parent(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # link is atomic and refuses to clobber an existing destination.
            os.link(temporary, path)
            os.unlink(temporary)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class JsonStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self._mutex = threading.RLock()
        self._lock_fd: int | None = None
        self._poisoned = False
        self._state: State
        try:
            # State must already exist; initialization is an explicit separate step.
            if not self.path.is_file():
                raise StoreError("state_unavailable")
            lock_path = self.path.with_name(self.path.name + ".lock")
            self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(self._lock_fd, 0o600)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreError("worker_already_running") from None
            raw = self.path.read_bytes()
            data = json.loads(raw)
            changed = data.get("schema_version") == 2
            if changed:
                data["schema_version"] = 3
                data["config"]["allowed_models"] = {"vertex": data["config"]["allowed_models"]}
                raw = json.dumps(data)
            self._state = State.model_validate_json(raw)
            for requests in self._state.requests.values():
                for record in requests.values():
                    if record.status == "processing":
                        record.status = "interrupted"
                        record.error_code = "interrupted"
                        record.updated_at = utc_now()
                        changed = True
            if changed:
                self._persist(self._state)
        except StoreError:
            self.close()
            raise
        except Exception:
            self.close()
            raise StoreError("state_unavailable") from None

    def close(self) -> None:
        with self._mutex:
            if self._lock_fd is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._lock_fd = None

    def _ensure_open(self) -> None:
        if self._lock_fd is None or self._poisoned:
            raise StoreError("state_unavailable")

    @property
    def config(self) -> BackendConfig:
        with self._mutex:
            self._ensure_open()
            return self._state.config.model_copy(deep=True)

    def authenticate(self, raw_token: str) -> str:
        with self._mutex:
            self._ensure_open()
            if not isinstance(raw_token, str) or not 16 <= len(raw_token) <= 256:
                raise StoreError("unauthorized", 401)
            token = self._state.tokens.get(token_hash(raw_token))
            if (token is None or not token.enabled or token.revoked
                    or token.expires_at <= utc_now()):
                raise StoreError("unauthorized", 401)
            assignment = self._state.assignments.get(token.assignment_id)
            if assignment is None or not assignment.enabled:
                raise StoreError("assignment_forbidden", 403)
            return token.assignment_id

    def get_assignment(self, assignment_id: str) -> AssignmentAccess:
        with self._mutex:
            self._ensure_open()
            assignment = self._state.assignments.get(assignment_id)
            if assignment is None or not assignment.enabled:
                raise StoreError("assignment_forbidden", 403)
            return assignment.model_copy(deep=True)

    def add_assignment(self, assignment_id: str, access: AssignmentAccess,
                       digest: str, token: TokenRecord) -> None:
        """Append access records while holding the same lock as the Web worker."""
        with self._mutex:
            self._ensure_open()
            if assignment_id in self._state.assignments or digest in self._state.tokens:
                raise ValueError("assignment or token already exists")
            if token.assignment_id != assignment_id:
                raise ValueError("token assignment must match")
            candidate = self._state.model_copy(deep=True)
            candidate.assignments[assignment_id] = access.model_copy(deep=True)
            candidate.tokens[digest] = token.model_copy(deep=True)
            self._persist(candidate)
            self._state = candidate

    def get_request(self, assignment_id: str, request_id: str) -> RequestRecord | None:
        with self._mutex:
            self._ensure_open()
            record = self._state.requests.get(assignment_id, {}).get(request_id)
            return record.model_copy(deep=True) if record is not None else None

    def _persist(self, state: State) -> None:
        try:
            content = json.dumps(state.model_dump(mode="json"), ensure_ascii=False,
                                 allow_nan=False, indent=2).encode("utf-8") + b"\n"
            State.model_validate_json(content)
            atomic_write(self.path, content)
        except Exception:
            # A failure after rename but before directory fsync has an uncertain
            # outcome. Never continue from potentially stale in-memory counters.
            self._poisoned = True
            raise StoreError("state_unavailable") from None

    def reserve(self, assignment_id: str, request_id: str, digest: str,
                target_id: str, model: str, *, engine: str = "vertex",
                provider: str = "GOOGLE") -> tuple[RequestRecord, bool]:
        with self._mutex:
            self._ensure_open()
            assignment = self.get_assignment(assignment_id)
            if model not in self._state.config.allowed_models.get(engine, []):
                raise StoreError("assignment_config_unavailable")
            existing = self._state.requests.get(assignment_id, {}).get(request_id)
            if existing is not None:
                if existing.digest != digest:
                    raise StoreError("request_conflict", 409)
                return existing.model_copy(deep=True), False
            if assignment.used_calls >= assignment.max_calls:
                raise StoreError("quota_exceeded", 429)
            now = utc_now()
            record = RequestRecord(request_id=request_id, target_id=target_id,
                                   engine=engine, provider=provider, model=model, digest=digest,
                                   status="processing", created_at=now, updated_at=now)
            candidate = self._state.model_copy(deep=True)
            candidate.assignments[assignment_id].used_calls += 1
            candidate.requests.setdefault(assignment_id, {})[request_id] = record
            # Never return authorization to call upstream before this durable write.
            self._persist(candidate)
            self._state = candidate
            return record.model_copy(deep=True), True

    def _finish(self, assignment_id: str, request_id: str, *,
                result: dict[str, Any] | None = None,
                code: str | None = None) -> RequestRecord:
        with self._mutex:
            self._ensure_open()
            existing = self._state.requests.get(assignment_id, {}).get(request_id)
            if existing is None:
                raise StoreError("request_unknown", 404)
            if existing.status != "processing":
                return existing.model_copy(deep=True)
            candidate = self._state.model_copy(deep=True)
            record = candidate.requests[assignment_id][request_id]
            record.status = "completed" if code is None else "failed"
            record.result = deepcopy(result)
            record.error_code = code
            record.updated_at = utc_now()
            self._persist(candidate)
            self._state = candidate
            return record.model_copy(deep=True)

    def complete(self, assignment_id: str, request_id: str,
                 result: dict[str, Any]) -> RequestRecord:
        return self._finish(assignment_id, request_id, result=result)

    def fail(self, assignment_id: str, request_id: str,
             code: str = "model_error") -> RequestRecord:
        if code not in ("model_error", "upstream_timeout", "backend_auth_unavailable"):
            raise StoreError("invalid_error_code", 500)
        return self._finish(assignment_id, request_id, code=code)

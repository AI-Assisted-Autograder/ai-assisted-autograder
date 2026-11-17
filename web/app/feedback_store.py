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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ai_grading.models import TokenUsage
from ai_grading.pricing import ModelPrice, RequestAccounting, default_model_prices, estimate_cost, select_price
from .feedback_config import AssignmentAccess, BackendConfig, RequestRecord, State, TokenRecord


COST_PERIODS = {
    "24h": "Last 24h",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "all": "All time",
}


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
            if self._state.cost_tracking_started_at is None:
                self._state.cost_tracking_started_at = utc_now()
                changed = True
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

    def allow_pitt_models(self) -> None:
        """Enable Pitt choices, fill missing prices, and correct the original Argon schedule."""
        from .pitt_settings import PITT_MODELS

        with self._mutex:
            self._ensure_open()
            allowed = self._state.config.allowed_models.get("pitt", [])
            missing = [model for model in PITT_MODELS if model not in allowed]
            prices = self._state.config.model_prices
            argon = "@pitt-ai-connect-google-vertex/gemini-4-argon"
            original_argon = [
                ModelPrice(engine="pitt", model=argon, response_model="gemini-4-argon",
                           effective_from=day, input_per_million=Decimal(input_rate),
                           cached_input_per_million=Decimal(cached_rate), output_per_million=Decimal(output_rate),
                           basis="Google Cloud Standard / Global; public rates including applicable credits")
                for day, input_rate, cached_rate, output_rate in (
                    (date(2026, 10, 13), "2", "0.10", "10"),
                    (date(2026, 12, 1), "4", "0.20", "20"),
                )
            ]
            configured_argon = [price for price in prices if price.engine == "pitt" and price.model == argon]
            # Replace only the untouched built-in promotion schedule, never instructor pricing.
            if sorted(configured_argon, key=lambda price: price.effective_from) == original_argon:
                prices = [price for price in prices if price.engine != "pitt" or price.model != argon]
            priced = {(price.engine, price.model) for price in prices}
            missing_prices = [price for price in default_model_prices()
                              if price.engine == "pitt" and price.model in PITT_MODELS
                              and (price.engine, price.model) not in priced]
            if not missing and not missing_prices:
                return
            candidate = self._state.model_copy(deep=True)
            candidate.config.allowed_models["pitt"] = [*allowed, *missing]
            candidate.config.model_prices = [*prices, *missing_prices]
            candidate.config = BackendConfig.model_validate(candidate.config.model_dump())
            self._persist(candidate)
            self._state = candidate

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

    def record_usage(self, assignment_id: str, request_id: str, usage: TokenUsage,
                     model_name: str) -> RequestAccounting:
        """Keep usage even when grading fails or the HTTP wait has already timed out."""
        with self._mutex:
            self._ensure_open()
            existing = self._state.requests[assignment_id][request_id]
            if existing.accounting is not None:
                return existing.accounting.model_copy(deep=True)
            accounting = estimate_cost(usage, model_name, existing.pricing)
            candidate = self._state.model_copy(deep=True)
            candidate.requests[assignment_id][request_id].accounting = accounting
            self._persist(candidate)
            self._state = candidate
            return accounting.model_copy(deep=True)

    def cost_summary(self, assignment_id: str, *, period: str | None = None) -> dict[str, Any] | None:
        """Summarize recorded costs, optionally grouped by request start time in UTC."""
        if period is not None and period not in COST_PERIODS:
            raise ValueError("invalid cost period")
        with self._mutex:
            self._ensure_open()
            if assignment_id not in self._state.assignments:
                return None
            records = self._state.requests.get(assignment_id, {}).values()
            tracking_since = self._state.cost_tracking_started_at
            if tracking_since is not None:
                if tracking_since.tzinfo is None:
                    tracking_since = tracking_since.replace(tzinfo=timezone.utc)
                tracking_since = tracking_since.astimezone(timezone.utc)
            buckets: dict[datetime, dict[str, Any]] = {}
            if period is not None:
                end = utc_now()
                if period == "all":
                    earliest_request = min((record.created_at for record in records),
                                           default=tracking_since or end)
                    start = min(end, earliest_request).astimezone(timezone.utc)
                    span = end - start
                    granularity = "day" if span <= timedelta(days=31) else (
                        "week" if span <= timedelta(days=180) else "month")
                else:
                    start = end - {"24h": timedelta(hours=24), "7d": timedelta(days=7),
                                   "30d": timedelta(days=30)}[period]
                    granularity = "hour" if period == "24h" else "day"

                def bucket_start(value: datetime) -> datetime:
                    value = value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                    if granularity == "hour":
                        return value
                    value = value.replace(hour=0)
                    if granularity == "week":
                        return value - timedelta(days=value.weekday())
                    return value.replace(day=1) if granularity == "month" else value

                cursor = bucket_start(start)
                while start < end and cursor < end:
                    if granularity == "month":
                        next_start = cursor.replace(year=cursor.year + (cursor.month == 12),
                                                    month=cursor.month % 12 + 1)
                    else:
                        next_start = cursor + {"hour": timedelta(hours=1), "day": timedelta(days=1),
                                               "week": timedelta(days=7)}[granularity]
                    buckets[cursor] = {
                        "start": max(cursor, start), "end": min(next_start, end),
                        "estimated_cost_usd": Decimal(0), "priced_requests": 0,
                        "unpriced_requests": 0, "pending_requests": 0,
                    }
                    cursor = next_start

            total = Decimal(0)
            counts = {"priced_requests": 0, "unpriced_requests": 0, "pending_requests": 0}
            tokens = {key: 0 for key in ("input_tokens", "output_tokens", "cached_input_tokens")}
            for record in records:
                if period is not None and not start <= record.created_at < end:
                    continue
                accounting = record.accounting
                if accounting is not None:
                    for key in tokens:
                        count = getattr(accounting.usage, key)
                        if count is not None:
                            tokens[key] += count
                amount = Decimal(0)
                if accounting is not None and accounting.estimated_cost_usd is not None:
                    amount = accounting.estimated_cost_usd
                    status = "priced_requests"
                elif record.status == "processing":
                    status = "pending_requests"
                else:
                    status = "unpriced_requests"
                total += amount
                counts[status] += 1
                if period is not None:
                    bucket = buckets[bucket_start(record.created_at)]
                    bucket["estimated_cost_usd"] += amount
                    bucket[status] += 1
            summary = {"estimated_cost_usd": str(total), **tokens, **counts,
                       "tracking_since": tracking_since}
            if period is not None:
                maximum = max((bucket["estimated_cost_usd"] for bucket in buckets.values()), default=Decimal(0))
                for bucket in buckets.values():
                    bucket["estimated_cost_usd"] = str(bucket["estimated_cost_usd"])
                summary["chart"] = {"start": start, "end": end, "granularity": granularity,
                                    "max_cost_usd": str(maximum), "buckets": list(buckets.values())}
            return summary

    def cost_summaries(self) -> dict[str, dict[str, Any]]:
        with self._mutex:
            self._ensure_open()
            return {key: self.cost_summary(key) for key in self._state.assignments}

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
                provider: str = "GOOGLE", run_id: str | None = None) -> tuple[RequestRecord, bool]:
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
                                   status="processing", created_at=now, updated_at=now, run_id=run_id,
                                   pricing=select_price(self._state.config.model_prices, engine, model, now.date()))
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

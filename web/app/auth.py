"""Instructor sign-in for the UI (OQ-9).

Instructors sign in with an email and password; accounts are created from
the command line (app.manage_instructors), since there is no sign-up page.
A successful sign-in stores the instructor's id in Starlette's signed
session cookie. Every UI route depends on require_instructor, which looks
the account up again on each request, so deactivating an account or
changing its password signs it out everywhere at once.

The /internal and /v1 APIs are unaffected: they keep their own tokens.
"""

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Instructor

MIN_PASSWORD_LENGTH = 12

# ---------------------------------------------------------------------
# Passwords: scrypt from the standard library, so no new dependency.
# Stored as scrypt$n$r$p$salt$hash so the cost can be raised later
# without breaking existing hashes.
# ---------------------------------------------------------------------

_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        expected_bytes = base64.b64decode(expected)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(expected_bytes),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected_bytes)


# Checked against when the email is unknown, so a wrong email takes as long
# as a wrong password and the response time doesn't reveal which accounts exist.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_new_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError(f"{email!r} is not an email address")
    return email


def password_fingerprint(password_hash: str) -> str:
    """Kept in the session so that changing the password ends old sessions."""
    return hashlib.sha256(password_hash.encode("ascii")).hexdigest()[:16]


# ---------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class InstructorAccount:
    id: uuid.UUID
    email: str
    display_name: str | None
    password_hash: str
    is_active: bool
    last_login_at: datetime | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.email


class InstructorStore(ABC):
    @abstractmethod
    def get(self, instructor_id: uuid.UUID) -> InstructorAccount | None: ...

    @abstractmethod
    def find_by_email(self, email: str) -> InstructorAccount | None: ...

    @abstractmethod
    def list_all(self) -> list[InstructorAccount]: ...

    @abstractmethod
    def add(self, email: str, password: str, display_name: str | None = None) -> uuid.UUID:
        """Raises ValueError for a bad email or password or a taken email."""

    @abstractmethod
    def set_password(self, email: str, password: str) -> None: ...

    @abstractmethod
    def set_active(self, email: str, active: bool) -> None: ...

    @abstractmethod
    def record_login(self, instructor_id: uuid.UUID) -> None: ...


def _account(row: Instructor) -> InstructorAccount:
    return InstructorAccount(
        id=row.id, email=row.email, display_name=row.display_name,
        password_hash=row.password_hash, is_active=row.is_active,
        last_login_at=row.last_login_at,
    )


class SqlInstructorStore(InstructorStore):
    def __init__(self, sessions):
        self._sessions = sessions

    def get(self, instructor_id):
        with self._sessions() as s:
            row = s.get(Instructor, instructor_id)
            return None if row is None else _account(row)

    def find_by_email(self, email):
        with self._sessions() as s:
            row = s.scalar(select(Instructor).where(Instructor.email == email.strip().lower()))
            return None if row is None else _account(row)

    def list_all(self):
        with self._sessions() as s:
            return [_account(r) for r in s.scalars(select(Instructor).order_by(Instructor.email))]

    def add(self, email, password, display_name=None):
        row = Instructor(
            email=normalize_email(email),
            display_name=(display_name or "").strip() or None,
            password_hash=hash_password(check_new_password(password)),
        )
        try:
            with self._sessions.begin() as s:
                s.add(row)
                s.flush()
                return row.id
        except IntegrityError:
            raise ValueError(f"an instructor with email {row.email} already exists") from None

    def _update(self, email, **values):
        with self._sessions.begin() as s:
            row = s.scalar(select(Instructor).where(Instructor.email == email.strip().lower()))
            if row is None:
                raise ValueError(f"no instructor with email {email}")
            for key, value in values.items():
                setattr(row, key, value)

    def set_password(self, email, password):
        self._update(email, password_hash=hash_password(check_new_password(password)))

    def set_active(self, email, active):
        self._update(email, is_active=active)

    def record_login(self, instructor_id):
        with self._sessions.begin() as s:
            row = s.get(Instructor, instructor_id)
            if row is not None:
                row.last_login_at = datetime.now(timezone.utc)


class InMemoryInstructorStore(InstructorStore):
    def __init__(self):
        self._accounts: dict[uuid.UUID, InstructorAccount] = {}

    def get(self, instructor_id):
        return self._accounts.get(instructor_id)

    def find_by_email(self, email):
        email = email.strip().lower()
        return next((a for a in self._accounts.values() if a.email == email), None)

    def list_all(self):
        return sorted(self._accounts.values(), key=lambda a: a.email)

    def add(self, email, password, display_name=None):
        email = normalize_email(email)
        if self.find_by_email(email):
            raise ValueError(f"an instructor with email {email} already exists")
        account = InstructorAccount(
            id=uuid.uuid4(), email=email, display_name=(display_name or "").strip() or None,
            password_hash=hash_password(check_new_password(password)), is_active=True,
        )
        self._accounts[account.id] = account
        return account.id

    def _update(self, email, **values):
        account = self.find_by_email(email)
        if account is None:
            raise ValueError(f"no instructor with email {email}")
        self._accounts[account.id] = replace(account, **values)

    def set_password(self, email, password):
        self._update(email, password_hash=hash_password(check_new_password(password)))

    def set_active(self, email, active):
        self._update(email, is_active=active)

    def record_login(self, instructor_id):
        if instructor_id in self._accounts:
            self._accounts[instructor_id] = replace(
                self._accounts[instructor_id], last_login_at=datetime.now(timezone.utc))


_store = SqlInstructorStore(SessionLocal)


def get_instructor_store() -> InstructorStore:
    """Overridden in tests with an InMemoryInstructorStore."""
    return _store


# ---------------------------------------------------------------------
# Failed sign-in throttle, per client address. In memory, which is right
# for the single uvicorn worker the Dockerfile runs.
# ---------------------------------------------------------------------

MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 15 * 60


class LoginThrottle:
    def __init__(self, max_failures=MAX_FAILURES, window=FAILURE_WINDOW_SECONDS, clock=time.monotonic):
        self._max, self._window, self._clock = max_failures, window, clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str) -> deque[float]:
        failures, cutoff = self._failures[key], self._clock() - self._window
        while failures and failures[0] < cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return deque()
        return failures

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self._max

    def fail(self, key: str) -> None:
        self._recent(key)
        self._failures[key].append(self._clock())

    def succeed(self, key: str) -> None:
        self._failures.pop(key, None)


throttle = LoginThrottle()


# ---------------------------------------------------------------------
# Sessions and the route guard
# ---------------------------------------------------------------------

SESSION_ID = "instructor_id"
SESSION_PASSWORD = "password_fingerprint"


def safe_next(value: str | None) -> str:
    """Only same-site paths, so ?next= can't bounce someone to another site."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def signed_in(request: Request, store: InstructorStore) -> InstructorAccount | None:
    raw_id = request.session.get(SESSION_ID)
    if not raw_id:
        return None
    try:
        account = store.get(uuid.UUID(raw_id))
    except ValueError:
        account = None
    if (
        account is None
        or not account.is_active
        or request.session.get(SESSION_PASSWORD) != password_fingerprint(account.password_hash)
    ):
        request.session.clear()
        return None
    return account


def login_url(request: Request) -> str:
    # After a POST the form is gone, so there is nothing useful to return to.
    if request.method != "GET":
        return "/login"
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return "/login" if target == "/" else "/login?" + urlencode({"next": target})


def require_instructor(
    request: Request, store: InstructorStore = Depends(get_instructor_store),
) -> InstructorAccount:
    account = signed_in(request, store)
    if account is None:
        location = login_url(request)
        headers = {"Location": location}
        if request.headers.get("HX-Request"):
            headers["HX-Redirect"] = location
        raise HTTPException(status_code=303, detail="Sign in required", headers=headers)
    request.state.instructor = account
    return account


# ---------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

BAD_CREDENTIALS = "Incorrect email or password."
TOO_MANY = "Too many failed sign-ins. Wait a few minutes and try again."


def _login_page(request: Request, *, next_url: str, email: str = "", error: str | None = None,
                status_code: int = 200):
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next_url, "email": email, "error": error},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/login")
def login_form(
    request: Request,
    next: str | None = None,
    store: InstructorStore = Depends(get_instructor_store),
):
    next_url = safe_next(next)
    if signed_in(request, store):
        return RedirectResponse(next_url, status_code=303)
    return _login_page(request, next_url=next_url)


@router.post("/login")
def login(
    request: Request,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
    store: InstructorStore = Depends(get_instructor_store),
):
    next_url = safe_next(next)
    client = request.client.host if request.client else "unknown"
    if throttle.blocked(client):
        return _login_page(request, next_url=next_url, email=email, error=TOO_MANY, status_code=429)

    account = store.find_by_email(email)
    password_ok = verify_password(password, account.password_hash if account else _DUMMY_HASH)
    if account is None or not password_ok or not account.is_active:
        throttle.fail(client)
        return _login_page(request, next_url=next_url, email=email, error=BAD_CREDENTIALS, status_code=401)

    throttle.succeed(client)
    request.session.clear()
    request.session[SESSION_ID] = str(account.id)
    request.session[SESSION_PASSWORD] = password_fingerprint(account.password_hash)
    store.record_login(account.id)
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

"""Instructor sign-in: passwords, sessions, the route guard and the CLI."""

import io

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.auth import (
    InMemoryInstructorStore,
    LoginThrottle,
    get_instructor_store,
    hash_password,
    safe_next,
    verify_password,
)
from app.main import app
from app.manage_instructors import main as manage
from app.repository import InMemoryRepository
from app.ui import get_repository

PASSWORD = "correct horse battery"


@pytest.fixture
def store():
    store = InMemoryInstructorStore()
    store.add("Ada@Example.edu", PASSWORD, "Ada Lovelace")
    app.dependency_overrides[get_instructor_store] = lambda: store
    app.dependency_overrides[get_repository] = lambda: InMemoryRepository()
    yield store
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    monkeypatch.setattr(auth, "throttle", LoginThrottle())


@pytest.fixture
def client(store):
    return TestClient(app, follow_redirects=False)


def sign_in(client, email="ada@example.edu", password=PASSWORD, next="/"):
    return client.post("/login", data={"email": email, "password": password, "next": next})


# --- passwords -------------------------------------------------------

def test_password_round_trip():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$")
    assert PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong password!", stored)


def test_same_password_hashes_differently():
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


@pytest.mark.parametrize("stored", ["", "plain", "md5$abc", "scrypt$x$8$1$AA==$AA=="])
def test_malformed_hash_never_verifies(stored):
    assert not verify_password(PASSWORD, stored)


def test_store_rejects_short_password_and_duplicate_email(store):
    with pytest.raises(ValueError, match="at least"):
        store.add("bob@example.edu", "short")
    with pytest.raises(ValueError, match="already exists"):
        store.add("ADA@example.edu", PASSWORD)


# --- redirects -------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("/assignments/1", "/assignments/1"),
    ("/a?view=targets", "/a?view=targets"),
    (None, "/"),
    ("", "/"),
    ("https://evil.example", "/"),
    ("//evil.example", "/"),
    ("/\\evil.example", "/"),
])
def test_safe_next(value, expected):
    assert safe_next(value) == expected


# --- the guard -------------------------------------------------------

def test_pages_redirect_to_login_when_signed_out(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    response = client.get("/assignments/new?x=1")
    assert response.headers["location"] == "/login?next=%2Fassignments%2Fnew%3Fx%3D1"


def test_form_posts_are_refused_when_signed_out(client):
    response = client.post("/assignments", data={"course_name": "CS 1"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_htmx_requests_get_hx_redirect(client):
    response = client.get("/", headers={"HX-Request": "true"})
    assert response.headers["hx-redirect"] == "/login"


def test_health_and_static_stay_public(client):
    assert client.get("/static/style.css").status_code == 200


# --- signing in and out ----------------------------------------------

def test_login_page_renders(client):
    response = client.get("/login?next=/assignments/new")
    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert 'value="/assignments/new"' in response.text
    assert "Sign out" not in response.text


def test_sign_in_then_see_pages(client, store):
    response = sign_in(client, email="  ADA@example.edu ", next="/assignments/new")
    assert response.status_code == 303
    assert response.headers["location"] == "/assignments/new"
    assert "autograder_session" in response.cookies

    page = client.get("/")
    assert page.status_code == 200
    assert "Ada Lovelace" in page.text
    assert "Sign out" in page.text
    assert store.find_by_email("ada@example.edu").last_login_at is not None


def test_sign_in_ignores_offsite_next(client):
    assert sign_in(client, next="https://evil.example").headers["location"] == "/"


@pytest.mark.parametrize("email, password", [
    ("ada@example.edu", "wrong password!!"),
    ("nobody@example.edu", PASSWORD),
])
def test_bad_credentials_get_one_message(client, email, password):
    response = sign_in(client, email=email, password=password)
    assert response.status_code == 401
    assert auth.BAD_CREDENTIALS in response.text
    assert f'value="{email}"' in response.text
    assert client.get("/").status_code == 303


def test_deactivated_account_cannot_sign_in(client, store):
    store.set_active("ada@example.edu", False)
    assert sign_in(client).status_code == 401


def test_signed_in_user_visiting_login_is_sent_on(client):
    sign_in(client)
    response = client.get("/login?next=/assignments/new")
    assert response.status_code == 303
    assert response.headers["location"] == "/assignments/new"


def test_sign_out(client):
    sign_in(client)
    response = client.post("/logout")
    assert response.headers["location"] == "/login"
    assert client.get("/").status_code == 303


def test_deactivating_ends_existing_sessions(client, store):
    sign_in(client)
    store.set_active("ada@example.edu", False)
    assert client.get("/").status_code == 303


def test_password_change_ends_existing_sessions(client, store):
    sign_in(client)
    store.set_password("ada@example.edu", "a brand new password")
    assert client.get("/").status_code == 303


def test_tampered_cookie_is_ignored(client):
    client.cookies.set("autograder_session", "not-a-signed-value")
    assert client.get("/").status_code == 303


def test_repeated_failures_are_throttled(client, monkeypatch):
    monkeypatch.setattr(auth, "throttle", LoginThrottle(max_failures=3))
    for _ in range(3):
        assert sign_in(client, password="wrong password!!").status_code == 401
    response = sign_in(client)  # right password, but blocked
    assert response.status_code == 429
    assert auth.TOO_MANY in response.text


def test_throttle_window_expires():
    now = [0.0]
    throttle = LoginThrottle(max_failures=2, window=60, clock=lambda: now[0])
    throttle.fail("ip")
    throttle.fail("ip")
    assert throttle.blocked("ip")
    now[0] = 61
    assert not throttle.blocked("ip")


# --- CLI -------------------------------------------------------------

def test_cli_add_list_and_deactivate(monkeypatch, capsys):
    store = InMemoryInstructorStore()
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert manage(["add", "Bob@Example.edu", "--name", "Bob", "--password-stdin"], store) == 0
    assert verify_password(PASSWORD, store.find_by_email("bob@example.edu").password_hash)

    assert manage(["deactivate", "bob@example.edu"], store) == 0
    assert manage(["list"], store) == 0
    assert "bob@example.edu\tBob\tdeactivated\tlast sign-in: never" in capsys.readouterr().out


def test_cli_reports_errors(monkeypatch, capsys):
    store = InMemoryInstructorStore()
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert manage(["add", "bob@example.edu", "--password-stdin"], store) == 1
    assert manage(["passwd", "nobody@example.edu", "--password-stdin"], store) == 1
    assert "Error:" in capsys.readouterr().err

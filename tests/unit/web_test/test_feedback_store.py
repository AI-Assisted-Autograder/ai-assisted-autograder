
import hashlib
import os
from pathlib import Path

import pytest

from web.app.feedback_store import (
    atomic_write,
    secure_parent,
    token_hash,
)


def test_token_hash_returns_sha256_digest():
    token = "test-assignment-token-123"

    result = token_hash(token)

    assert result == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert len(result) == 64


def test_token_hash_is_deterministic():
    token = "same-token"

    assert token_hash(token) == token_hash(token)


def test_different_tokens_have_different_hashes():
    assert token_hash("token-one") != token_hash("token-two")


def test_secure_parent_creates_missing_directories(tmp_path):
    state_path = tmp_path / "private" / "nested" / "state.json"

    secure_parent(state_path)

    assert state_path.parent.is_dir()


def test_secure_parent_does_not_change_existing_parent(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)

    original_mode = os.stat(existing).st_mode & 0o777

    secure_parent(existing / "state.json")

    assert os.stat(existing).st_mode & 0o777 == original_mode


def test_atomic_write_creates_file_with_expected_content(tmp_path):
    path = tmp_path / "state.json"
    content = b'{"schema_version": 3}\n'

    atomic_write(path, content)

    assert path.read_bytes() == content


def test_atomic_write_sets_restrictive_file_permissions(tmp_path):
    path = tmp_path / "state.json"

    atomic_write(path, b"private state")

    assert os.stat(path).st_mode & 0o777 == 0o600


def test_atomic_write_overwrites_existing_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"old content")

    atomic_write(path, b"new content")

    assert path.read_bytes() == b"new content"


def test_atomic_write_without_overwrite_preserves_existing_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"original")

    with pytest.raises(FileExistsError):
        atomic_write(path, b"replacement", overwrite=False)

    assert path.read_bytes() == b"original"


def test_atomic_write_does_not_leave_temporary_file_on_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.json"

    def fail_replace(source, destination):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError):
        atomic_write(path, b"some content")

    assert not path.exists()
    assert list(tmp_path.glob(".state.json.*")) == []

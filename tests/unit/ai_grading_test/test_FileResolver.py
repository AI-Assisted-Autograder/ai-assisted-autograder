"""Unit tests for FileResolver.

These tests verify file selection, identifier validation, glob matching,
language-specific naming conventions, and filesystem security.

All tests use temporary directories. No database, network access, or
execution of submitted source code is required.
"""

import pytest

from ai_grading.FileResolver import FileResolver
from ai_grading.models import GradingInputError, TargetKind


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def resolver():
    """Return a fresh FileResolver for each test."""
    return FileResolver()


@pytest.fixture
def submission_dir(tmp_path):
    """Create a sample submission directory for file-resolution tests."""
    (tmp_path / "main.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )

    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "math_utils.py").write_text(
        "def multiply(a, b):\n"
        "    return a * b\n",
        encoding="utf-8",
    )

    (tmp_path / "README.md").write_text(
        "Example submission",
        encoding="utf-8",
    )

    return tmp_path


# ---------------------------------------------------------------------
# Basic file resolution
# ---------------------------------------------------------------------

def test_resolve_exact_file_returns_content_and_other_paths(
    resolver, submission_dir
):
    """Resolve one file and list the remaining regular files."""
    result = resolver.resolve(
        "main.py",
        TargetKind.FILE,
        "Python",
        submission_dir,
    )

    assert result.source_file.path == "main.py"
    assert "def add(a, b)" in result.source_file.content
    assert set(result.other_file_paths) == {
        "utils/math_utils.py",
        "README.md",
    }


def test_resolve_file_in_subdirectory(resolver, submission_dir):
    """Resolve a file using its relative path."""
    result = resolver.resolve(
        "utils/math_utils.py",
        "FILE",
        "Python",
        submission_dir,
    )

    assert result.source_file.path == "utils/math_utils.py"
    assert "def multiply(a, b)" in result.source_file.content


def test_resolve_file_using_glob(resolver, submission_dir):
    """Resolve a file using a wildcard pattern."""
    result = resolver.resolve(
        "main.*",
        "FILE",
        "Python",
        submission_dir,
    )

    assert result.source_file.path == "main.py"


def test_double_star_matches_nested_files(resolver, submission_dir):
    """Allow ** to match files in nested directories."""
    result = resolver.resolve(
        "**/math_utils.py",
        "FILE",
        "Python",
        submission_dir,
    )

    assert result.source_file.path == "utils/math_utils.py"


def test_double_star_matches_zero_directory_segments(
    resolver, submission_dir
):
    """Allow **/main.py to match main.py at the submission root."""
    result = resolver.resolve(
        "**/main.py",
        "FILE",
        "Python",
        submission_dir,
    )

    assert result.source_file.path == "main.py"


def test_file_matching_is_case_sensitive(resolver, submission_dir):
    """Do not match MAIN.py when the actual filename is main.py."""
    with pytest.raises(GradingInputError, match="No source file matches"):
        resolver.resolve(
            "MAIN.py",
            "FILE",
            "Python",
            submission_dir,
        )


def test_resolve_file_supports_unknown_language(resolver, submission_dir):
    """FILE targets should not require a supported programming language."""
    result = resolver.resolve(
        "main.py",
        "FILE",
        "Ruby",
        submission_dir,
    )

    assert result.source_file.path == "main.py"


# ---------------------------------------------------------------------
# Invalid identifiers and target kinds
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "   ",
        "../main.py",
        "utils/../../main.py",
        "/etc/passwd",
        r"C:\Users\student\main.py",
        r"utils\math_utils.py",
        "main.py\x00",
    ],
)
def test_reject_invalid_file_paths(resolver, submission_dir, identifier):
    """Reject blank, absolute, traversal, and non-POSIX paths."""
    with pytest.raises(GradingInputError):
        resolver.resolve(
            identifier,
            "FILE",
            "Python",
            submission_dir,
        )


@pytest.mark.parametrize("kind", ["UNKNOWN", "", None])
def test_reject_unsupported_target_kind(resolver, submission_dir, kind):
    """Reject target kinds that are not defined by TargetKind."""
    with pytest.raises(
        GradingInputError,
        match="Unsupported target kind",
    ):
        resolver.resolve(
            "main.py",
            kind,
            "Python",
            submission_dir,
        )


def test_reject_non_string_identifier(resolver, submission_dir):
    """Reject an identifier that is not a string."""
    with pytest.raises(
        GradingInputError,
        match="must not be blank",
    ):
        resolver.resolve(
            None,
            "FILE",
            "Python",
            submission_dir,
        )


def test_reject_missing_file(resolver, submission_dir):
    """Raise a useful error when no file matches the identifier."""
    with pytest.raises(
        GradingInputError,
        match="No source file matches",
    ):
        resolver.resolve(
            "missing.py",
            "FILE",
            "Python",
            submission_dir,
        )


def test_reject_ambiguous_file_pattern(resolver, submission_dir):
    """Require a pattern to match exactly one file."""
    (submission_dir / "other.py").write_text(
        "print('other')",
        encoding="utf-8",
    )

    with pytest.raises(
        GradingInputError,
        match="Multiple source files match",
    ):
        resolver.resolve(
            "*.py",
            "FILE",
            "Python",
            submission_dir,
        )


# ---------------------------------------------------------------------
# Python METHOD and CLASS identifiers
# ---------------------------------------------------------------------

@pytest.mark.parametrize("kind", [TargetKind.METHOD, TargetKind.CLASS])
def test_python_qualname_resolves_module_file(
    resolver, submission_dir, kind
):
    """Python identifiers select the module containing the qualname."""
    result = resolver.resolve(
        "main.py:add",
        kind,
        "Python",
        submission_dir,
    )

    assert result.source_file.path == "main.py"
    assert "def add(a, b)" in result.source_file.content


def test_python_nested_qualname_resolves_module_file(
    resolver, submission_dir
):
    """Support dotted Python qualnames."""
    result = resolver.resolve(
        "main.py:Calculator.add",
        "METHOD",
        "Python",
        submission_dir,
    )

    assert result.source_file.path == "main.py"


@pytest.mark.parametrize(
    "identifier",
    [
        "main.py",
        "main.py:",
        "main.py:add:",
        "main.txt:add",
        "main.py:invalid-name",
        "main.py:123",
        "main.py:add.*",
        "../main.py:add",
    ],
)
def test_reject_invalid_python_identifiers(
    resolver, submission_dir, identifier
):
    """Reject malformed Python module-and-qualname identifiers."""
    with pytest.raises(GradingInputError):
        resolver.resolve(
            identifier,
            "METHOD",
            "Python",
            submission_dir,
        )


def test_python_identifier_must_reference_existing_file(
    resolver, submission_dir
):
    """Reject valid identifier syntax when the module does not exist."""
    with pytest.raises(
        GradingInputError,
        match="No source file matches",
    ):
        resolver.resolve(
            "missing.py:calculate",
            "METHOD",
            "Python",
            submission_dir,
        )


def test_resolving_python_target_does_not_execute_source(
    resolver, submission_dir
):
    """Read submitted source without executing its top-level statements."""
    marker = submission_dir / "executed.txt"

    (submission_dir / "danger.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )

    resolver.resolve(
        "danger.py:run",
        "METHOD",
        "Python",
        submission_dir,
    )

    assert not marker.exists()


# ---------------------------------------------------------------------
# Java METHOD and CLASS identifiers
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("identifier", "kind", "filename"),
    [
        ("pkg.Calculator", "CLASS", "Calculator.java"),
        ("pkg.Calculator#add", "METHOD", "Calculator.java"),
        ("pkg.Outer.Inner", "CLASS", "Outer.java"),
        ("pkg.Outer$Inner", "CLASS", "Outer.java"),
        ("Outer", "CLASS", "Outer.java"),
        ("Outer$Inner#calculate", "METHOD", "Outer.java"),
    ],
)
def test_java_identifier_resolves_outer_class_file(
    resolver, tmp_path, identifier, kind, filename
):
    """Resolve Java identifiers to the outermost class source file."""
    (tmp_path / filename).write_text(
        f"class {filename.removesuffix('.java')} {{}}\n",
        encoding="utf-8",
    )

    result = resolver.resolve(
        identifier,
        kind,
        "Java",
        tmp_path,
    )

    assert result.source_file.path == filename


@pytest.mark.parametrize(
    ("identifier", "kind"),
    [
        ("pkg.Calculator#add", "CLASS"),
        ("pkg.Calculator", "METHOD"),
        ("pkg.Calculator#add#extra", "METHOD"),
        ("pkg.Calculator#invalid-name", "METHOD"),
        ("pkg..Calculator", "CLASS"),
        ("pkg.123Calculator", "CLASS"),
        ("pkg.Outer.Inner", "CLASS"),
    ],
)
def test_reject_invalid_java_identifiers(
    resolver, tmp_path, identifier, kind
):
    """Reject invalid Java class and method identifier formats."""
    (tmp_path / "Calculator.java").write_text(
        "class Calculator {}\n",
        encoding="utf-8",
    )

    with pytest.raises(GradingInputError):
        resolver.resolve(
            identifier,
            kind,
            "Java",
            tmp_path,
        )


def test_java_identifier_rejects_ambiguous_filename(
    resolver, tmp_path
):
    """Reject duplicate class filenames in different directories."""
    (tmp_path / "Calculator.java").write_text(
        "class Calculator {}\n",
        encoding="utf-8",
    )

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "Calculator.java").write_text(
        "class Calculator {}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        GradingInputError,
        match="Multiple source files match",
    ):
        resolver.resolve(
            "pkg.Calculator",
            "CLASS",
            "Java",
            tmp_path,
        )


# ---------------------------------------------------------------------
# Unsupported languages and invalid submission directories
# ---------------------------------------------------------------------

def test_reject_unsupported_language_for_method(
    resolver, submission_dir
):
    """Require a supported language for non-FILE targets."""
    with pytest.raises(
        GradingInputError,
        match="Cannot resolve",
    ):
        resolver.resolve(
            "main.py:add",
            "METHOD",
            "Ruby",
            submission_dir,
        )


def test_reject_nonexistent_submission_directory(resolver, tmp_path):
    """Reject a submission directory that does not exist."""
    with pytest.raises(
        GradingInputError,
        match="Cannot inspect the submission directory",
    ):
        resolver.resolve(
            "main.py",
            "FILE",
            "Python",
            tmp_path / "missing",
        )


def test_reject_file_as_submission_directory(resolver, tmp_path):
    """Require submission_dir to refer to an existing directory."""
    source = tmp_path / "not_a_directory.py"
    source.write_text("print('hello')", encoding="utf-8")

    with pytest.raises(
        GradingInputError,
        match="submission_dir must be an existing directory",
    ):
        resolver.resolve(
            "not_a_directory.py",
            "FILE",
            "Python",
            source,
        )


# ---------------------------------------------------------------------
# File encoding
# ---------------------------------------------------------------------

def test_reject_non_utf8_source_file(resolver, tmp_path):
    """Reject a selected source file that is not valid UTF-8."""
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe\xfa")

    with pytest.raises(
        GradingInputError,
        match="must be UTF-8 text",
    ):
        resolver.resolve(
            "bad.py",
            "FILE",
            "Python",
            tmp_path,
        )


# ---------------------------------------------------------------------
# Symbolic-link security
# ---------------------------------------------------------------------

def test_reject_source_file_symlink(resolver, tmp_path):
    """Reject a selected source file that is a symbolic link."""
    outside = tmp_path.parent / f"{tmp_path.name}_outside.py"
    outside.write_text(
        "print('outside')",
        encoding="utf-8",
    )

    try:
        (tmp_path / "linked.py").symlink_to(outside)

        with pytest.raises(
            GradingInputError,
            match=r"(?i)symbolic link",
        ):
            resolver.resolve(
                "linked.py",
                "FILE",
                "Python",
                tmp_path,
            )
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable in this environment")
    finally:
        outside.unlink(missing_ok=True)


def test_reject_directory_symlink_that_escapes_submission(
    resolver, tmp_path
):
    """Reject a directory symlink that points outside the submission."""
    outside = tmp_path.parent / f"{tmp_path.name}_outside_dir"
    outside.mkdir(exist_ok=True)

    (outside / "secret.py").write_text(
        "print('outside')",
        encoding="utf-8",
    )

    try:
        (tmp_path / "escape").symlink_to(
            outside,
            target_is_directory=True,
        )

        with pytest.raises(
            GradingInputError,
            match="escapes the submission directory",
        ):
            resolver.resolve(
                "main.py",
                "FILE",
                "Python",
                tmp_path,
            )
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable in this environment")
    finally:
        (outside / "secret.py").unlink(missing_ok=True)
        outside.rmdir()


def test_does_not_traverse_directory_symlinks(resolver, tmp_path):
    """Do not follow directory symlinks while searching for source files."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()

    (real_dir / "hidden.py").write_text(
        "print('hidden')",
        encoding="utf-8",
    )

    try:
        (tmp_path / "linked").symlink_to(
            real_dir,
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable in this environment")

    with pytest.raises(
        GradingInputError,
        match="No source file matches",
    ):
        resolver.resolve(
            "linked/hidden.py",
            "FILE",
            "Python",
            tmp_path,
        )
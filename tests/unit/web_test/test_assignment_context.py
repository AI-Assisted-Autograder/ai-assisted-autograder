"""Text extraction, limits and prompt rendering for assignment context."""

import pytest

from app.assignment_context import (
    HEADER,
    MAX_FILES,
    MAX_TOTAL_CHARS,
    MAX_UPLOAD_BYTES,
    check_context,
    clean_filename,
    extract_text,
    render_context,
    with_assignment_context,
)


def make_pdf(*lines: str) -> bytes:
    """A one-page PDF with a text layer, built by hand so the tests need no PDF writer."""
    text_ops = " ".join(f"({line}) Tj 0 -20 Td" for line in lines)
    stream = f"BT /F1 12 Tf 72 720 Td {text_ops} ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_text_file_is_decoded_and_normalized():
    assert extract_text("spec.md", b"\xef\xbb\xbf# Spec\r\nAdd two numbers.\r\n") == "# Spec\nAdd two numbers."


def test_pdf_text_is_extracted():
    text = extract_text("handout.pdf", make_pdf("Exercise 4", "Write Playwright tests"))
    assert "Exercise 4" in text
    assert "Write Playwright tests" in text


@pytest.mark.parametrize("name, data, message", [
    ("report.docx", b"PK\x03\x04\xff\xfe binary", "not a PDF or a UTF-8 text file"),
    ("blob.txt", b"abc\x00def", "binary"),
    ("empty.txt", b"  \n ", "no text"),
    ("broken.pdf", b"%PDF-1.4 not really", "could not be read"),
    ("big.txt", b"a" * (MAX_UPLOAD_BYTES + 1), "larger than"),
], ids=["unsupported-format", "binary", "empty", "invalid-pdf", "oversized"])
def test_unusable_files_are_refused(name, data, message):
    with pytest.raises(ValueError, match=message):
        extract_text(name, data)


def test_pdf_without_text_layer_is_refused():
    with pytest.raises(ValueError, match="no text layer"):
        extract_text("scan.pdf", make_pdf())


@pytest.mark.parametrize("raw, expected", [
    ("handout.pdf", "handout.pdf"),
    ("C:\\Users\\ada\\Desktop\\handout.pdf", "handout.pdf"),
    ("../../etc/passwd", "passwd"),
])
def test_filenames_lose_any_client_path(raw, expected):
    assert clean_filename(raw) == expected


def test_blank_filename_is_refused():
    with pytest.raises(ValueError):
        clean_filename("  ")


def test_limits():
    check_context("n" * 10, ["x" * (MAX_TOTAL_CHARS - 10)])
    with pytest.raises(ValueError, match="over the"):
        check_context("n" * 11, ["x" * (MAX_TOTAL_CHARS - 10)])
    with pytest.raises(ValueError, match="at most"):
        check_context(None, ["x"] * (MAX_FILES + 1))


def test_render_notes_then_files():
    text = render_context("Use recursion.", [("spec.md", "Add."), ("starter.py", "def add(): ...")])
    assert text.startswith(f"--- {HEADER} ---")
    assert text.index("Instructor notes:\nUse recursion.") < text.index("File: spec.md\nAdd.") < text.index("File: starter.py")


def test_background_without_context_is_unchanged():
    assert with_assignment_context("Correct and readable.", None, []) == "Correct and readable."
    assert with_assignment_context("Correct and readable.", "  ", []) == "Correct and readable."


def test_background_gets_context_appended():
    found = with_assignment_context("Correct and readable.", "Use recursion.", [])
    assert found.startswith("Correct and readable.\n\n--- ")
    assert found.endswith("Instructor notes:\nUse recursion.")

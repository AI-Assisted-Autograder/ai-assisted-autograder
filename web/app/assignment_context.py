"""Instructor-provided assignment context: notes and uploaded documents.

The instructor can attach a handout, a spec or starter code to an
assignment, and write free-text notes. Their text is added to the
background of every target's prompt (see with_assignment_context), so
the AI grades against the same assignment description the students had.

Only text is kept. A PDF's text is extracted on upload; a scanned PDF
with no text layer is refused rather than stored empty.
"""

import io
from pathlib import PurePath

# The upload itself. A long handout PDF is well under this.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_FILES = 20
# Characters of text across notes and every file. The context is repeated in
# each target's background, and the container reads its whole configuration
# in one response capped at 1 MB (feedback_assignment.MAX_CONFIG_BYTES), so
# this keeps several targets' worth comfortably under that.
MAX_TOTAL_CHARS = 60_000
MAX_FILENAME_CHARS = 200

HEADER = "Assignment context provided by the instructor"


def clean_filename(filename: str | None) -> str:
    # Browsers send a bare name, but old ones sent the client's full path.
    name = PurePath((filename or "").replace("\\", "/")).name.strip()
    if not name:
        raise ValueError("The uploaded file has no name")
    return name[:MAX_FILENAME_CHARS]


def extract_text(filename: str, data: bytes) -> str:
    """The text of a PDF or a UTF-8 text file. ValueError for anything else."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{filename} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if filename.lower().endswith(".pdf") or data.startswith(b"%PDF"):
        text = _pdf_text(filename, data)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError(
                f"{filename} is not a PDF or a UTF-8 text file. Save Word documents as PDF first."
            ) from None
        if "\x00" in text:
            raise ValueError(f"{filename} looks like a binary file, not text")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError(f"{filename} has no text in it")
    return text


def _pdf_text(filename: str, data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError(f"{filename} is password-protected")
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, OSError, KeyError, TypeError):
        raise ValueError(f"{filename} could not be read as a PDF") from None
    text = "\n\n".join(p.strip() for p in pages if p.strip()).replace("\x00", "")
    if not text:
        raise ValueError(f"{filename} has no text layer (it may be a scan), so the AI cannot read it")
    return text


def check_context(notes: str | None, file_texts: list[str]) -> None:
    """Refuse context that would not fit, before anything is saved."""
    if len(file_texts) > MAX_FILES:
        raise ValueError(f"An assignment can have at most {MAX_FILES} context files")
    total = len(notes or "") + sum(len(t) for t in file_texts)
    if total > MAX_TOTAL_CHARS:
        raise ValueError(
            f"Assignment context would be {total:,} characters, over the {MAX_TOTAL_CHARS:,} limit. "
            "Remove a file or shorten the notes."
        )


def render_context(notes: str | None, files) -> str:
    """Notes, then each file in upload order, as one block of prompt text.

    files are (filename, content) pairs, or objects with those attributes.
    """
    parts = []
    if notes and notes.strip():
        parts.append(f"Instructor notes:\n{notes.strip()}")
    for f in files:
        name, content = (f.filename, f.content) if hasattr(f, "filename") else f
        parts.append(f"File: {name}\n{content}")
    if not parts:
        return ""
    return f"--- {HEADER} ---\n\n" + "\n\n".join(parts)


def with_assignment_context(criteria_text: str | None, notes: str | None, files) -> str | None:
    """A target's background with the assignment's context appended."""
    context = render_context(notes, files)
    if not context:
        return criteria_text
    return f"{criteria_text}\n\n{context}" if criteria_text else context

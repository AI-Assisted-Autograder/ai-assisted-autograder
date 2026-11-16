"""Turn one autograder run's Gradescope test rows into per-target results.

The hook posts every row of results.json once per run; each target picks its
rows with the same rules the hook uses for AI evidence (analysis_config):

- evidence_sources reading results/results.json: rows whose name matches one
  of the source's ``include`` patterns, or every row when it has none;
- otherwise expected_tests: rows with exactly those names.

A target with neither gets no test status. Status: any failing row is FAIL,
an expected test missing from the run or a row with no readable outcome is
ERROR, otherwise PASS. Hidden rows count too: only instructors see these.

A functional_test_feedback target with no visible failing row is opted out,
matching the hook, which makes no AI call for it.
"""

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

from app.repository.base import TargetConfig, TargetTestOutcome

RESULTS_PATH = "results/results.json"
MESSAGE_LIMIT = 4_000
_TRUNCATED = "\n[truncated]"


def row_status(row: dict) -> str:
    """'pass', 'fail' or 'unknown', read the way the hook reads it."""
    status = row.get("status")
    if status:
        return {"passed": "pass", "failed": "fail"}.get(status, "unknown")
    try:
        score, maximum = Decimal(str(row.get("score"))), Decimal(str(row.get("max_score")))
    except (InvalidOperation, ValueError):
        return "unknown"
    if maximum > 0:
        return "pass" if score >= maximum else "fail"
    return "unknown"


def _visible(row: dict, default: str | None) -> bool:
    return (row.get("visibility") or default or "visible") == "visible"


def _selected(analysis: dict, tests: Sequence[dict]) -> tuple[list[dict], list[str]] | None:
    """The target's rows and the expected tests missing from the run, or None if unmapped."""
    sources = [
        s for s in analysis.get("evidence_sources") or ()
        if s.get("format", "gradescope") == "gradescope" and s.get("path") == RESULTS_PATH
    ]
    if sources:
        rows = []
        for row in tests:
            name = str(row.get("name") or "") + "\n"
            if any(
                not s.get("include") or any(re.search(p, name) for p in s["include"])
                for s in sources
            ):
                rows.append(row)
        return rows, []
    expected = list(analysis.get("expected_tests") or ())
    if expected:
        by_name = {str(row.get("name") or ""): row for row in tests}
        return [by_name[n] for n in expected if n in by_name], [n for n in expected if n not in by_name]
    return None


def _message(lines: Iterable[str]) -> str | None:
    text = "\n\n".join(lines).strip()
    if not text:
        return None
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - len(_TRUNCATED)] + _TRUNCATED
    return text


def target_outcome(target: TargetConfig, tests: Sequence[dict], visibility: str | None = None) -> TargetTestOutcome:
    analysis = target.analysis_config or {}
    selection = _selected(analysis, tests)
    ai_needed = None
    status = message = None
    if selection is not None:
        rows, missing = selection
        outcomes = [(row, row_status(row)) for row in rows]
        failing = [row for row, s in outcomes if s == "fail"]
        unknown = [row for row, s in outcomes if s == "unknown"]
        if failing:
            status = "FAIL"
        elif missing or unknown:
            status = "ERROR"
        elif rows:
            status = "PASS"
        if status in ("FAIL", "ERROR"):
            lines = [
                f"{row.get('name') or 'Unnamed test'}: {(row.get('output') or '').strip()}".rstrip(": ")
                for row in failing + unknown
            ]
            if missing:
                lines.append("Missing from the run: " + ", ".join(missing))
            message = _message(lines)
        if analysis.get("behavior") == "functional_test_feedback":
            ai_needed = any(_visible(row, visibility) for row in failing)
    return TargetTestOutcome(target.identifier, status, message, ai_needed)


def map_outcomes(
    targets: Iterable[TargetConfig], tests: Sequence[dict], visibility: str | None = None
) -> list[TargetTestOutcome]:
    """Each target's outcome; targets with nothing to record are left out."""
    found = (target_outcome(t, tests, visibility) for t in targets)
    return [o for o in found if o.status is not None or o.ai_needed is not None]

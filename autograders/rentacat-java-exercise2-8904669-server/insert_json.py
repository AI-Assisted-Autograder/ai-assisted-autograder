#!/usr/bin/env python3
"""Append coverage-result entries to a Gradescope results document."""

import json
import shutil
import sys
import tempfile
from pathlib import Path


RESULTS = Path("/autograder/results/results.json")
COVERAGE = Path("coverage_results.json")


def load_coverage_entries(path: Path) -> list[dict]:
    """Read the comma-separated JSON objects produced by fill_json.sh."""
    entries = json.loads("[" + path.read_text(encoding="utf-8").strip() + "]")
    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("coverage_results.json must contain test objects")
    return entries


def main() -> int:
    if not COVERAGE.is_file():
        print(f"insert_json.py: {COVERAGE} not found, run fill_json.sh first", file=sys.stderr)
        return 1
    if not RESULTS.is_file():
        print(f"insert_json.py: {RESULTS} not found", file=sys.stderr)
        return 1

    try:
        results = json.loads(RESULTS.read_text(encoding="utf-8"))
        if not isinstance(results, dict):
            raise ValueError("results.json must contain a JSON object")
        tests = results.get("tests")
        if not isinstance(tests, list):
            raise ValueError("results.json does not contain a tests array")
        tests.extend(load_coverage_entries(COVERAGE))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"insert_json.py: could not merge coverage results: {error}", file=sys.stderr)
        return 1

    temporary_path = None
    try:
        # Write and validate the entire replacement before touching results.json.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=RESULTS.parent, delete=False
        ) as temporary_file:
            json.dump(results, temporary_file, separators=(",", ":"))
            temporary_path = Path(temporary_file.name)

        # Copying over the original retains its ownership and permissions.
        shutil.copyfile(temporary_path, RESULTS)
    except OSError as error:
        print(f"insert_json.py: could not write merged results: {error}", file=sys.stderr)
        return 1
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())

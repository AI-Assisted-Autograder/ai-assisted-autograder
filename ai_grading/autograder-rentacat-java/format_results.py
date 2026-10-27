#!/usr/bin/env python3
"""Wrap Gradescope feedback messages in a completed results document."""

import json
import shutil
import sys
import tempfile
from pathlib import Path


RESULTS = Path("/autograder/results/results.json")
LINE_WIDTH = 100


def wrap(text: str, width: int = LINE_WIDTH) -> str:
    """Insert display line breaks without changing the words in feedback."""
    wrapped_lines = []
    for line in text.splitlines():
        remaining = line.strip()
        while len(remaining) > width:
            break_at = remaining.rfind(" ", 0, width + 1)
            if break_at < 1:
                break_at = width
            wrapped_lines.append(remaining[:break_at])
            remaining = remaining[break_at:].strip()
        wrapped_lines.append(remaining)
    return "\n".join(wrapped_lines)


def main() -> int:
    if not RESULTS.is_file():
        print(f"format_results.py: {RESULTS} not found", file=sys.stderr)
        return 1

    try:
        results = json.loads(RESULTS.read_text(encoding="utf-8"))
        if not isinstance(results, dict):
            raise ValueError("results.json must contain a JSON object")
        tests = results.get("tests")
        if not isinstance(tests, list):
            raise ValueError("results.json does not contain a tests array")

        for test in tests:
            if not isinstance(test, dict):
                raise ValueError("results.json tests array must contain objects")
            output = test.get("output")
            if isinstance(output, str):
                test["output"] = wrap(output)
            test["output_format"] = "simple_format"
        results["test_output_format"] = "simple_format"
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"format_results.py: could not format results: {error}", file=sys.stderr)
        return 1

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=RESULTS.parent, delete=False
        ) as temporary_file:
            json.dump(results, temporary_file, separators=(",", ":"))
            temporary_path = Path(temporary_file.name)
        shutil.copyfile(temporary_path, RESULTS)
    except OSError as error:
        print(f"format_results.py: could not write formatted results: {error}", file=sys.stderr)
        return 1
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())

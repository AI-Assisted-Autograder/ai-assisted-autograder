"""Deterministically splice AI feedback into an instructor-owned results file.

The instructor's test framework owns the functional-test entries.  This module
only reads that JSON object and appends zero-point entries, so AI feedback
cannot change the functional-test score or the platform's existing schema.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


class ResultsFormatError(ValueError):
    """The instructor's results file is not an object with a tests list."""


@dataclass(frozen=True)
class FeedbackItem:
    target: str
    body: str
    suggested_score: Decimal | float | None = None

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise ValueError("feedback target must not be blank")
        if not self.body.strip():
            raise ValueError("feedback body must not be blank")


@dataclass(frozen=True)
class FeedbackPackage:
    items: tuple[FeedbackItem, ...] = ()


class ResultsPackager:
    """Read, update, and atomically write a Gradescope-style results object."""

    def parse(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        with Path(path).open("r", encoding="utf-8") as file:
            return self._validate(json.load(file))

    def write(self, path: str | os.PathLike[str], results: Mapping[str, Any]) -> None:
        validated = self._validate(results)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(validated, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def splice(
        self, results: Mapping[str, Any], feedback: FeedbackPackage
    ) -> dict[str, Any]:
        updated = self._validate(results)
        for item in feedback.items:
            updated["tests"].append(self._feedback_entry(item))
        return updated

    def append_deferred(self, results: Mapping[str, Any]) -> dict[str, Any]:
        updated = self._validate(results)
        updated["tests"].append(
            self._message_entry(
                "AI Feedback Deferred",
                "AI feedback was deferred. It will be generated for the active submission during the next nightly run.",
            )
        )
        return updated

    def append_error(self, results: Mapping[str, Any], message: str) -> dict[str, Any]:
        if not message.strip():
            raise ValueError("error message must not be blank")
        updated = self._validate(results)
        updated["tests"].append(self._message_entry("AI Feedback Unavailable", message))
        return updated

    @staticmethod
    def _validate(results: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(results, Mapping):
            raise ResultsFormatError("results must be a JSON object")
        tests = results.get("tests")
        if not isinstance(tests, list):
            raise ResultsFormatError("results must contain a tests list")
        # Copy the outer object and list so callers never observe mutation.
        return {**results, "tests": [*tests]}

    @staticmethod
    def _message_entry(name: str, output: str) -> dict[str, Any]:
        return {"name": name, "score": 0, "max_score": 0, "output": output}

    @classmethod
    def _feedback_entry(cls, item: FeedbackItem) -> dict[str, Any]:
        output = item.body
        if item.suggested_score is not None:
            output = f"Suggested score: {item.suggested_score}\n\n{output}"
        return cls._message_entry(f"AI Feedback: {item.target}", output)

"""Run a complete grading call without API keys: python -m ai_grading.demo."""

import json
from dataclasses import asdict
from pathlib import Path

from .AIGradingComponent import AIGradingComponent
from .ILLMProvider import FakeProvider
from .models import FeedbackRequest


def main() -> None:
    grader = AIGradingComponent([FakeProvider()])
    samples = Path(__file__).parent / "tests" / "samples"
    for path in sorted(samples.glob("*.json")):
        request = FeedbackRequest(**json.loads(path.read_text(encoding="utf-8")))
        result = grader.grade(request)
        print(json.dumps({"sample": path.stem, "response": asdict(result.selected)},
                         indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

"""Run GeminiProvider against the shared samples using the real API."""

import json
from dataclasses import asdict
from pathlib import Path

from ai_grading.AIGradingComponent import AIGradingComponent
from ai_grading.ILLMProvider.GeminiProvider import GeminiProvider
from ai_grading.models import FeedbackRequest


def main() -> None:
    provider = GeminiProvider()
    try:
        grader = AIGradingComponent([provider])
        for path in sorted((Path(__file__).parent / "samples").glob("*.json")):
            request = FeedbackRequest(**json.loads(path.read_text(encoding="utf-8")))
            result = grader.grade(request)
            print(json.dumps({"sample": path.stem, "response": asdict(result.selected)},
                             indent=2, ensure_ascii=False), flush=True)
    finally:
        provider.close()


if __name__ == "__main__":
    main()

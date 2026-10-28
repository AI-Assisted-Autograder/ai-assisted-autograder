"""Connect the existing AIGrading module to the original Gradescope results."""

import json
from pathlib import Path
import sys

from .AIGradingComponent import AIGradingComponent
from .FileResolver import FileResolver
from .ILLMProvider.GeminiProvider import GeminiProvider
from .models import FeedbackRequest, GradingTarget, Rubric


def add_feedback(root=Path("/autograder")):
    root = Path(root)
    results_path = root / "results/results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    config = json.loads((root / "source/ai_feedback.json").read_text(encoding="utf-8"))
    diagnostics = tuple(
        f"{test['name']}: {test['score']}/{test['max_score']}\n{test.get('output', '')}"
        for test in results["tests"]
        if test.get("visibility", results.get("test_visibility", "visible")) == "visible"
    )

    for target in config["targets"]:
        row = {
            "name": f"AI Feedback (Gemini): {target['source']}",
            "score": 0,
            "max_score": 0,
            "visibility": "visible",
            "output_format": "text",
            "output": "Gemini feedback is unavailable. Your functional-test score is unchanged.",
        }
        try:
            request = FeedbackRequest(
                target=GradingTarget(identifier=target["source"], kind="FILE", max_score=0.0),
                rubric=Rubric(
                    criteria_text=target["rubric"]["criteria_text"],
                    feedback_guidance=target["rubric"].get("feedback_guidance", "")
                    + "\nProvide feedback only. Use SCORE: NONE; do not assign a grade.",
                ),
                resolved=FileResolver().resolve(target["source"], "FILE", "", root / "submission"),
                functional_test_feedback=diagnostics,
            )
            provider = GeminiProvider()
            try:
                response = AIGradingComponent([provider]).grade(request).selected
            finally:
                provider.close()
            row["output"] = response.feedback_text
            row["extra_data"] = {
                "provider": response.provider,
                "model": response.model_name,
            }
        except Exception as error:
            print(f"Gemini feedback unavailable: {type(error).__name__}", file=sys.stderr)
        results["tests"].append(row)

    results_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


if __name__ == "__main__":
    add_feedback()

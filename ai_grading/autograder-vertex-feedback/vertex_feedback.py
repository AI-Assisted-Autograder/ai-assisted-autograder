"""Append backend feedback while preserving the original functional-test results."""

import json
from pathlib import Path
import sys

from local_client import ClientError, request_feedback


def add_feedback(root=Path("/autograder")):
    root = Path(root)
    results_path = root / "results/results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    config = json.loads((root / "source/config.json").read_text(encoding="utf-8"))
    feedback_config = json.loads((root / "source/ai_feedback.json").read_text(encoding="utf-8"))
    diagnostics = [
        f"{test['name']}: {test['score']}/{test['max_score']}\n{test.get('output', '')}"
        for test in results["tests"]
        if test.get("visibility", results.get("test_visibility", "visible")) == "visible"
    ]
    for target in feedback_config["targets"]:
        row = {
            "name": f"AI Feedback (Vertex): {target['source']}",
            "score": 0,
            "max_score": 0,
            "visibility": "visible",
            "output_format": "text",
            "output": "Vertex feedback is unavailable. Your functional-test score is unchanged.",
        }
        try:
            submissions = [
                path for path in (root / "submission").glob(target["source"]) if path.is_file()
            ]
            if len(submissions) != 1:
                raise ClientError("Each feedback target must match exactly one submitted file.")
            response = request_feedback(
                backend_url=config["backend_url"],
                token_file=root / "source/assignment.token",
                code=submissions[0].read_text(encoding="utf-8"),
                target_id=target["target_id"],
                functional_test_feedback=diagnostics,
            )
            row["output"] = response["feedback_text"]
            row["extra_data"] = {
                "provider": "vertex",
                "model": response["model_name"],
                "request_id": response["request_id"],
            }
        except ClientError as error:
            row["output"] += f"\n{error}"
        except Exception:
            pass
        results["tests"].append(row)
    temporary_path = results_path.with_suffix(".tmp")
    try:
        temporary_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        temporary_path.replace(results_path)
    finally:
        temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        add_feedback()
    except Exception:
        print("Vertex feedback unavailable: cannot update the original results.", file=sys.stderr)

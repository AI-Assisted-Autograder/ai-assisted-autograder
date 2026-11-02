"""Validate legacy text and structured analysis through one provider contract."""

import json
import re
from dataclasses import replace
from decimal import Decimal

from pydantic import TypeAdapter

from ..models import FailureFeedbackItem, FeedbackItem, FeedbackRequest, FeedbackResponse, Score
from .ILLMProvider import ProviderError


def parse_feedback(
    output: str,
    *,
    request: FeedbackRequest,
    provider: str,
    model_name: str,
    prompt_version: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> FeedbackResponse:
    """Validate model output; code owns verdict IDs, scores and serialization."""
    try:
        items = ()
        failure_feedback = ()
        suggested_score = None
        grading_status = "complete"
        if request.analysis.behavior is not None:
            if len(output) > 64_000:
                raise ValueError("structured feedback exceeds the response limit")
            document = json.loads(output)
            if request.analysis.behavior == "functional_test_feedback":
                if (not isinstance(document, dict) or set(document) != {"failure_feedback"}
                        or not isinstance(document["failure_feedback"], list)):
                    raise ValueError("functional feedback requires only failure explanations")
                failure_feedback = TypeAdapter(tuple[FailureFeedbackItem, ...]).validate_python(
                    document["failure_feedback"],
                )
                expected = [item.id for item in request.evidence
                            if item.source == request.analysis.functional_result_source
                            and item.kind == "runtime" and item.status == "fail"]
                ids = [item.evidence_id for item in failure_feedback]
                if not expected or len(ids) != len(set(ids)) or set(ids) != set(expected):
                    raise ValueError("functional feedback must match every selected failed row")
                by_id = {item.evidence_id: item for item in failure_feedback}
                failure_feedback = tuple(by_id[identifier] for identifier in expected)
                feedback_text = "Explanations for failed functional tests."
            else:
                if (not isinstance(document, dict) or set(document) != {"items"}
                        or not isinstance(document["items"], list)):
                    raise ValueError("AI grading requires only grading verdicts")
                if any(not isinstance(item, dict)
                       or set(item) != {"test_id", "verdict", "feedback", "evidence"}
                       for item in document["items"]):
                    raise ValueError("model verdicts must not contain scoring or extra fields")
                items = TypeAdapter(tuple[FeedbackItem, ...]).validate_python(document["items"])
                ids = [item.test_id for item in items]
                expected = request.analysis.grading_items
                if (not expected or len(ids) != len(set(ids))
                        or set(ids) != {item.id for item in expected}):
                    raise ValueError("AI verdicts must match every configured grading item")
                by_id = {item.test_id: item for item in items}
                items = tuple(replace(
                    by_id[configured.id], max_score=configured.points,
                    suggested_score=(configured.points if by_id[configured.id].verdict == "pass"
                                     else 0.0 if by_id[configured.id].verdict == "fail" else None),
                ) for configured in expected)
                if all(item.suggested_score is not None for item in items):
                    suggested_score = float(sum(
                        (Decimal(str(item.suggested_score)) for item in items), Decimal(0),
                    ))
                    feedback_text = "AI grading completed."
                else:
                    grading_status = "pending_regrade"
                    feedback_text = "AI grading incomplete; pending regrade."
        elif request.analysis.mode == "code_feedback":
            score_line, separator, feedback_text = output.partition("\n")
            match = re.fullmatch(r"SCORE: (NONE|[0-9]+(?:\.[0-9]+)?)\r?", score_line)
            if not separator or match is None:
                raise ValueError("missing or invalid score line")
            value = match.group(1)
            suggested_score = None if value == "NONE" else float(value)
            if (suggested_score is not None and request.target.max_score is not None
                    and suggested_score > request.target.max_score):
                raise ValueError("score exceeds maximum")
        else:
            # A bounded JSON document also limits arrays and evidence returned by a model.
            if len(output) > 64_000:
                raise ValueError("structured feedback exceeds the response limit")
            document = json.loads(output)
            if (not isinstance(document, dict)
                    or not {"feedback_text", "items"}.issubset(document)
                    or set(document) - {"feedback_text", "items", "suggested_score"}
                    or not isinstance(document["items"], list) or not document["items"]):
                raise ValueError("structured feedback requires a summary and nonempty items")
            feedback_text = document["feedback_text"]
            suggested_score = TypeAdapter(Score | None).validate_python(document.get("suggested_score"))
            per_test = request.analysis.points_per_test
            if suggested_score is not None:
                if per_test is not None:
                    raise ValueError("per-test totals are computed from verdicts, not model scores")
                if request.target.max_score is None:
                    raise ValueError("structured scoring requires a configured maximum")
                if suggested_score > request.target.max_score:
                    raise ValueError("score exceeds maximum")
            if any(not isinstance(item, dict)
                   or set(item) != {"test_id", "verdict", "feedback", "evidence"}
                   for item in document["items"]):
                raise ValueError("model verdict items must not contain scoring or extra fields")
            items = TypeAdapter(tuple[FeedbackItem, ...]).validate_python(document["items"])
            ids = [item.test_id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError("analysis verdict identifiers must be distinct")
            expected = request.analysis.expected_tests
            if request.analysis.mode == "testplan" and not expected:
                raise ValueError("testplan analysis requires expected test identifiers")
            if expected:
                if set(ids) != set(expected):
                    raise ValueError("analysis verdicts must match the expected tests")
                by_id = {item.test_id: item for item in items}
                items = tuple(by_id[test_id] for test_id in expected)
            if per_test is not None:
                items = tuple(replace(
                    item, max_score=per_test,
                    suggested_score=(per_test if item.verdict == "pass" else
                                     0.0 if item.verdict == "fail" else None),
                ) for item in items)
                if all(item.suggested_score is not None for item in items):
                    suggested_score = float(Decimal(str(per_test)) * sum(item.verdict == "pass" for item in items))
        return FeedbackResponse(
            rubric_id=request.rubric.id,
            provider=provider,
            model_name=model_name,
            prompt_version=prompt_version,
            feedback_text=feedback_text,
            suggested_score=suggested_score,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            items=items,
            failure_feedback=failure_feedback,
            grading_status=grading_status,
        )
    except ValueError:
        raise ProviderError(
            "invalid_provider_response", f"{provider} returned invalid grading feedback.",
        ) from None

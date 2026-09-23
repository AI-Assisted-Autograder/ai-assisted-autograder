"""Local HTTP harness tests; no database or real model calls."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_grading.api import create_app, engine_from_env
from ai_grading.engine import GradingEngine
from ai_grading.models import CodeFile, FeedbackRequest, TargetKind
from ai_grading.prompt_builder import PromptBuilder
from ai_grading.providers import FakeProvider
from ai_grading.providers.base import ProviderError


def request_payload():
    return {
        "target": {"identifier": "main.py:run", "kind": "METHOD", "max_score": 5},
        "rubric": {"criteria_text": "Use the helper correctly.",
                   "feedback_guidance": "Give a concise hint."},
        "resolved": {
            "source_file": {"path": "main.py", "content": "from helper import add\nprint(add(1, 2))\n"},
            "other_file_paths": ["helper.py"],
        },
        "functional_test_feedback": ["test_add passed: add(1, 2) returned 3."],
    }


class FailingProvider:
    name = "FAILING"
    model = "test"

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def generate_feedback(self, request):
        self.calls += 1
        raise self.error

    def estimated_cost(self, request):
        return None


class ApiTests(unittest.TestCase):
    def client(self, *providers, **engine_options):
        engine = GradingEngine(providers or [FakeProvider()], sleep=lambda _: None, **engine_options)
        return TestClient(create_app(engine), raise_server_exceptions=False)

    def test_resolved_source_without_database_ids_roundtrips_canonical_data(self):
        provider = FakeProvider("Review helper.py.", suggested_score=4)
        payload = request_payload()
        with self.client(provider) as client:
            response = client.post("/test/grade", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(set(result), {"responses", "selected", "comparison"})
        self.assertEqual(result["selected"]["suggested_score"], 4)
        self.assertEqual(len(provider.requests), 1)
        canonical = provider.requests[0]
        self.assertIsInstance(canonical, FeedbackRequest)
        self.assertIs(canonical.target.kind, TargetKind.METHOD)
        self.assertIsInstance(canonical.resolved.source_file, CodeFile)
        self.assertEqual(canonical.resolved.source_file.path, "main.py")
        self.assertEqual(canonical.resolved.other_file_paths, ("helper.py",))
        self.assertIn("helper.py", provider.prompts[0])
        self.assertEqual(canonical.functional_test_feedback, tuple(payload["functional_test_feedback"]))

    def test_openapi_documents_nested_contract_and_docs_are_available(self):
        with self.client() as client:
            self.assertEqual(client.get("/health").json()["status"], "ok")
            self.assertEqual(client.get("/docs").status_code, 200)
            response = client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        spec = response.json()
        operation = spec["paths"]["/test/grade"]["post"]
        schemas = spec["components"]["schemas"]

        def resolve(schema):
            return schemas[schema["$ref"].rsplit("/", 1)[1]]

        request = resolve(operation["requestBody"]["content"]["application/json"]["schema"])
        self.assertEqual(set(request["required"]), {"target", "rubric", "resolved"})
        self.assertIn("identifier", resolve(request["properties"]["target"])["properties"])
        resolved = resolve(request["properties"]["resolved"])
        self.assertIn("content", resolve(resolved["properties"]["source_file"])["properties"])
        self.assertIn("other_file_paths", resolved["properties"])
        result = resolve(operation["responses"]["200"]["content"]["application/json"]["schema"])
        self.assertIn("feedback_text", resolve(result["properties"]["selected"])["properties"])
        self.assertIn("confidence", resolve(result["properties"]["comparison"])["properties"])
        error = resolve(operation["responses"]["422"]["content"]["application/json"]["schema"])
        self.assertIn("retryable", resolve(error["properties"]["error"])["properties"])

    def test_invalid_contract_is_rejected_before_provider_and_does_not_echo_input(self):
        invalid = []
        for missing in ("target", "rubric", "resolved"):
            payload = request_payload()
            del payload[missing]
            invalid.append(payload)
        for value in (True, "5", float("nan"), float("inf")):
            payload = request_payload()
            payload["target"]["max_score"] = value
            invalid.append(payload)
        payload = request_payload()
        payload["functional_test_feedback"] = [{"status": "PASS"}]
        invalid.append(payload)
        for container in (None, "rubric", "target"):
            payload = request_payload()
            (payload if container is None else payload[container])["unknown"] = "secret-marker"
            invalid.append(payload)
        provider = FakeProvider()
        with self.client(provider) as client:
            for payload in invalid:
                with self.subTest(payload=payload):
                    if "resolved" in payload:
                        payload["resolved"]["source_file"]["content"] = "private-source-marker"
                    response = client.post("/test/grade", content=json.dumps(payload),
                                           headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()["error"]["code"], "invalid_request")
                    self.assertNotIn("private-source-marker", response.text)
                    self.assertNotIn("secret-marker", response.text)
        self.assertEqual(provider.requests, [])

    def test_external_payload_is_rejected_before_provider(self):
        provider = FakeProvider()
        with self.client(provider) as client:
            response = client.post("/test/grade", json={
                "grading_input": request_payload(), "other": "private-source-marker",
            })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertNotIn("private-source-marker", response.text)
        self.assertEqual(provider.requests, [])

    def test_old_contract_and_invalid_resolved_source_are_rejected(self):
        provider = FakeProvider()
        invalid = []
        for resolved in ({}, {"source_file": []},
                         {"source_file": request_payload()["resolved"]["source_file"],
                          "other_file_paths": ["main.py"]},
                         {"source_file": request_payload()["resolved"]["source_file"],
                          "other_file_paths": ["helper.py", "helper.py"]}):
            payload = request_payload()
            payload["resolved"] = resolved
            invalid.append(payload)
        for obsolete in ("files", "outcomes", "grading_task_id", "request_id", "metadata"):
            payload = request_payload()
            payload[obsolete] = []
            invalid.append(payload)
        with self.client(provider) as client:
            for payload in invalid:
                with self.subTest(payload=payload):
                    self.assertEqual(client.post("/test/grade", json=payload).status_code, 422)
        self.assertEqual(provider.requests, [])

    def test_context_limit_rejects_whole_request_without_truncation_or_provider_call(self):
        provider = FakeProvider(builder=PromptBuilder(max_context_chars=100))
        with self.client(provider) as client:
            response = client.post("/test/grade", json=request_payload())
        self.assertEqual(response.status_code, 422)
        self.assertIn("max_context_chars", response.json()["error"]["message"])
        self.assertEqual(provider.requests, [])

    def test_provider_errors_report_terminal_failure_after_in_run_retries(self):
        for code, retryable, status in (("timeout", True, 504),
                                       ("provider_unavailable", True, 503),
                                       ("invalid_provider_response", False, 502)):
            with self.subTest(code=code):
                provider = FailingProvider(ProviderError(code, "Public failure.", retryable=retryable))
                with self.client(provider) as client:
                    response = client.post("/test/grade", json=request_payload())
                self.assertEqual(response.status_code, status)
                detail = response.json()["error"]
                self.assertEqual(detail["code"], "retry_exhausted" if retryable else code)
                self.assertFalse(detail["retryable"])
                self.assertEqual(detail["attempts"], 3 if retryable else 1)
                self.assertEqual(provider.calls, detail["attempts"])
                self.assertNotIn("selected", response.json())

    def test_unexpected_failure_does_not_expose_source_or_secret(self):
        provider = FailingProvider(RuntimeError("private-source-marker secret-marker"))
        payload = request_payload()
        payload["resolved"]["source_file"]["content"] = "private-source-marker"
        with self.client(provider) as client:
            response = client.post("/test/grade", json=payload)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("private-source-marker", response.text)
        self.assertNotIn("secret-marker", response.text)
        self.assertNotIn("selected", response.json())
        self.assertEqual(provider.calls, 1)

    def test_default_selection_preserves_every_result_and_reports_score_spread(self):
        first, second = FakeProvider("First hint", 2), FakeProvider("Second hint", 4.5)
        with self.client(first, second) as client:
            response = client.post("/test/grade", json=request_payload())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual([r["suggested_score"] for r in result["responses"]], [2, 4.5])
        self.assertEqual(result["selected"], result["responses"][0])
        self.assertIn("First configured provider", result["comparison"]["selection_reason"])
        self.assertEqual(result["comparison"]["score_spread"], 2.5)
        self.assertIsNone(result["comparison"]["confidence"])

    def test_selector_can_choose_second_result_without_changing_provider_contract(self):
        with self.client(FakeProvider("First", 2), FakeProvider("Second", 4),
                         selector=lambda responses: 1) as client:
            response = client.post("/test/grade", json=request_payload())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["selected"], result["responses"][1])
        self.assertIn("configured selector", result["comparison"]["selection_reason"])

    def test_incomplete_run_returns_error_instead_of_partial_success(self):
        first, last = FakeProvider("Finished", 3), FakeProvider("Should not run", 0)
        failure = FailingProvider(ProviderError("incomplete_provider_response", "Incomplete feedback."))
        with self.client(first, failure, last) as client:
            response = client.post("/test/grade", json=request_payload())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(set(response.json()), {"error"})
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(failure.calls, 1)
        self.assertEqual(last.requests, [])

    def test_out_of_range_provider_score_is_an_error(self):
        with self.client(FakeProvider("Bad score", 6)) as client:
            response = client.post("/test/grade", json=request_payload())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "invalid_response")
        self.assertNotIn("selected", response.json())


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config_path = Path(folder.name) / ".gemini.json"
        config_patch = patch("ai_grading.providers.gemini.LOCAL_CONFIG_PATH", self.config_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_fake_mode_needs_no_key_and_no_invented_score(self):
        with patch.dict(os.environ, {}, clear=True), TestClient(create_app()) as client:
            response = client.post("/test/grade", json=request_payload())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["selected"]["suggested_score"])
        self.assertIsNone(response.json()["comparison"]["score_spread"])

    def test_gemini_missing_model_or_key_fails_at_startup(self):
        for extra in ({}, {"GEMINI_MODEL": "test"}, {"GEMINI_API_KEY": "dummy-key"}):
            with self.subTest(extra=extra), patch.dict(
                os.environ, {"LLM_PROVIDER": "gemini", **extra}, clear=True,
            ), self.assertRaises(ValueError):
                create_app()

    def test_gemini_mode_uses_local_configuration(self):
        self.config_path.write_text(json.dumps({"model": "local-model", "api_key": "local-key"}))
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini"}, clear=True), patch(
            "ai_grading.providers.gemini.genai.Client",
        ) as ctor:
            engine = engine_from_env()
            self.assertEqual(engine.providers[0].model, "local-model")
            self.assertEqual(ctor.call_args.kwargs["api_key"], "local-key")
            engine.providers[0].close()

    def test_invalid_environment_configuration_fails_at_startup(self):
        invalid = [
            {"LLM_PROVIDER": "unknown"},
            {"LLM_MAX_CONTEXT_CHARS": "0"},
            {"LLM_MAX_CONTEXT_CHARS": "no-limit"},
            {"LLM_MAX_FEEDBACK_CHARS": "0"},
            {"LLM_MAX_TARGET_FEEDBACK_CHARS": "0"},
            {"LLM_MAX_ATTEMPTS": "0"},
            {"LLM_RETRY_INITIAL_DELAY": "-1"},
            {"LLM_RETRY_MULTIPLIER": "0.5"},
            {"LLM_RETRY_MAX_DELAY": "nan"},
            *[{"LLM_PROVIDER": "gemini", "GEMINI_MODEL": "test", "GEMINI_API_KEY": "dummy-key", **v}
              for v in ({"LLM_TIMEOUT_SECONDS": "nan"}, {"LLM_TIMEOUT_SECONDS": "0"},
                        {"LLM_MAX_OUTPUT_TOKENS": "0"}, {"LLM_MAX_OUTPUT_TOKENS": "4.5"})],
        ]
        for environment in invalid:
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True,
            ), self.assertRaises(ValueError):
                engine_from_env()


if __name__ == "__main__":
    unittest.main()

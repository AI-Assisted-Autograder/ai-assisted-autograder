"""Exercise the real Gemini SDK with an in-memory HTTP transport; no API key needed."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import httpx
from google import genai
from google.genai import types

from ai_grading.examples import sample_request
from ai_grading.models import MAX_FEEDBACK_CHARS, GradingInputError
from ai_grading.prompt_builder import PromptBuilder
from ai_grading.providers.base import ProviderError
from ai_grading.providers.gemini import GeminiProvider


def completed(output="SCORE: 3.5\nCheck the empty input case.", *, finish_reason="STOP"):
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": output}]},
            "finishReason": finish_reason,
        }],
        "usageMetadata": {
            "promptTokenCount": 120, "candidatesTokenCount": 35, "thoughtsTokenCount": 8,
        },
        "modelVersion": "gemini-test-001",
    }


class GeminiProviderTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config_path = Path(folder.name) / ".gemini.json"
        config_patch = patch("ai_grading.providers.gemini.LOCAL_CONFIG_PATH", self.config_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        environment_patch = patch.dict(os.environ, {}, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

    def provider(self, payload, *, status=200, transport_error=None, **kwargs):
        self.requests = []

        def respond(request):
            self.requests.append(request)
            if transport_error:
                raise transport_error("sensitive upstream detail", request=request)
            return httpx.Response(status, json=payload)

        http_client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                httpx_client=http_client,
                timeout=999_000,
                retry_options=types.HttpRetryOptions(attempts=3),
            ),
        )
        self.addCleanup(client.close)
        return GeminiProvider("gemini-test", client=client, **kwargs)

    def test_sends_all_context_separately_from_instructions_and_preserves_result(self):
        req = sample_request()
        provider = self.provider(completed())
        result = provider.generate_feedback(req)
        self.assertEqual(result.rubric_id, req.rubric.id)
        self.assertEqual(result.provider, "GOOGLE")
        self.assertEqual(result.model_name, "gemini-test-001")
        self.assertEqual(result.prompt_version, PromptBuilder.version)
        self.assertEqual(result.suggested_score, 3.5)
        self.assertEqual((result.input_tokens, result.output_tokens), (120, 43))
        self.assertIsNone(provider.estimated_cost(req))

        sent = json.loads(self.requests[0].content)
        context = json.loads(sent["contents"][0]["parts"][0]["text"])
        self.assertEqual(context["resolved"]["source_file"]["content"],
                         req.resolved.source_file.content)
        self.assertEqual(context["resolved"]["other_file_paths"],
                         list(req.resolved.other_file_paths))
        self.assertEqual(context["functional_test_feedback"],
                         list(req.functional_test_feedback))
        self.assertEqual(context["rubric"]["criteria_text"], req.rubric.criteria_text)
        self.assertEqual(sent["systemInstruction"]["parts"][0]["text"],
                         PromptBuilder.instructions)
        config = sent["generationConfig"]
        self.assertEqual(config["responseMimeType"], "text/plain")
        self.assertEqual(config["maxOutputTokens"], 4096)
        self.assertNotIn("responseJsonSchema", config)
        self.assertNotIn("responseSchema", config)
        self.assertNotIn("tools", sent)

    def test_builds_and_sends_the_same_prepared_prompt_once(self):
        builder = PromptBuilder()
        builder.instructions += "Custom local instruction.\n"
        builder.version = "custom-version"
        req = sample_request()
        expected = builder.prepare(req)
        with patch.object(builder, "prepare", wraps=builder.prepare) as prepare:
            result = self.provider(completed(), builder=builder).generate_feedback(req)
        prepare.assert_called_once_with(req)
        sent = json.loads(self.requests[0].content)
        self.assertEqual(sent["contents"][0]["parts"][0]["text"], expected.context)
        self.assertEqual(sent["systemInstruction"]["parts"][0]["text"],
                         expected.system_instruction)
        self.assertEqual(result.prompt_version, expected.version)

    def test_rejects_oversized_actual_prompt_and_diagnostics_before_network(self):
        req = sample_request()
        builder = PromptBuilder(max_context_chars=2_000)
        builder.instructions = "x" * 2_001
        cases = [
            (req, builder),
            (replace(req, functional_test_feedback=("long",)),
             PromptBuilder(max_feedback_chars=3)),
            (replace(req, functional_test_feedback=("abc", "def")),
             PromptBuilder(max_target_feedback_chars=5)),
        ]
        for request, current_builder in cases:
            with self.subTest(builder=current_builder):
                provider = self.provider(completed(), builder=current_builder)
                with self.assertRaises(GradingInputError):
                    provider.generate_feedback(request)
                self.assertEqual(self.requests, [])

    def test_injected_client_uses_adapter_timeout(self):
        self.provider(completed(), timeout_seconds=12.5).generate_feedback(sample_request())
        timeouts = self.requests[0].extensions["timeout"]
        self.assertEqual(timeouts["read"], 12.5)

    def test_optional_zero_decimal_and_maximum_scores_are_valid(self):
        for value, score in (("NONE", None), ("0", 0), ("3.25", 3.25), ("5", 5)):
            for newline in ("\n", "\r\n"):
                with self.subTest(value=value, newline=newline):
                    result = self.provider(completed(
                        f"SCORE: {value}{newline}Evidence is limited.",
                    )).generate_feedback(sample_request())
                    self.assertEqual(result.suggested_score, score)
                    self.assertEqual(result.feedback_text, "Evidence is limited.")

    def test_feedback_remains_text_without_interpreting_its_contents(self):
        feedback = 'Inspect solution.py.\n\nQuoted output: {"x": "<tag>"}\nπ\nSCORE: 0'
        result = self.provider(completed(f"SCORE: 3.5\n{feedback}")).generate_feedback(
            sample_request()
        )
        self.assertEqual(result.feedback_text, feedback)
        self.assertEqual(result.suggested_score, 3.5)

    def test_score_is_finite_even_without_a_target_maximum(self):
        req = sample_request()
        req = replace(req, target=replace(req.target, max_score=None))
        result = self.provider(completed("SCORE: 100\nHint.")).generate_feedback(req)
        self.assertEqual(result.suggested_score, 100)
        with self.assertRaises(ProviderError) as caught:
            self.provider(completed("SCORE: " + "9" * 400 + "\nHint.")).generate_feedback(req)
        self.assertEqual(caught.exception.code, "invalid_provider_response")

    def test_optional_usage_and_model_version_remain_honest(self):
        payload = completed()
        del payload["usageMetadata"]
        del payload["modelVersion"]
        result = self.provider(payload).generate_feedback(sample_request())
        self.assertEqual(result.model_name, "gemini-test")
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)

    def test_does_not_score_blocked_or_incomplete_responses(self):
        cases = [
            ({"promptFeedback": {"blockReason": "SAFETY"}}, "provider_refusal"),
            (completed(finish_reason="SAFETY"), "provider_refusal"),
            (completed(finish_reason="MAX_TOKENS"), "incomplete_provider_response"),
            (completed(finish_reason="OTHER"), "incomplete_provider_response"),
            ({"candidates": []}, "invalid_provider_response"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ProviderError) as caught:
                    self.provider(payload).generate_feedback(sample_request())
                self.assertEqual(caught.exception.code, code)

    def test_rejects_invalid_feedback_without_returning_zero(self):
        invalid = [
            "SCORE: 1\n ",
            "SCORE: 1\n" + "x" * (MAX_FEEDBACK_CHARS + 1),
            *[f"SCORE: {value}\nHint." for value in (
                "-1", "6", "True", '"3"', "nan", "inf", "1e0", "3/5", "null", "",
            )],
            "SCORE: 1",
            "SCORE: NONE\n",
            "Hint without a score line.",
            "```\nSCORE: 1\nHint.\n```",
            '{"feedback_text": "Hint.", "suggested_score": 1}',
        ]
        for feedback in invalid:
            with self.subTest(feedback=feedback):
                with self.assertRaises(ProviderError) as caught:
                    self.provider(completed(feedback)).generate_feedback(sample_request())
                self.assertEqual(caught.exception.code, "invalid_provider_response")
                self.assertFalse(caught.exception.retryable)

    def test_no_text_or_missing_score_line_is_an_error(self):
        for parts in ([], [{"text": "Missing score line"}], [{"text": "thinking", "thought": True}]):
            payload = completed()
            payload["candidates"][0]["content"]["parts"] = parts
            with self.subTest(parts=parts):
                with self.assertRaises(ProviderError) as caught:
                    self.provider(payload).generate_feedback(sample_request())
                self.assertEqual(caught.exception.code, "invalid_provider_response")

    def test_joins_text_parts_and_excludes_thoughts(self):
        payload = completed()
        payload["candidates"][0]["content"]["parts"] = [
            {"text": "SCORE: 999\nInternal reasoning.", "thought": True},
            {"text": "SCORE: 2."},
            {"text": "5\nCheck the empty input case."},
        ]
        result = self.provider(payload).generate_feedback(sample_request())
        self.assertEqual(result.suggested_score, 2.5)
        self.assertEqual(result.feedback_text, "Check the empty input case.")

    def test_network_failures_are_sanitized_and_retryable(self):
        for error, code in (
            (httpx.ReadTimeout, "timeout"),
            (httpx.ConnectError, "provider_unavailable"),
        ):
            with self.subTest(error=error):
                with self.assertRaises(ProviderError) as caught:
                    self.provider({}, transport_error=error).generate_feedback(sample_request())
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(caught.exception.attempts, 1)
                self.assertNotIn("sensitive upstream detail", str(caught.exception))
                self.assertEqual(len(self.requests), 1)

    def test_api_error_retryability_and_sanitization(self):
        for status, retryable in ((400, False), (401, False), (429, True), (503, True)):
            with self.subTest(status=status):
                with self.assertRaises(ProviderError) as caught:
                    self.provider(
                        {"error": {"code": status, "message": "sensitive upstream detail"}},
                        status=status,
                    ).generate_feedback(sample_request())
                self.assertEqual(caught.exception.code, "provider_error")
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn("sensitive upstream detail", str(caught.exception))
                self.assertEqual(len(self.requests), 1)

    def test_configuration_and_client_ownership(self):
        for kwargs in ({"model": " "}, {"model": "test", "timeout_seconds": 0},
                       {"model": "test", "max_output_tokens": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                GeminiProvider(**kwargs)
        owned_client = Mock()
        with patch("ai_grading.providers.gemini.genai.Client", return_value=owned_client) as ctor:
            provider = GeminiProvider("test", api_key="test-key", timeout_seconds=12.5)
            options = ctor.call_args.kwargs["http_options"]
            self.assertEqual(options.timeout, 12500)
            self.assertEqual(options.retry_options.attempts, 1)
            provider.close()
            owned_client.close.assert_called_once()
        injected_client = Mock()
        GeminiProvider("test", client=injected_client).close()
        injected_client.close.assert_not_called()

    def test_local_configuration_and_overrides(self):
        self.config_path.write_text(json.dumps({"model": "local-model", "api_key": "local-key"}))
        with patch("ai_grading.providers.gemini.genai.Client") as ctor:
            provider = GeminiProvider()
            self.assertEqual(provider.model, "local-model")
            self.assertEqual(ctor.call_args.kwargs["api_key"], "local-key")
            provider.close()
            with patch.dict(os.environ, {"GEMINI_MODEL": "env-model", "GEMINI_API_KEY": "env-key"}):
                provider = GeminiProvider()
                self.assertEqual(provider.model, "env-model")
                self.assertEqual(ctor.call_args.kwargs["api_key"], "env-key")
                provider.close()
                provider = GeminiProvider("explicit-model", api_key="explicit-key")
                self.assertEqual(provider.model, "explicit-model")
                self.assertEqual(ctor.call_args.kwargs["api_key"], "explicit-key")
                provider.close()

    def test_missing_configuration_fails_before_client_creation(self):
        with patch("ai_grading.providers.gemini.genai.Client") as ctor:
            with self.assertRaisesRegex(ValueError, "model must be configured"):
                GeminiProvider()
            with self.assertRaisesRegex(ValueError, "API key must be configured"):
                GeminiProvider("test")
            ctor.assert_not_called()

    def test_injected_client_receives_one_attempt_per_provider_call(self):
        client = Mock()
        client.models.generate_content.return_value = types.GenerateContentResponse.model_validate(
            completed()
        )
        GeminiProvider("test", client=client, timeout_seconds=9).generate_feedback(
            sample_request()
        )
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.http_options.timeout, 9_000)
        self.assertEqual(config.http_options.retry_options.attempts, 1)


if __name__ == "__main__":
    unittest.main()

"""Exercise in-run retries with scripted providers and no network or waiting."""

import unittest
from dataclasses import replace

from ai_grading.engine import GradingEngine, RetryExhaustedError, RetryPolicy
from ai_grading.examples import sample_request
from ai_grading.models import GradingInputError
from ai_grading.providers import FakeProvider
from ai_grading.providers.base import ProviderError


class ScriptedProvider:
    """Test-only boundary stub; each call consumes one response or exception."""

    name = 'SCRIPTED'
    model = 'test'

    def __init__(self, *steps):
        self.steps = iter(steps)
        self.requests = []

    def generate_feedback(self, request):
        self.requests.append(request)
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step

    def estimated_cost(self, request):
        return None


class EngineTests(unittest.TestCase):
    def test_transient_failure_then_success_only_retries_failing_provider(self):
        req = sample_request()
        first = FakeProvider('First completed.', 1)
        response = FakeProvider('Second completed.', 2).generate_feedback(req)
        second = ScriptedProvider(ProviderError('rate_limit', 'Try again.', retryable=True), response)
        waits = []
        result = GradingEngine([first, second], sleep=waits.append).grade(req)
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(second.requests, [req, req])
        self.assertEqual(waits, [0.5])
        self.assertEqual(len(result.responses), 2)
        self.assertEqual(result.comparison.score_spread, 1)

    def test_retry_exhaustion_is_terminal_and_backoff_is_capped(self):
        req = sample_request()
        provider = ScriptedProvider(*[
            ProviderError('provider_unavailable', 'Unavailable.', retryable=True)
            for _ in range(4)
        ])
        waits = []
        engine = GradingEngine([provider], sleep=waits.append, retry_policy=RetryPolicy(
            max_attempts=4, initial_delay=1, multiplier=3, max_delay=2))
        with self.assertRaises(RetryExhaustedError) as caught:
            engine.grade(req)
        self.assertEqual(waits, [1, 2, 2])
        self.assertEqual(len(provider.requests), 4)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.attempts, 4)
        self.assertEqual(caught.exception.last_error_code, 'provider_unavailable')

    def test_deterministic_or_input_errors_never_retry(self):
        for error in (ProviderError('provider_refusal', 'Refused.'),
                      ProviderError('invalid_provider_response', 'Malformed.'),
                      GradingInputError('Context too long.')):
            provider = ScriptedProvider(error)
            waits = []
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                GradingEngine([provider], sleep=waits.append).grade(sample_request())
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(waits, [])

    def test_nonretryable_error_after_transient_error_stops_immediately(self):
        provider = ScriptedProvider(
            ProviderError('rate_limit', 'Wait.', retryable=True),
            ProviderError('provider_refusal', 'Refused.'),
        )
        waits = []
        with self.assertRaises(ProviderError) as caught:
            GradingEngine([provider], sleep=waits.append).grade(sample_request())
        self.assertEqual(caught.exception.code, 'provider_refusal')
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(waits, [0.5])

    def test_one_attempt_policy_does_not_sleep(self):
        provider = ScriptedProvider(ProviderError('timeout', 'Timed out.', retryable=True))
        waits = []
        with self.assertRaises(RetryExhaustedError):
            GradingEngine([provider], retry_policy=RetryPolicy(max_attempts=1),
                          sleep=waits.append).grade(sample_request())
        self.assertEqual(waits, [])

    def test_result_validation_does_not_retry_or_turn_failure_into_zero(self):
        req = sample_request()
        response = FakeProvider().generate_feedback(req)
        for changes in ({'rubric_id': 'wrong-rubric'}, {'suggested_score': 100}):
            provider = ScriptedProvider(replace(response, **changes))
            waits = []
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                GradingEngine([provider], sleep=waits.append).grade(req)
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(waits, [])

    def test_invalid_result_after_retry_reports_actual_attempt_count(self):
        req = sample_request()
        invalid = replace(FakeProvider().generate_feedback(req), rubric_id='wrong-rubric')
        provider = ScriptedProvider(
            ProviderError('rate_limit', 'Wait.', retryable=True), invalid,
        )
        waits = []
        with self.assertRaises(ProviderError) as caught:
            GradingEngine([provider], sleep=waits.append).grade(req)
        self.assertEqual(caught.exception.code, 'invalid_response')
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(waits, [0.5])

    def test_invalid_retry_configuration_is_rejected(self):
        cases = ({'max_attempts': 0}, {'max_attempts': True}, {'max_attempts': 1.5},
                 {'initial_delay': -1}, {'initial_delay': float('nan')},
                 {'multiplier': 0.5}, {'multiplier': float('inf')},
                 {'max_delay': True}, {'max_delay': -1})
        for config in cases:
            with self.subTest(config=config), self.assertRaises(ValueError):
                RetryPolicy(**config)
        with self.assertRaises(ValueError):
            GradingEngine([])


if __name__ == '__main__':
    unittest.main()

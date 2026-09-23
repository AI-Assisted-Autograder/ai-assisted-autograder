"""Check scenario evidence preservation and the user-facing demo commands."""

import json
import subprocess
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

from ai_grading.engine import GradingEngine
from ai_grading.examples import SCENARIOS
from ai_grading.prompt_builder import PromptBuilder
from ai_grading.providers import FakeProvider


class ScenarioTests(unittest.TestCase):
    def test_every_scenario_preserves_its_context_and_returns_deterministic_feedback(self):
        for name, request_factory in SCENARIOS.items():
            with self.subTest(scenario=name):
                request = request_factory()
                provider = FakeProvider()
                result = GradingEngine([provider]).grade(request)
                response = result.selected
                context = json.loads(provider.prompts[0].split(
                    "\nGRADING_CONTEXT_JSON:\n", 1
                )[1])
                self.assertEqual(context["resolved"]["source_file"],
                                 asdict(request.resolved.source_file))
                self.assertEqual(context["resolved"]["other_file_paths"],
                                 list(request.resolved.other_file_paths))
                self.assertEqual(context["functional_test_feedback"],
                                 list(request.functional_test_feedback))
                self.assertEqual(context["target"], asdict(request.target))
                self.assertEqual(context["feedback_guidance"], request.rubric.feedback_guidance)
                self.assertEqual(response.rubric_id, request.rubric.id)
                self.assertIsNone(response.suggested_score)
                self.assertEqual(result.responses, (response,))
                self.assertEqual(result, GradingEngine([provider]).grade(request))

    def test_cross_file_fixture_exposes_one_body_and_names_the_unseen_helper(self):
        request = SCENARIOS["multi-file"]()
        self.assertEqual(request.resolved.source_file.path, "report.py")
        self.assertEqual(request.resolved.other_file_paths, ("parsing.py",))
        self.assertIn("test_average failed", request.functional_test_feedback[0])
        self.assertIn("test_empty_field failed", request.functional_test_feedback[1])
        fake = FakeProvider()
        GradingEngine([fake]).grade(request)
        self.assertNotIn("def parse_numbers", fake.prompts[0])
        self.assertIn("their contents were not supplied", fake.prompts[0])

    def test_passing_style_fixture_can_receive_feedback(self):
        request = SCENARIOS["passing-style"]()
        self.assertTrue(request.functional_test_feedback)
        self.assertTrue(all("passed:" in note for note in request.functional_test_feedback))
        provider = FakeProvider()
        response = GradingEngine([provider]).grade(request).selected
        self.assertEqual(len(provider.requests), 1)
        self.assertIn("no code evaluation", response.feedback_text)

    def test_error_skip_fixture_has_no_pass_fail_evidence_or_assigned_score(self):
        request = SCENARIOS["error-skip"]()
        self.assertIn("ERROR", request.functional_test_feedback[0])
        self.assertIn("SKIP", request.functional_test_feedback[1])
        response = GradingEngine([FakeProvider()]).grade(request).selected
        self.assertIsNone(response.suggested_score)


class DemoCommandTests(unittest.TestCase):
    def run_demo(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "ai_grading.demo", *args],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_default_and_each_named_scenario_run(self):
        for name in (None, *SCENARIOS):
            with self.subTest(scenario=name):
                args = () if name is None else ("--scenario", name)
                result = self.run_demo(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"=== SCENARIO: {name or 'empty-list'} ===", result.stdout)
                self.assertEqual(result.stdout.count("=== FAKE GRADING RESULT ==="), 1)
                response_json = result.stdout.split("=== FAKE GRADING RESULT ===\n", 1)[1]
                response = json.loads(response_json)
                self.assertEqual(response["selected"]["provider"], "FAKE")
                self.assertEqual(response["selected"]["prompt_version"], PromptBuilder.version)
                self.assertEqual(response["responses"], [response["selected"]])

    def test_list_only_lists_and_all_runs_each_scenario(self):
        listing = self.run_demo("--list-scenarios")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(listing.stdout.splitlines(), list(SCENARIOS))
        result = self.run_demo("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("=== FAKE GRADING RESULT ==="), len(SCENARIOS))
        for name in SCENARIOS:
            self.assertEqual(result.stdout.count(f"=== SCENARIO: {name} ==="), 1)

    def test_invalid_or_conflicting_selection_fails_before_generation(self):
        for args in (("--scenario", "missing"), ("--all", "--scenario", "empty-list")):
            with self.subTest(args=args):
                result = self.run_demo(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()

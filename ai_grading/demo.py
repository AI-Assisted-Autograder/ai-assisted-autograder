"""Run with python3 -m ai_grading.demo from the repository root."""

import argparse
import json
from dataclasses import asdict

from .engine import GradingEngine
from .examples import SCENARIOS
from .providers import FakeProvider


def run_scenario(name: str) -> None:
    request = SCENARIOS[name]()
    fake = FakeProvider()
    result = GradingEngine([fake]).grade(request)
    print(f"=== SCENARIO: {name} ===")
    print("=== LOCAL REQUEST ===")
    print(json.dumps(asdict(request), indent=2, ensure_ascii=False))
    print("\n=== PROMPT ===")
    print(fake.prompts[-1])
    print("\n=== FAKE GRADING RESULT ===")
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run offline AI feedback examples.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--scenario", choices=SCENARIOS,
                           help="Example to run (default: empty-list).")
    selection.add_argument("--all", action="store_true", help="Run all examples.")
    selection.add_argument("--list-scenarios", action="store_true",
                           help="List examples without generating feedback.")
    args = parser.parse_args(argv)
    if args.list_scenarios:
        print("\n".join(SCENARIOS))
        return
    names = tuple(SCENARIOS) if args.all else (args.scenario or "empty-list",)
    for index, name in enumerate(names):
        if index:
            print()
        run_scenario(name)


if __name__ == "__main__":
    main()

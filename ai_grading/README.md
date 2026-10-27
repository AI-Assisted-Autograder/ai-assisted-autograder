# AI grading (Student A)

Python library for generating feedback on one grading target at a time.
The grading core, three LLM provider adapters, FakeProvider, and the
deterministic `ResultsPackager` are implemented. FakeProvider returns fixed
feedback without API calls for offline integration. The packager preserves the
instructor's functional-test entries and appends AI feedback as zero-point
entries; it does not call an LLM or decide scores.

## Setup

Install dependencies before importing the `ai_grading` module.
Run these commands from the repository root, using the same Python environment as the program calling it.

```bash
python3 -m venv .venv
source .venv/bin/activate  # .venv\Scripts\activate on Windows
python -m pip install -r ai_grading/requirements.txt
```

## Configure a provider

A Gemini free-tier API is available for testing through
[`.gemini.json`](ILLMProvider/.gemini.json). `GeminiProvider()` loads the model and API key from this file, so the example below needs no environment variables.

To use other credentials, set the model and API key:

| Provider | Model variable | API key variable |
| --- | --- | --- |
| OpenAI | `OPENAI_MODEL` | `OPENAI_API_KEY` |
| Claude | `ANTHROPIC_MODEL` | `ANTHROPIC_API_KEY` |
| Gemini | `GEMINI_MODEL` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |

## Call from Python

For container deployment, include the `ai_grading/` source folder and make it importable by the Python entrypoint.
Prepare a [`FeedbackRequest`](models.py) with `target` (the item to grade),
`rubric` (grading rules), `resolved` (source contents and other file paths),
and `functional_test_feedback` (messages from the instructor's tests, in reporting order).
Use [`FileResolver`](FileResolver.py) to read the source:
`FileResolver().resolve(identifier, kind, language, submission_dir)` returns `resolved`.

```python
from ai_grading.AIGradingComponent import AIGradingComponent
from ai_grading.ILLMProvider.GeminiProvider import GeminiProvider
from ai_grading.models import FeedbackRequest

def grade_target(request: FeedbackRequest):
    provider = GeminiProvider()
    try:
        return AIGradingComponent([provider]).grade(request)
    finally:
        provider.close()
```

Call `result = grade_target(request)`. Read `result.selected.feedback_text` for
feedback and `result.selected.suggested_score` for the optional score.
The caller handles fetching backend data, saving results and updating submission state.

## ResultsPackager

`ResultsPackager` reads the existing `results.json` object and provides three
deterministic updates:

```python
from ai_grading.ResultsPackager import FeedbackItem, FeedbackPackage, ResultsPackager

packager = ResultsPackager()
results = packager.parse("results.json")
results = packager.splice(
    results,
    FeedbackPackage((FeedbackItem("add", "Use a clearer variable name."),)),
)
packager.write("results.json", results)
```

`append_deferred` and `append_error` cover the non-GRADE paths. The original
functional-test entries are copied unchanged, and writes use a temporary file
followed by an atomic replace.

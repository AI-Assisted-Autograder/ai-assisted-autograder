# AI grading

The module is delivered as a Python library. The Gradescope container entrypoint
calls `AIGradingComponent.grade(request)` directly.

`api.py` and `/test/grade` are local testing tools, outside the production delivery
scope. The container does not need to start uvicorn to use this module.

## Structure

```text
ai_grading/
  AIGradingComponent.py   # Grading, retries and result selection
  FileResolver.py         # Read the target source from a submission directory
  models.py               # FeedbackRequest and GradingResult
  PromptBuilder.py
  ILLMProvider/           # Interface, three real adapters and FakeProvider
  api.py                  # Local HTTP testing only
  demo.py
  tests/
  requirements.txt
```

## Setup

Run from the repository root:

```bash
source .venv/bin/activate
python -m pip install -r ai_grading/requirements.txt
```

## Fake entrypoint

Pass a prepared `FeedbackRequest` as `request`. Fake returns fixed feedback
without API keys or charges:

```python
from ai_grading.AIGradingComponent import AIGradingComponent
from ai_grading.ILLMProvider import FakeProvider

result = AIGradingComponent([FakeProvider()]).grade(request)
```

For an offline example with supplied inputs, run `python -m ai_grading.demo`.

## Real entrypoint

Set the selected provider's model and key:

| Provider class | Model | API key |
| --- | --- | --- |
| `GeminiProvider` | `GEMINI_MODEL` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `OpenAIProvider` | `OPENAI_MODEL` | `OPENAI_API_KEY` |
| `ClaudeProvider` | `ANTHROPIC_MODEL` | `ANTHROPIC_API_KEY` |

For example, after setting the OpenAI variables:

```python
from ai_grading.AIGradingComponent import AIGradingComponent
from ai_grading.ILLMProvider.OpenAIProvider import OpenAIProvider

provider = OpenAIProvider()
try:
    result = AIGradingComponent([provider]).grade(request)
finally:
    provider.close()
```

Use the corresponding provider class for Gemini or Claude. Gemini also supports
`ILLMProvider/.gemini.json`.

## Optional local HTTP testing

For local testing, start the harness and use `/docs` to send a `FeedbackRequest`:

```bash
LLM_PROVIDER=fake python -m uvicorn ai_grading.api:create_app --factory --port 8001
```

Schema: `http://127.0.0.1:8001/docs`. This harness is separate from the backend
Web API accessed through `IGradingClient` in the production flow below.

## Future Gradescope integration

The module runs inside the Gradescope container. Assignment configuration,
rubrics and test feedback come through B's `IGradingClient`; source files come
from the container's submission directory. Only the backend accesses the DB.

The container entrypoint will connect the flow:

1. Call `get_action()` and handle `GRADE`, `DEFER` or `REEMIT`.
2. For `GRADE`, resolve the source with `FileResolver` and combine it with the
   configuration to build `FeedbackRequest`, then call `AIGradingComponent.grade()`.
3. Send results with `post_feedback()` or errors with `mark_ai_failed()`, and
   call `mark_ai_complete()` when the targets are finished.
4. Use `ResultsPackager` to splice feedback into the existing `results.json`.

The current Python grading entrypoint receives a complete
`FeedbackRequest`. HTTP calls through `IGradingClient` are part of the production
integration; they fetch and save backend data, not invoke the local `/test/grade` route.
See [the interface diagram](../docs/architecture/07-interfaces.puml) for the contracts.

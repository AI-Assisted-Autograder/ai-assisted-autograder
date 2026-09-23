# AI grading

No DB access or `results.json` writes now 

## Install dependencies

From the repository root, install the AI grading dependencies with the same
Python you will use to run the commands below:

```bash
python -m pip install fastapi uvicorn httpx google-genai
```

The repository does not currently have a dependency manifest for `ai_grading`.
`python -m pip` installs into the Python selected by `python`.

## Integration

- Input: `FeedbackRequest` (`models.py`) with target, rubric, resolved source,
  and ordered test feedback. Use `FileResolver`; see `examples/request.json`.
- Output: `GradingResult.selected` has feedback and an optional score;
  `responses` contains all provider results.
- Errors: invalid input raises validation errors or `GradingInputError`;
  model failures raise `ProviderError`.

```python
from ai_grading.engine import GradingEngine
from ai_grading.providers.gemini import GeminiProvider

result = GradingEngine([GeminiProvider()]).grade(request)
```

`POST /test/grade` is a local HTTP test endpoint; the Web app is not connected.

## Offline testing

From the repository root:

```bash
python -m ai_grading.demo --all
python -m unittest discover -s ai_grading/tests -v
```

The demo returns fixed `FakeProvider` feedback. Tests use fake providers
or mock Gemini's HTTP response; neither calls Gemini's API.

## Run with Gemini API

Set `GEMINI_MODEL` and `GEMINI_API_KEY`, then start the local API:

```bash
LLM_PROVIDER=gemini python -m uvicorn ai_grading.api:create_app --factory --host 127.0.0.1 --port 8001
```

From another terminal in the repo root:

```bash
curl -sS http://127.0.0.1:8001/test/grade -H 'Content-Type: application/json' \
  --data-binary @ai_grading/examples/request.json
```

This calls Gemini; `selected.provider` should be `GOOGLE`. Config may also come from `ai_grading/providers/.gemini.json`.

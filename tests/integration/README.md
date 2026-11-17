# Integration test suite

These tests exercise **production components across their boundaries**. The directory
name is not the criterion: each suite composes components that are separately
tested in `tests/unit/`, and checks the data or behavior that crosses between them.

## Coverage

| Suite | Real components exercised | Controlled boundary |
|---|---|---|
| `ai_grading_workflow/` | `FileResolver` -> `PromptBuilder` -> `AIGradingComponent` -> `ResultsPackager` | Deterministic `FakeProvider` instead of a paid/network LLM |
| `typescript/nightly-workflow.integration.test.ts` | `NightlyTrigger` -> `HttpGradingClient`, including request serialization, authorization ordering, audit writes and completion | Local HTTP API double; `FakePlatform` replaces live Gradescope/browser access |
| `web_api/` | FastAPI internal nightly endpoints -> SQLAlchemy session -> PostgreSQL constraints and persisted `TriggerRun` | Dedicated PostgreSQL test database |

These are not unit tests: assertions are made after data has crossed multiple
production components. They are not browser E2E tests: no live Gradescope account,
browser session, or production LLM is needed.

## Run

From the repository root:

```bash
# AI grading workflow integration
python -m pytest tests/integration/ai_grading_workflow -v

# Web API + PostgreSQL integration (requires a dedicated database)
export TEST_DATABASE_URL='postgresql+psycopg://autograder:autograder@localhost:5432/autograder_test'
python -m pytest tests/integration/web_api -v

# TypeScript nightly orchestration + HTTP client integration
npm run test:integration:ts
```

Install Python dependencies with `pip install -r tests/requirements.txt`.
The TypeScript suite uses the existing `vitest` dev dependency.

The web API suite skips when `TEST_DATABASE_URL` is absent or the database
cannot be reached. It refuses to use the app's default database URL and also
requires the database name to contain `test`. Create a disposable database
before running it. The suite creates missing tables but does not drop the
schema; it removes its nightly audit rows after the test.

## Scope and deliberate exclusions

- No test calls a real LLM provider or spends model credits.
- No test clicks a real Gradescope rerun button or uses instructor credentials.
- The TypeScript HTTP test uses an in-process HTTP server, not mocked `fetch`;
  the real `HttpGradingClient` sends requests through Node's HTTP stack.
- The PostgreSQL test uses the actual FastAPI routes, SQLAlchemy models and
  configured database engine. It verifies auth, the single-running-run
  constraint, and persisted run completion.
- Existing `tests/e2e/` tests remain the place for live browser/login journeys.
- Existing `integration/` source modules are production code, not the test suite.

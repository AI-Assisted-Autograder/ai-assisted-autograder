# AI Grading

The active flow is Gradescope container → Web backend → configured AI provider →
Gradescope feedback. PostgreSQL owns assignment, target, rubric and model
configuration. Provider credentials stay on the backend; the container uses an
assignment token and a backend URL.

For credentials, token setup and deployment, see the
[Web AI feedback guide](../web/README.md#ai-feedback-api-database-configuration).

## Current flow

1. After functional grading, `gradescope_feedback.py` reads this assignment's
   collection rules and configuration revision from `GET /v1/assignment/config`.
2. The hook collects the configured source files and Gradescope, JUnit, JaCoCo or
   text evidence, then sends each target to `POST /v1/feedback`. Gradescope evidence
   respects test visibility and explicit pass/fail status.
3. The backend authenticates the assignment token, checks the configuration
   revision and input scope, reserves call quota, and invokes the DB-selected
   provider with the current rubric.
4. The hook adds returned feedback and configured AI scores to `results.json`.
   Functional scores are preserved when AI feedback is unavailable. Teacher
   evidence is retained separately in `results/ai_evidence.json` and logged only
   when Gradescope's `stdout_visibility` is `hidden` (the default).

This path does not write submissions, test results or feedback to PostgreSQL and
is not connected to nightly scheduling. Backend JSON stores token hashes, call
quota and replay responses.

| Engine | Backend credentials | Adapter |
| --- | --- | --- |
| `vertex` | Impersonation ADC | `VertexProvider`, using `GeminiProvider` |
| `pitt` | `PITT_API_KEY` | `PittProvider`, using the full project-authorized `@.../...` model ID |
| `gemini_api` | `GEMINI_API_KEY` | `GeminiProvider` |

## Configure a package

Create the assignment, add targets and save their rubrics through the Web forms,
or use the protected assignment API. The target configuration page also edits
source/context patterns, analysis mode, testplan, expected test IDs, per-test
points and evidence sources. Existing DB-injected configurations use the same
reader. Targets without a current rubric are excluded from AI execution.

For basic targets, FILE identifiers are submission-relative globs; Java and
Python CLASS/METHOD identifiers use [FileResolver](FileResolver.py)'s conventions.
Explicit source patterns support custom layouts and other languages. Report paths
are relative to `/autograder`.

Backend-connected packages in [autograders/](../autograders/) contain their own
client/hook copies, `config.json` with only `backend_url`, and a private
`assignment.token`. They do not carry a duplicate `ai_feedback.json`. Instructors
add the reusable files and hook to their functional grader; Web ZIP injection is
not implemented. Keep credentials and generated upload ZIPs outside Git.

The connector hashes input content and the configuration revision into its
request ID. Unchanged inputs/configuration reuse completed results. Rubric,
analysis, model or prompt-version changes use a new revision on the next run,
without repackaging. New requests with a stale revision are rejected before
reserving quota; accepted requests can still be polled or replayed after a
configuration edit, subject to current assignment and target access. Use
`gradescope_feedback.py --run-id NEW_ID` for an independent model trial; reuse the
same ID for retries.

## Use the backend

`local_client.py` uses only the Python standard library. From the repository root:

```bash
python3 -I -S ai_grading/local_client.py \
  --backend-url https://YOUR-SERVER \
  --token-file /private/path/assignment.token \
  --files-json /absolute/path/files.json \
  --target-id YOUR_TARGET_UUID --json
```

`--files-json` accepts an array of `{path, content}` objects; `--code-file` accepts
one source file as legacy text input. `--evidence-json` is optional. The target ID
is the DB UUID, not the Gradescope assignment number or filename. Connections use
HTTPS, or HTTP on loopback for local development.

The direct CLI does not fetch a configuration revision. Supply a new
`--request-id` after changing grading configuration or for each independent trial;
reuse it only for the same request. Programmatic callers can use
`get_assignment_config()` and pass its revision to `request_feedback()`.

The client requests asynchronous processing and polls the same POST request after
HTTP 202 responses. Polling does not reserve another call. Default budgets are
240 seconds for backend generation, 270 seconds per client request, and 300
seconds for the entire hook. Backend request/output limits are initialized with
`seed_feedback`; the default output limit is 16,384 tokens. Failed or timed-out
provider calls remain charged to the call quota. Pitt requests bypass gateway
cache; backend replay still applies.

## RentACat feedback

- **Playwright:** 11 testplan criteria at 5 AI points each. Application code
  derives scores from verdicts; insufficient evidence remains unscored. The
  configured primary source must match exactly one submitted file.
- **Java:** one overall, unscored request covering implementations and tests with
  functional results, JUnit reports, aggregate coverage and build diagnostics.
  Feedback is combined into one final `AI Feedback` entry.

Both packages run the AI hook after functional grading, including error paths.
Set Gradescope's Outline maximum to the intended grading scale; adding AI rows
does not change that maximum. Failure-only inline feedback and multi-run voting
are not implemented.

## Module layout

| Location | Responsibility |
| --- | --- |
| [local_client.py](local_client.py) | Assignment-token config/feedback HTTP client and CLI |
| [gradescope_feedback.py](gradescope_feedback.py) | Runtime evidence collection and Gradescope rendering |
| [models.py](models.py) | Source, target, rubric, analysis and response contracts |
| [FileResolver.py](FileResolver.py) | Resolve target identifiers to source files |
| [PromptBuilder.py](PromptBuilder.py) | Build prompts from DB configuration and supplied evidence |
| [ILLMProvider/](ILLMProvider/) | Provider adapters, response parsing and offline `FakeProvider` |
| [AIGradingComponent.py](AIGradingComponent.py) | Optional direct-Python provider orchestration and selection |
| [ResultsPackager.py](ResultsPackager.py) | Optional zero-point feedback writer |
| [web/app/](../web/app/) | DB configuration, HTTP routes, authentication, quota and replay state |

For direct Python reuse, install `ai_grading/requirements.txt`, construct a
`FeedbackRequest`, and call a provider or `AIGradingComponent(providers).grade()`.
The component's default selector chooses the first provider; score spread is not
calibrated confidence. The current Web route calls its selected provider directly,
and the shared container hook does not use `ResultsPackager`.

## Deployment verification

Local checks do not establish deployed acceptance. Before using updated packages,
deploy the matching backend and verify Gradescope → backend authentication/quota
→ DB configuration → selected provider → Gradescope feedback on the target server.

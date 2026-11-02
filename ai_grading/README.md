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
2. The hook collects the configured source files and visible Gradescope evidence,
   then sends each target to `POST /v1/feedback`. Evidence respects test visibility
   and explicit pass/fail status; functional-test feedback selects failed rows only.
3. The backend authenticates the assignment token, checks the configuration
   revision and input scope, reserves call quota, and invokes the DB-selected
   provider with the current rubric.
4. AI grading adds one scored block per configured item. Functional-test feedback
   appends explanations inside the original failed tests without changing scores;
   when no eligible failures exist, the hook skips AI. Teacher evidence and errors
   are retained in `results/ai_evidence.json` and logged only when Gradescope's
   `stdout_visibility` is `hidden` (the default).

This path does not write submissions, test results or feedback to PostgreSQL and
is not connected to nightly scheduling. Backend JSON stores token hashes, call
quota and replay responses.

| Engine | Backend credentials | Adapter |
| --- | --- | --- |
| `vertex` | Impersonation ADC | `VertexProvider`, using `GeminiProvider` |
| `pitt` | `PITT_API_KEY` | `PittProvider`, using the full project-authorized `@.../...` model ID |
| `gemini_api` | `GEMINI_API_KEY` | `GeminiProvider` |

## Configure a package

Create the assignment through the Web form, select a target's behavior, and
continue to its full configuration page. It edits source/context patterns, public
assignment context (`test_plan`), evaluation focus, grading items, evidence sources,
background and feedback guidance. The protected creation API requires an explicit
`analysis.behavior`; both paths use the same DB configuration reader. Targets
without a current rubric are excluded from AI execution.

- **`ai_grading`:** each `grading_items` entry defines an ID, requirement and points.
  The item list is the sole scoring source; the total is derived from its points.
  Existing IDs remain stable when editing or reordering. Background and guidance
  provide shared context and presentation instructions.
- **`functional_test_feedback`:** configure exactly one final Gradescope source at
  `results/results.json`, selected by its label in `functional_result_source`.
  One active feedback target covers all eligible visible failed rows, without
  per-test configuration or points.

Both behaviors currently support Gradescope evidence only. JUnit, JaCoCo and text
sources are rejected. API and Web saves check the combined instructor prompt size;
student source and execution evidence remain subject to runtime limits.

For basic targets, FILE identifiers are submission-relative globs; Java and
Python CLASS/METHOD identifiers use [FileResolver](FileResolver.py)'s conventions.
Explicit source patterns support custom layouts and other languages. Report paths
are relative to `/autograder`.

Backend-connected packages in [autograders/](../autograders/) contain their own
client/hook copies, `config.json` with only `backend_url`, and a private
`assignment.token`. They do not carry a duplicate `ai_feedback.json`. Instructors
add the reusable files and hook to their functional grader; Web ZIP injection is
not implemented. Keep credentials and generated upload ZIPs outside Git.

The connector hashes source content, grading-relevant observations and the
configuration revision into its request ID. Runtime metadata such as
`execution_time` does not affect reuse. Unchanged inputs/configuration reuse
completed results. Rubric,
analysis, model or prompt-version changes use a new revision on the next run,
without repackaging. New requests with a stale revision are rejected before
reserving quota; accepted requests can still be polled or replayed after a
configuration edit, subject to current assignment and target access.

To request fresh AI results, select **Start a new AI attempt** in the configuration
form, save, and rerun the submission in Gradescope. This changes the assignment's
revision for all targets; it does not start grading automatically. Alternatively,
use `gradescope_feedback.py --run-id NEW_ID` for an independent trial. Reuse the
same run ID for retries.

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
HTTPS, or HTTP on loopback for local development. Functional-test feedback requests
require selected failed-row evidence, which the container hook collects automatically.

By default, the CLI fetches the current configuration revision before reading
input files and derives a request ID from the inputs and revision. Before sending
the request, it prints a retry pair to stderr:
`--request-id ORIGINAL_REQUEST_ID --config-revision ORIGINAL_REVISION`.
For a retry, append that exact pair to the original command and keep its inputs
unchanged. An explicit `--request-id` always requires `--config-revision`; it does
not fetch a newer revision.

For an independent trial, supply a new request ID with the revision used to prepare
the inputs. If configuration changed, fetch it again and prepare matching inputs.
Programmatic callers can use `get_assignment_config()` and pass the returned
revision to `request_feedback()`.

The client requests asynchronous processing and polls the same POST request after
HTTP 202 responses. Polling does not reserve another call. Default budgets are
240 seconds for backend generation, 270 seconds per client request, and 300
seconds for the entire hook. Backend request/output limits are initialized with
`seed_feedback`; the default output limit is 16,384 tokens. Failed or timed-out
provider calls remain charged to the call quota. Pitt requests bypass gateway
cache; backend replay still applies.

## RentACat feedback

- **Playwright:** preserves the prototype's rubric, test cases and 11 ordered
  criteria at 5 AI points each. Application code awards 5 for pass and 0 for fail;
  zero-point criteria keep their blocks. The primary source must match exactly one
  submitted file.
- **Java:** one functional-test feedback target explains eligible failed rows from
  the final Gradescope report inside those rows. It does not add an overall
  `AI Feedback` block or change functional scores. All-pass runs skip AI.

Insufficient evidence or model failure retains the affected grading items and
their full maxima as pending regrade. Their actual scores and the final AI total
remain unknown. Gradescope shows provisional zero placeholders and a provisional
combined grade until regrading completes.

Both packages run the AI hook after functional grading, including error paths.
Set Gradescope's Outline maximum to the intended grading scale; adding AI rows
does not change that maximum. Multi-run voting is not implemented.

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

Local checks do not establish deployed acceptance. Coordinate the matching backend,
new upload ZIPs and explicit assignment configuration migration before running the
new autograder. Targets without `analysis.behavior` are skipped by the new hook;
legacy `expected_tests`/`points_per_test` and unsupported evidence sources need
conversion. Backend startup and Alembic do not activate the new behaviors.

The existing migration command supports only the two reviewed RentACat assignments.
Run it explicitly in the deployed Web container during the upgrade:

```bash
python -m app.feedback_assignment --migrate-behavior --assignment-id ASSIGNMENT_UUID
```

It preserves assignment/target IDs, validates the original Playwright policy and
retains only supported Gradescope evidence (Java: the selected final report).
Other legacy assignments require their own configuration conversion. New Web/API
assignments also need an assignment token and quota registration as described in
the Web guide. Verify Gradescope → backend authentication/quota → DB configuration
→ selected provider → Gradescope feedback after deployment and migration.

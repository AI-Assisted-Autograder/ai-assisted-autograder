# AI Grading

The active flow is Gradescope container → Web backend → configured AI provider →
Gradescope feedback. PostgreSQL owns assignment, target, rubric and model
configuration. Provider credentials stay on the backend; the container uses an
assignment token and a backend URL.

For credentials, token setup and deployment, see the
[Web AI feedback guide](../web/README.md#ai-feedback-api-database-configuration).

## Current flow

- **Grading.** After functional grading, the hook sends one `POST /v1/grading/run`
  containing the original results, submission-relative files, run metadata and any
  configured additional Gradescope reports. Web records functional results, selects
  files and visible evidence for each target using one assignment configuration
  snapshot, grades the configured items and records the overall AI outcome. The
  response contains the complete Gradescope `results` and private `ai_evidence`.
- **Feedback.** The same hook reports functional results, fetches collection rules
  from `GET /v1/assignment/config`, and calls `POST /v1/feedback` when visible
  failed tests qualify. It appends explanations to those tests and reports completion.

Both modes apply assignment-token authentication, submission authorization and
per-target call quotas. Teacher evidence is written to `results/ai_evidence.json`.
The hook prints diagnostic details when Gradescope's `stdout_visibility` is `hidden`.

With submission metadata, PostgreSQL stores submission/target metadata, provider
feedback, mapped functional results and final AI outcomes. A normally deferred run
leaves the previous AI outcome in place until the authorized rerun. A run
with no eligible feedback reports `SUCCEEDED` with `completion_reason=not_needed`.
Automation authorizes each pending submission before triggering its rerun; the
assignment stays `NIGHTLY`. Student files are processed for the run. Backend JSON
retains quota, replay and cost accounting.

| Engine | Backend credentials | Adapter |
| --- | --- | --- |
| `vertex` | Impersonation ADC | `VertexProvider`, using `GeminiProvider` |
| `pitt` | `PITT_API_KEY` | `PittProvider`, using the full project-authorized `@.../...` model ID |
| `gemini_api` | `GEMINI_API_KEY` | `GeminiProvider` |

## Configure a package

Create the assignment through the Web form, select a target's behavior, and
continue to its full configuration page. It edits source/context patterns, public
assignment context (`test_plan`), evaluation focus, shared grading rubric, grading
items, evidence sources, background and feedback guidance. The protected creation
API requires an explicit `analysis.behavior`; both paths use the same DB configuration reader. Targets
without a current rubric are excluded from AI execution.

- **`ai_grading`:** each `grading_items` entry defines an ID, requirement and points.
  Optional `shared_grading_rules` applies to every item and appears once in the prompt.
  Shared rules and each item's requirement define its verdict; points come only from items.
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

Both modes use the same `local_client.py`, `gradescope_feedback.py` and hook.
The assignment ZIP supplies `backend_url`, `ai_mode` and a private `assignment.token`.
Grading packages list any additional Gradescope reports in `grading_report_paths`.
Download an updated package when adding a new report path. Web reads current
targets, rubrics and file globs on every run. Older packages without a mode
continue to use the configuration and per-target endpoints.

The connector generates a fresh run UUID for each execution, including a single
submission rerun and Regrade All. Request IDs include
this UUID, the submission ID when available, source content, grading-relevant
observations and the configuration revision. Targets, configuration refreshes and
HTTP polling share the same run UUID. `NIGHTLY` requests require valid submission
metadata to check authorization; `IMMEDIATE` local calls can omit it. Existing
instructor diagnostics record the run and request IDs. Deferred or inactive
responses end the hook's AI requests immediately, without polling or a model cost.

On a rerun, Web reuses the selected PostgreSQL result for a target when the
submission, target inputs and assignment revision match. A fully scored Grading
target or a completed Feedback explanation can be reused. Failed targets and
Grading targets with pending items get a fresh model call. The run still records
its functional results and overall AI outcome.

Use `gradescope_feedback.py --run-id ORIGINAL_RUN_ID` only to intentionally replay
the same submission, inputs and configuration. Runtime metadata such as
`execution_time` does not affect reuse. Rubric, analysis, model or prompt-version
changes use a new revision without repackaging. New requests with a stale revision
are rejected before reserving quota; accepted requests can still be polled or
replayed after a configuration edit, subject to current assignment and target
access. Only new provider calls consume quota. A reused result reports zero
tokens and zero additional cost for the current request.

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

The per-target client requests asynchronous processing and polls the same POST request after
HTTP 202 responses. Polling does not reserve another call. Default budgets are
240 seconds for backend generation, 270 seconds per client request, and 300
seconds for the entire hook. Backend request/output limits are initialized with
`seed_feedback`; the default output limit is 16,384 tokens. Failed or timed-out
provider calls remain charged to the call quota. Pitt requests bypass gateway
cache; backend replay still applies.

## Estimated API cost

The backend prices reported token usage once per request and persists it with
the request's model, run ID and price snapshot. The hidden autograder output
shows the current run's estimate. The assignment list shows each assignment's
cumulative estimate across submissions and reruns. Polling and intentional
request replay do not add cost. Functional scores are unaffected.

The assignment detail page defaults to the last 7 days. Its total and bar chart
cover the selected range, using the time each request started in UTC. The last
24 hours are grouped by hour, and the last 7 or 30 days by day. **All time**
starts with the assignment's first recorded request and groups costs by day,
week, or month according to the length of its history.

Prices are configured centrally in `config.model_prices` in the backend state
file. Each entry uses USD per million tokens and an effective date (UTC):

```json
{
  "engine": "pitt",
  "model": "@pitt-ai-connect-google-vertex/gemini-4-argon",
  "response_model": "gemini-4-argon",
  "effective_from": "2026-11-10",
  "input_per_million": "2.00",
  "cached_input_per_million": "0.10",
  "output_per_million": "10.00",
  "basis": "Pitt listed input/output (2026-11-10); Google Cloud public cache-read rates; no assumed future price changes"
}
```

The latest effective entry for the exact engine/model is selected when a request
is reserved. The actual response model must match `response_model`, including
the dated model versions returned by Azure OpenAI. Web startup and AI settings
saves fill missing Pitt model prices without overwriting custom price schedules.
Stop Web before editing its state file, preserve existing tokens, quota and
request records, then restart. Do not reinitialize state. Existing request
estimates keep their original prices.

Default input/output rates use the Pitt AI Connect price table supplied on
November 10, 2026. All amounts below are USD per million tokens:

| Model | Input | Cached input estimate | Output |
| --- | ---: | ---: | ---: |
| Gemini 4 Argon | 2.00 | 0.10 | 10.00 |
| Gemini 3.8 Flash | 0.75 | 0.075 | 3.75 |
| Claude Sonnet 5.5 | 2.00 | 0.10 | 10.00 |
| Claude Opus 5.5 | 4.00 | 0.20 | 20.00 |
| GPT-6.1 Sol | 2.00 | 0.10 | 10.00 |
| GPT-6 Luna | 0.10 | 0.01 | 0.50 |


GPT estimates also use the default 30-minute cache-write rates: $2.50 for Sol
and $0.125 for Luna. Reported reads and writes are subtracted from ordinary input
before applying their rates; output already includes reasoning tokens. Missing
required cache-write usage or an unconfigured write rate leaves cost unavailable.
The current connector does not request explicit Claude/Google cache creation.
These are reference estimates, not verified Pitt invoices; upstream tier, cache,
or storage charges may differ from Pitt billing.

No regional surcharge or future promotional-price increase is assumed for Pitt.
The previous untouched two-entry Argon default schedule is replaced on Web
startup/save; edited schedules and historical request snapshots are preserved.

Usage remains recorded when feedback parsing or grading fails. A provider
response arriving after the hook ends can update the Web total, but cannot
rewrite the finished autograder log. Missing usage, missing prices and older
requests without price snapshots are shown as unpriced, not zero-cost calls.
The Web pages show the tracking start and unpriced/pending request counts.

## Grading results

Insufficient evidence or model failure retains the affected grading items and
their full maxima as pending regrade. Their actual scores and the final AI total
remain unknown. Gradescope shows provisional zero placeholders and a provisional
combined grade until regrading completes.

Set Gradescope's Outline maximum to the intended grading scale; adding AI rows
does not change that maximum.

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

Local checks do not establish deployed acceptance. Deploy backend support before
using new configuration fields. Backend-only rubric and prompt changes do not
require a new autograder ZIP; connector changes require updating the package.
Targets without `analysis.behavior` are skipped by the hook;
legacy `expected_tests`/`points_per_test` and unsupported evidence sources need
explicit configuration conversion. Backend startup and Alembic do not activate
new behaviors.

New Web/API assignments need an assignment token and quota registration as described
in the Web guide. Verify Gradescope → backend authentication/quota → DB configuration
→ selected provider → Gradescope feedback after deployment and migration.

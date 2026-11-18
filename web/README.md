# Web Application

## Overview

The Web application is the backend and instructor-facing interface for the project's AI-assisted grading system. It manages assignment configuration, grading targets, rubrics, submission records, test results, and AI-generated feedback. It also provides internal endpoints used by the nightly grading integration and an optional API for generating AI feedback.

The application is built with **FastAPI**, **Jinja2**, and **HTMX**, with **PostgreSQL** for relational data storage. Its AI feedback API supports three backend engines: Vertex, Pitt, and the Gemini Developer API. The application also maintains JSON-based runtime state for assignment tokens, call quotas, and replayed responses.

## System Components

- **Instructor UI:** A server-rendered interface for configuring assignments, grading targets and rubrics, and inspecting submissions, test results and provider feedback.
- **FastAPI backend:** Provides the application's routes, internal endpoints, database-backed assignment configuration, health checks, and optional AI feedback API.
- **PostgreSQL database:** Stores courses, assignments, grading targets, rubric versions, provider configuration, submissions, test results, AI feedback, and nightly trigger records.
- **Repository layer:** Provides a common interface for grading-pipeline data access, with a PostgreSQL implementation for the application and an in-memory implementation for testing.
- **AI feedback API:** Generates feedback using the configured AI engine and model. Its JSON runtime state stores assignment tokens, usage quotas, and replay responses separately from the relational grading data.
- **Nightly grading integration:** Uses protected internal endpoints to identify submissions awaiting nightly processing and authorize them for grading.

## Documentation Guide

Use the sections below to find setup instructions, implementation details, and operational guidance.

- [Quick Start: View the Instructor UI](#quick-start-see-the-instructor-ui) — Start the application locally and explore sample grading data.
- [Run with Docker](#run-with-docker-recommended) — Set up and run the application and its database using Docker Compose.
- [Run without Docker](#run-without-docker) — Run the application against a local PostgreSQL instance.
- [AI Feedback API](#ai-feedback-api-database-configuration) — Learn about supported AI engines, credentials, API endpoints, and assignment configuration.
- [Deploy the Combined Web and AI Backend](#deploy-the-combined-web-and-ai-backend) — Configure and deploy the combined service.
- [Change Engine or Model](#change-engine-or-model) — Update the configured AI engine and model.
- [Database Schema](#database-schema) — Explore the database tables, relationships, conventions, and migration process.
- [Application Layout](#layout) — Find the main source files and understand their responsibilities.
- [Instructor UI](#instructor-ui) — Review the available pages and how the interface is implemented.
- [Repository Layer](#repository-layer) — Understand the data-access interface and its SQL and in-memory implementations.
- [Tests](#tests) — Run the Web application tests.
- [Status](#status) and [Known Limitations](#known-limitations) — Review implemented functionality and outstanding limitations.

For more information about the system's design, see [`docs/architecture/DECISIONS.md`](../docs/architecture/DECISIONS.md) (AD-13), which explains the technology-stack decision. The database model and repository interfaces are documented in [`docs/architecture/02-data-model.puml`](../docs/architecture/02-data-model.puml) and [`docs/architecture/07-interfaces.puml`](../docs/architecture/07-interfaces.puml).

## Getting Started

To run the application locally, follow the [Quick Start](#quick-start-see-the-instructor-ui) instructions below. This provides a disposable local database and sample data for exploring the instructor interface.

---

## Quick start: see the instructor UI

For a disposable local database, with Docker installed and running. From the repository root:

```bash
cd web
[ -f .env ] || cp .env.example .env
docker compose up --build -d
docker compose exec web python -m app.seed
docker compose exec web python -m app.manage_instructors add you@example.edu
```

The last command prompts for a password (at least 12 characters). Then
open http://localhost:8000 and sign in. Click an assignment to see its
submissions, targets and rubrics, then a submission to see its test
results and each provider's feedback. `bob@example.edu` on Project 1 has
a target in each state: feedback given, failed and opted out.

The image contains the application and AI package; source files are not bind-mounted.
After pulling code changes, run `docker compose up -d --build web`. The server uses
one worker without auto-reload.

## Run with Docker (recommended)

From the repository root:

```bash
cd web
[ -f .env ] || cp .env.example .env
docker compose up --build -d
```

Then visit http://localhost:8000 for the instructor UI and
http://localhost:8000/health (which round-trips a query to Postgres to
confirm the DB connection).
The API docs are at http://localhost:8000/docs. `docker compose logs -f web`
follows the server output, and `docker compose down` stops everything
(the database volume is kept).

`.env.example` sets `INTEGRATION_API_TOKEN` to `local-development-token`.
The `/internal/nightly/...` and assignment configuration endpoints return 401
unless requests send that value in an `X-Internal-Token` header. The nightly
trigger in `integration/` must use the same token. Recreate Web with
`docker compose up -d web` after changing `.env`. For server deployment with AI feedback, follow
[Deploy the combined Web and AI backend](#deploy-the-combined-web-and-ai-backend).

### Sample data

For a disposable local database only. Do not run this seeder on the shared server.
With the containers running:

```bash
docker compose exec web python -m app.seed          # load once; a rerun does nothing
docker compose exec web python -m app.seed --reset  # wipe EVERY table, then reload
```

This adds one course with three assignments: an IMMEDIATE Python
calculator, a NIGHTLY Java linked list and an inactive one. It also adds
their targets and rubrics, three LLM providers (Gemini primary), and six
submissions, one in each state the pipeline produces:

| Submission | State | `decide_action` |
| --- | --- | --- |
| `sample-calc-sub-1` | Graded, feedback from two providers | REEMIT |
| `sample-calc-sub-2` | Graded, with one target opted out and one failed | REEMIT |
| `sample-calc-sub-3` | Tests recorded, not graded yet | GRADE |
| `sample-list-sub-1` | Waiting for the nightly run | DEFER |
| `sample-list-sub-2` | Authorized by the nightly trigger | GRADE |
| `sample-list-sub-3` | Graded by last night's run | REEMIT |

The script prints the assignment ids. The instructor UI at
http://localhost:8000 shows all of it. To browse the tables directly:
`docker compose exec db psql -U autograder`.

`GET /internal/nightly/submissions` then returns `sample-list-sub-1` and
`sample-list-sub-2`, which gives the nightly trigger work to find:

```bash
curl -H "X-Internal-Token: local-development-token" http://localhost:8000/internal/nightly/submissions
```

## Run without Docker

Requires a local PostgreSQL instance; point `DATABASE_URL` at it via `.env`.

```bash
python -m venv .venv
source .venv/bin/activate  # .venv\Scripts\activate on Windows
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

## AI feedback API (database configuration)

The Web Docker service reads assignment, target, rubric and the primary engine/model
configuration from PostgreSQL. During a Gradescope autograder run, the hook sends
submission metadata with each feedback request. The API then stores provider
feedback and target completion in PostgreSQL; a final `ai-complete` call records
the connector's aggregate outcome and sets the completion guard on success. JSON
retains backend policy, assignment token hashes, call quota and replay responses.
The backend supports three engines:

| Engine | Credentials on the backend | Model |
| --- | --- | --- |
| `vertex` | Google impersonation ADC | Vertex Gemini ID; vendor `GOOGLE` |
| `pitt` | `PITT_API_KEY` | Full Pitt project route; vendor `OPENAI`, `ANTHROPIC` or `GOOGLE` |
| `gemini_api` | `GEMINI_API_KEY` | Gemini Developer API ID; vendor `GOOGLE` |

Pitt uses `https://api.portkey.ai/v1/chat/completions` and `x-portkey-api-key`.
Copy the complete `@.../...` model ID from the Pitt project. The container sends
neither model choices nor provider credentials. When running without Docker,
export provider keys in the server process environment; loading Web settings from
`.env` does not export them. Compose supplies them to the container.

### Prepare the database configuration

At `/assignments/new`, choose **Grading** or **Feedback** and save. For Grading,
add a target for each source scope and give each item a requirement and points.
For Feedback, choose the source files and optionally add guidance for explaining
visible failed tests. You can add assignment context later. **Gradescope
integration** downloads the connector ZIP with the fixed token generated when
the assignment was created. Repeat downloads use the same token.

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| `POST /internal/assignments` | `X-Internal-Token` | Create an assignment, targets and current rubrics atomically |
| `GET /internal/assignments/{id}/config` | `X-Internal-Token` | Read current AI configuration |
| `POST /v1/feedback` | Assignment Bearer token | Generate and persist feedback for one submission target |
| `POST /v1/submissions/{platform_submission_id}/ai-complete` | Assignment Bearer token | Record the whole execution's required `ai_status`: `SUCCEEDED` or `FAILED` |
| `POST /v1/submissions/{platform_submission_id}/test-results` | Assignment Bearer token | Record one run's Gradescope test rows for the results dashboard; see below |
| `GET /internal/ai-failures/submissions` | `X-Internal-Token` | List active submissions with `ai_status=FAILED`, across IMMEDIATE and NIGHTLY assignments |
| `GET /v1/assignment/config` | Assignment Bearer token | Read the token's target collection rules and revision, without using call quota |

Use `/docs` for the internal API request schema. Creation accepts either an
existing `course_id` or a new `course`; duplicate platform assignment IDs return
409. The assignment UUID binds its token, and target UUIDs identify feedback
requests. These are DB UUIDs, not Gradescope assignment numbers or filenames.
Targets without a current rubric are excluded from AI execution.

Before any AI call, the hook sends every row of `results.json` once to
`test-results` (`submission`, `run_id`, top-level `visibility`, and `tests`
with each row's `name`, `status`, `score`, `max_score`, `output`, `visibility`).
`app/run_results.py` maps rows to targets with each target's stored rules:
rows matched by a `results/results.json` evidence source's `include` patterns
(every row when it has none), otherwise rows named in `expected_tests`. Any
failing row is `FAIL`; a missing expected test or unreadable row is `ERROR`;
otherwise `PASS`. A `functional_test_feedback` target with no visible failure
is opted out. Rows are stored per `run_id` (migration 0011), so repeating a run
adds nothing and a rerun adds one fresh row per target. A failure here is
logged and never stops the AI step.

The connector reports `submission` metadata and `ai_status` to `ai-complete`
after writing `results.json`. Any AI-processing failure in the execution makes
the submission `FAILED`; a successful target cannot erase another target's
failure. A fully successful rerun updates the same submission to `SUCCEEDED`,
removing it from the failure list. Normal no-call cases succeed; failed student
tests, rubric verdicts and valid insufficient-evidence responses are not API
failures. The response acknowledges recording with `status: completed` and
echoes `ai_status`. Old clients must be updated to send the explicit outcome.
Unreported historical rows retain a null `ai_status`; they are not assumed to
have succeeded. Target errors remain available through the existing diagnostics.

The failure-list response contains `submission_id`, `platform`,
`platform_assignment_id`, `platform_submission_id` and `ai_status`. It does not
trigger a rerun. The single-outcome field
assumes executions of the same submission do not overlap; it records the last
reported outcome. Credentials, call quotas and request replay remain unchanged.

### NIGHTLY authorization and failure retries

The backend owns eligibility; Gradescope owns the selected submission. Before
either automated cycle, the caller reads the complete instructor submission list
(all pages, without filtering by result) and synchronizes each assignment:

`PUT /internal/assignments/{assignment_id}/active-submissions`

Body: `{"platform_submission_ids": ["123456", "123457"]}`. IDs are Gradescope
submission IDs; the path uses the DB assignment UUID. The endpoint atomically
replaces `is_active_attempt` flags without deleting history or granting permission.
An empty list clears the assignment's selections. Unknown IDs return `409` with
`detail.missing_submission_ids`, leaving the previous snapshot unchanged; do not
continue that assignment's cycle. New submissions have an unknown selection
(`is_active_attempt = NULL`) until synchronized; `true` means selected and `false`
means superseded. Unknown submissions remain visible in Web results and target
views without a Superseded label, but cannot enter automated queues or receive
nightly authorization. IMMEDIATE first-run AI still executes without a snapshot.
Synchronization preserves submission metadata, outcomes and feedback history.

| Cycle | Query | Authorization request before rerun |
| --- | --- | --- |
| Initial nightly processing | `GET /internal/nightly/submissions?mode=NIGHTLY` | `{"trigger_source":"PENDING_NIGHTLY"}` |
| Periodic AI-failure retry | `GET /internal/ai-failures/submissions?retry_eligible=true` | `{"trigger_source":"AI_FAILURES"}` |

Send the authorization body to
`POST /internal/nightly/submissions/{submission_id}/authorize`, using the DB
submission UUID returned by the query. All these endpoints require
`X-Internal-Token`. The response includes `authorized: true` and
`authorization_granted`; ineligible submissions return `409`, missing IDs `404`.
The source is required: the old bodyless authorization call is no longer accepted.

Nightly authorization applies only to selected NIGHTLY submissions awaiting work,
including failures that have never been authorized. It sets `ai_authorized_at`
once and preserves it across subsequent retries. Authorized failures are excluded
from the nightly list. The retry source only checks eligibility and never grants
or refreshes authorization: it requires `FAILED` and either IMMEDIATE mode or an
existing NIGHTLY authorization. Automated queries require a confirmed selected
submission and an active assignment; the unfiltered failure endpoint remains
available for diagnostics.
Authorized, unfinished submissions without a failed outcome remain eligible for
a later nightly cycle if the Gradescope rerun was not successfully triggered.
Retry scans can also query
`GET /internal/nightly/submissions?mode=NIGHTLY&retry_failed=true`: this restricts
the pending list to submissions whose latest audit is `RERUN_FAILED` in a finished
cycle. A newer authorization, submitted rerun, or successful rerun excludes an
older failure. These recovery candidates still use `PENDING_NIGHTLY` authorization.

`POST /v1/feedback` checks NIGHTLY authorization before reserving quota or starting
a new model call. Its HTTP 200 response can be `status: deferred` (awaiting nightly
authorization) or `status: inactive`, with `request_id` and `reason`, but no model
result. The connector stops AI requests, preserves functional scores, displays
pending AI grading, and does not report a normal deferral as success or failure.
Deferring a fresh attempt reopens its target's pending marker and clears the
submission completion guard while preserving the previously reported AI outcome.
Actual processing errors still report `FAILED`, even before nightly authorization.
`ai-complete` accepts `completion_reason: processed` (default) or `not_needed`;
the latter is only for successful no-call executions of feedback-only/empty
configurations. It preserves the Java no-feedback-needed case without granting
nightly permission. NIGHTLY requests require valid submission metadata and the
combined Web service's database-backed repository.

The caller must refresh the snapshot and recheck eligibility if the selected ID
changes before clicking rerun. New IDs do not inherit authorization; old versions
retain their records but leave automated queues. This API cannot observe platform
changes until synchronized. Teacher-initiated reruns retain independent request
identities; accepted-request polling/replay does not start another model call.

Rollout requires updated shared autograder files and caller support for these
contracts. Existing automation is not updated by this change. At the coordinated
cutover, clear historical NIGHTLY authorization timestamps, preserve outcomes and
feedback, synchronize selections, and authorize through the next nightly cycle.
Deployments must apply migration `0009` before running the updated backend; it
makes `submission.is_active_attempt` nullable so unsynced submissions remain
distinct from superseded ones. Scheduling and live authorization resets remain
separate deployment actions.

Migration `0003` adds the provider engine; `0004` adds target analysis configuration.
Migration `0007` adds submission AI outcomes and backfills existing target failures.
The migration chain uses `0005` for instructors and `0006` for rerun audit statuses;
verify deployed history before upgrading an installation previously labeled `0005`.
The Web image applies migrations at startup. Packages contain only the backend
URL in `config.json` and an assignment token, not a duplicated assignment JSON.

### Request lifecycle

The shared connector generates a fresh run UUID for each submission execution,
including individual reruns and Regrade All. It combines this UUID with the
submission ID when available, target inputs and the fetched configuration revision
to derive request IDs. Polling within a run reuses its request ID; an explicit
`--run-id` supports intentional replay of the same submission and inputs.
Rubric, analysis, model or prompt-version changes produce new request IDs on the
next run. New requests with stale revisions return `409 config_changed` before
reserving quota. Accepted requests retain their original configuration while
polling or replaying, subject to current assignment and target access.
Legacy requests without a revision keep their original replay behavior; use a new
request ID after changing their grading configuration.

The standard-library client sends `Prefer: respond-async` and repeats the same
POST after HTTP 202 until completion. Polling and completed-result replay consume
no additional calls. Provider failures/timeouts retain their reserved calls.
Default budgets are 240 seconds for generation, 270 seconds per client request
and 300 seconds for the entire Gradescope hook. Calls still require current DB
configuration and valid assignment access, including during replay.

### Deploy the combined Web and AI backend

The team must provide public HTTPS to Web port 8000. The client uses short
asynchronous polling requests; synchronous API callers need a proxy timeout longer
than the configured generation timeout. Instructor pages require sign-in. The
combined service reads SQL directly; `ASSIGNMENT_API_URL` is only needed for a
separate AI process.

1. From the existing `web/` directory, preserve the Compose project name and `.env`.
   Keep the private `INTEGRATION_API_TOKEN` shared with nightly automation. Leave
   `FEEDBACK_STATE_PATH=` empty for initial setup.
2. Set `PITT_API_KEY` and/or `GEMINI_API_KEY` in private `.env`. For Vertex, set
   `GOOGLE_ADC_DIR` to a private host directory containing
   `application_default_credentials.json`, using directory/file permissions
   `700`/`600`. Compose mounts it read-only. Create impersonation ADC with
   `gcloud auth application-default login --impersonate-service-account=YOUR_SERVICE_ACCOUNT`.
   The service account must match the initializer and have Vertex access; the
   caller needs Service Account Token Creator permission. The adapter accepts
   impersonation ADC, not ordinary user ADC or service-account keys. See
   [Google's ADC setup](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment).
   Pitt/Gemini API do not need ADC; leave `GOOGLE_ADC_DIR` empty for those engines.
3. Run `docker compose up -d --build web`. Have the DB owner configure exactly one
   enabled primary row in `llm_provider_config`. For an empty provider table:

   ```sql
   INSERT INTO llm_provider_config (engine, provider, model_name, is_enabled, is_primary)
   VALUES ('pitt', 'OPENAI', '@YOUR_PROJECT/YOUR_MODEL', true, true);
   ```

   Substitute the exact authorized model route. Use the update below if a primary
   row already exists. Do not run `app.seed` on the shared DB. Create the assignment
   and its targets/rubrics through the forms or internal API.
4. Initialize runtime state once with the assignment UUID and the same engine/model
   as the DB. For example:

   ```bash
   docker compose exec web python -m app.seed_feedback \
     --assignment-id YOUR_ASSIGNMENT_UUID \
     --state /var/lib/ai-feedback/state.json \
     --token-file /var/lib/ai-feedback/client/assignment.token \
     --client-config /var/lib/ai-feedback/client/config.json \
     --backend-url https://YOUR-SERVER \
     --allow-model pitt:@YOUR_PROJECT/YOUR_MODEL \
     --max-code-chars 64000 --max-body-bytes 262144 \
     --max-output-tokens 16384 --timeout-seconds 240 \
     --max-calls 10
   ```

   Repeat `--allow-model ENGINE:MODEL` for each permitted pair. Use
   `gemini_api:YOUR_GEMINI_MODEL` for Gemini API, or `vertex:YOUR_GEMINI_MODEL` with
   explicit `--project` and `--service-account` for Vertex; `--location` defaults
   to `global`.
   The current RentACat configurations each make one call per fresh submission:
   one overall Java target or one Playwright testplan target. Allow for failed
   calls and independent trials. Copy the private client files into the matching
   package; keep provider credentials and generated upload archives out of Git.
5. Set `FEEDBACK_STATE_PATH=/var/lib/ai-feedback/state.json` in `.env`, then run
   `docker compose up -d --no-deps web`. Both assignment-token routes should appear
   in `/docs`. `/health` checks the DB, not provider access; verify an authenticated
   request with the [HTTP client](../ai_grading/README.md#use-the-backend),
   then the complete Gradescope flow.

For later code updates, use `docker compose up -d --build web`. Preserve `.env`,
`db_data` and `feedback_data`; do not use `down -v` or rerun initialization to update.
Use one Web worker/container per state volume. For Web-issued packages, set
`FEEDBACK_PUBLIC_BASE_URL` to the backend origin Gradescope can reach and
recreate Web. Each ZIP includes that origin in `config.json`.
For CLI provisioning, stop Web and run `seed_feedback --add-assignment` with
the existing state path, the new assignment UUID and new client output paths.
Additions inherit the existing model policy and limits.

### Change engine or model

Signed-in instructors can use **AI settings** (`/settings/ai`) to switch all
assignments between Pitt Gemini 4 Argon, Gemini 3.8 Flash, Claude Sonnet 5.5,
Claude Opus 5.5, GPT-6.1 Sol and GPT-6 Luna. The saved vendor follows the selected
model (`GOOGLE`, `ANTHROPIC` or `OPENAI`). **Save settings**
saves the model and shared API key together. An empty key field restores the
server's `PITT_API_KEY`; a nonempty field replaces the shared key. Saved keys are
never returned to the browser: the page displays a fixed `********` marker and
leaves the input empty. Re-enter the custom key when saving another model if you
want to retain it. **Test API** tests the current form values without saving or
using assignment quota; it makes one short, billable provider request.

To save custom keys, set a stable private `PITT_KEY_ENCRYPTION_KEY` in `.env`
(generate it with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
and recreate Web. Keep this encryption key across restarts. Migration `0010`
stores only encrypted credentials; it preserves the selected model and uses the
server key until a custom key is saved. Web switches update the Pitt allowlist
under the existing state lock, preserving quotas and request history. New requests
use the saved settings immediately; existing requests and replays retain their
original attempt. Missing default prices for all six choices are added at the
same time; custom schedules and historical cost snapshots are preserved. The
previous untouched Argon default schedule is replaced with the supplied Pitt
rate, without an assumed future increase. See the [price estimates](../ai_grading/README.md#estimated-api-cost)
for the Pitt input/output rates and separate public cache-rate assumptions.

For engines and models outside these Web choices, use the manual procedure:

The enabled primary DB row selects the engine/model for all assignments. The
state's `config.allowed_models` must include that exact pair. Stop Web before
editing state, retain tokens/quota/replay records, and restart afterward. Do not
rerun the initializer. Example allowlist:

```json
{
  "vertex": ["YOUR_VERTEX_GEMINI_MODEL"],
  "pitt": ["@YOUR_PROJECT/YOUR_MODEL"],
  "gemini_api": ["YOUR_GEMINI_API_MODEL"]
}
```

Omit disabled engines. If adding Vertex, also set `project`, `location` and
`service_account` in state and supply matching ADC. After preparing credentials
and the allowlist, update the existing primary row:

```sql
UPDATE llm_provider_config
SET engine = 'pitt', provider = 'OPENAI',
    model_name = '@YOUR_PROJECT/YOUR_MODEL', is_enabled = true
WHERE is_primary = true;
```

Use the matching vendor and verify that one row was updated. Read the internal
configuration endpoint to confirm the selection.

### Current limits and separate-process use

- State files on schema 2 or 3 upgrade to schema 4. Existing active tokens keep
  working without expiry, along with their quotas and replay history.
- Pitt bypasses the upstream gateway cache. Backend replay remains in effect.
- To run AI as a separate local process, set `ASSIGNMENT_API_URL` and
  `INTEGRATION_API_TOKEN`; see `python3 -m web.app.feedback --help` from the repository
  root. Initialize its private state where the assignment database is accessible,
  then transfer it to the separate process. Export provider
  credentials in the process environment; standalone Python does not load
  `web/.env`. API readers accept HTTPS or loopback HTTP origins only. The separate
  process supports the server-key setting; a saved Web key requires the combined
  Web/backend deployment and is never exported through the configuration API.

## Database schema

Tables are defined in `app/models.py`, which mirrors
`docs/architecture/02-data-model.puml`. Migrations live in
`migrations/versions/` and are applied automatically when the Docker
container starts.

### Tables

Instructor configuration, created through the assignment API or directly;
global providers are managed separately. `app.seed` also supplies sample data:

| Table | One row per | Notes |
| --- | --- | --- |
| `course` | Course | Name and term. |
| `assignment` | Assignment on a platform | `platform` + `platform_assignment_id` is unique. `grading_mode` is IMMEDIATE or NIGHTLY. `language` is free text (AD-12). `is_active` false hides it from the nightly queue. |
| `grading_target` | Method, class or file graded within an assignment | `identifier` is unique within the assignment (AD-3); `display_order` orders feedback; `analysis_config` defines source selection, evidence and testplan settings. |
| `rubric` | Version of a target's rubric | Versions are kept; at most one per target has `is_current`. |
| `llm_provider_config` | Provider and model to ask | Global, not per assignment. At most one `is_primary`; disabled rows are skipped. |
| `instructor` | Account that can sign in to the UI | Not in the data model diagram (OQ-9). `email` is stored lowercased and unique; `password_hash` is scrypt. Managed with `app.manage_instructors`. |

Runtime, written as submissions are tested and graded:

| Table | One row per | Notes |
| --- | --- | --- |
| `submission` | Student submission | Unique per assignment by `platform_submission_id`, so reruns reuse the row. `ai_authorized_at` is set by the nightly trigger; `ai_graded_submitted_at` is the AD-9 "already graded" guard. |
| `submission_target` | Target within a submission | `ai_needed` false means opted out. `completed_at` set means done, with `last_error` set if it failed (AD-8). Also holds the provider agreement and why the feedback was selected. |
| `test_result` | Test outcome against a target | `status` is PASS, FAIL, ERROR or SKIP (AD-10). |
| `ai_feedback` | Provider response for a target | Records the rubric, model, prompt version, score, confidence and tokens. The one sent back to the student has `is_selected` (at most one per target). |
| `ai_feedback_review` | Instructor rating of a response | 1-5 plus an optional comment, from the submission page (FR-11). Re-rating adds a row, and the page shows the latest. `reviewer` is the signed-in instructor's email. |
| `trigger_run` | Nightly trigger run | Audit row: start and finish times, status and counts. Written by the `/internal/nightly/runs` endpoints. |

Each submission gets a `submission_target` row for every target on its
assignment, and test results and feedback hang off that row, not off the
submission.

### Conventions

- Primary keys are UUIDs generated by Postgres (`gen_random_uuid()`).
- Timestamps are `timestamptz`, stored in UTC.
- Enum-like columns (`platform`, `grading_mode`, `kind`, `provider`,
  `status`, `trigger`) are text with a CHECK constraint rather than
  Postgres enums, so adding a value is a one-line migration. The allowed
  values are the tuples at the top of `models.py`.
- "At most one" rules (current rubric, primary provider, selected
  feedback) are partial unique indexes, so the database rejects a
  second one.
- Constraint names follow `NAMING_CONVENTION` in `models.py`, so
  migrations can refer to them.
- Where the schema departs from the diagram, a comment in `models.py`
  says why. For example, `ai_graded_submitted_at` is nullable because
  it stays empty until grading finishes.

### Changing the schema

To change the schema, edit `app/models.py`, then generate and review a
migration against a running database:

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

Autogenerate misses some things, notably CHECK constraint changes, so
read the generated file before committing it.

## Layout

```
app/
  auth.py        Instructor sign-in: /login, /logout, password hashing, the UI route guard
  manage_instructors.py  CLI to add, list, deactivate and reset instructor accounts
  config.py      Settings read from .env (including optional FEEDBACK_STATE_PATH)
  db.py          SQLAlchemy engine and SessionLocal
  feedback.py    Feedback router and standalone local entry point
  feedback_config.py  DB configuration and JSON runtime record types
  feedback_assignment.py  DB configuration adapter and protected API reader
  feedback_store.py   JSON storage, quota reservation and idempotency
  main.py        FastAPI app: assignment configuration, health, nightly, UI and optional feedback
  models.py      ORM tables (02-data-model.puml)
  repository/    Data access used by the grading pipeline (below)
  seed.py        Sample data (see "Sample data" above)
  seed_feedback.py  Bind private token/runtime state to an existing DB assignment
  ui.py          Instructor UI pages (below)
  templates/     Jinja2 templates for the UI
  static/        CSS
migrations/      Alembic migrations
```

## Instructor UI

Server-rendered pages: `app/ui.py` routes, Jinja2 templates, and HTMX
available for partial updates. The pages call the repository directly
rather than the JSON API, so each operation is written once. Every page
needs a signed-in instructor (see "Instructor sign-in" below):

| Page | Shows |
| --- | --- |
| `/` | Every assignment, with its mode, language and target and submission counts. |
| `/assignments/{id}` | Its submissions (newest first, with each one's status and targets counted by state), its targets with their current rubric, and the providers. |
| `/submissions/{id}` | Each target's test results, and every provider's feedback side by side with the one sent to the student marked. Opted-out, waiting and failed targets each say why there is no feedback. Each response can be rated 1-5 with a comment (`POST /feedback/{id}/review`, FR-11). |

The routes get the repository from `get_repository`, which the tests in
`test_ui.py` override with an `InMemoryRepository`. LLM output is
rendered with Jinja2's autoescaping, never as raw HTML.

### Instructor sign-in

`/login` takes an email and password; there is no sign-up page, so
accounts are made from the command line:

```bash
docker compose exec web python -m app.manage_instructors add ada@pitt.edu --name "Ada Lovelace"
docker compose exec web python -m app.manage_instructors passwd ada@pitt.edu
docker compose exec web python -m app.manage_instructors deactivate ada@pitt.edu
docker compose exec web python -m app.manage_instructors list
```

For a script, add `--password-stdin` and pipe the password into
`docker compose exec -T`.

- A signed-in session is a signed cookie (`autograder_session`, HttpOnly,
  SameSite=Lax) holding the instructor's id. It expires after 12 hours
  without a request.
- Each request looks the account up again, so deactivating an account or
  changing its password signs that instructor out everywhere.
- Set `SESSION_SECRET` in `.env` on any shared server; without it a random
  key is used and each restart signs everyone out. Set
  `SESSION_COOKIE_SECURE=true` once the site is served over HTTPS.
- After 10 failed sign-ins from one address in 15 minutes, that address is
  refused until the window passes (kept in memory, for the single worker).
- Every instructor can see and edit every course; per-course access is not
  built yet.
- `/internal/*` and `/v1/feedback` are not affected; they keep their own
  tokens. `/health` and `/static` stay public.

Tests (`test_ui.py`) override `require_instructor`; `test_auth.py` covers
sign-in itself with an `InMemoryInstructorStore`.

## Repository layer

All database access for the grading pipeline goes through
`IGradingRepository` in `app/repository/base.py`, which follows
`docs/architecture/07-interfaces.puml`. There are two implementations:

- `SqlGradingRepository` (`sql.py`): the real one, on PostgreSQL.
- `InMemoryRepository` (`memory.py`): plain Python objects, for tests and
  for other components that need a repository without a database. It
  also has `add_assignment`, `add_grading_target`, `add_rubric` and
  `add_provider` for setting up data.

Both pass the same contract tests, so they behave the same. Adding a
method to the interface means adding it to both in the same commit.

Methods that act on one target take the submission id and the target's
name, not a `submission_target` id. That way every one of them can reject
a name that is not registered on the assignment.

### Methods

Submission run (when a submission first reports in):

| Method | What it does |
| --- | --- |
| `upsert_submission(ref)` | Returns the submission's id, creating the row on first report. A rerun of the same submission gets the same row back. |
| `create_targets_default_needed(submission_id)` | Creates one row per registered target with `ai_needed` true (AD-7). Safe to repeat: existing rows keep their state. |
| `set_ai_not_needed(submission_id, target)` | Opts a target out of AI feedback. Returns whether it was needed before. |
| `add_test_result(submission_id, target, status, feedback)` | Records one test outcome against a target (AD-10). |
| `decide_action(submission_id)` | Returns GRADE, DEFER or REEMIT for this run (AD-11, see below). GRADE comes with the targets still needing feedback. |

Grading run (while AI feedback is produced):

| Method | What it does |
| --- | --- |
| `authorize_for_grading(submission_id, trigger_source)` | Checks current eligibility; only `PENDING_NIGHTLY` can grant permission. `AI_FAILURES` never grants or refreshes it. Returns whether permission was newly granted. |
| `get_test_results(submission_id, target)` | A target's test results, oldest first, for the prompt. |
| `save_feedback(submission_id, target, result)` | Stores every provider's response, marks the selected one and completes the target. |
| `mark_target_failed(submission_id, target, error)` | Completes the target with `last_error` set (AD-8), so the nightly trigger does not retry it forever. |
| `mark_ai_complete_if_all_done(submission_id)` | Sets the AD-9 "already graded" guard once every needed target is completed. Returns whether the guard is set. |

Reads:

| Method | What it does |
| --- | --- |
| `get_assignment_config(assignment_id)` | The assignment, its targets in display order with their current rubric, and the enabled providers (primary first). |
| `get_selected_feedback(submission_id)` | For REEMIT: the selected feedback or error for each needed target. Opted-out targets are left out. |
| `list_submissions_pending_ai(mode)` | Active selections awaiting work or initial authorization after failure. Excludes already-authorized failures; oldest first. |

Instructor UI reads:

| Method | What it does |
| --- | --- |
| `list_assignments()` | Every assignment with its target and submission counts. Active first, then by name. |
| `list_submissions(assignment_id)` | The assignment's submissions, newest first, each with its action and its targets counted as done, failed, waiting or opted out. Includes superseded attempts. |
| `get_submission_detail(submission_id)` | Every target with its status, test results and every provider's feedback (selected first, with the rubric version it was written against). |

A target's status (`TargetStatus`) comes from `target_status`, shared by
both implementations: opted out if `ai_needed` is false, otherwise
waiting until `completed_at` is set, then failed or done depending on
`last_error`.

### Choosing an action

`decide_action` uses `derive_action`, a pure function both
implementations share:

1. If the submission was already graded at this submission time
   (the AD-9 guard), **REEMIT**: send the stored feedback again.
2. Otherwise, an `IMMEDIATE` assignment is **GRADE**.
3. A `NIGHTLY` assignment is **DEFER** until `authorize_for_grading` has
   been called, then **GRADE**.

### Target lifecycle

A target starts needed (`ai_needed` true, `completed_at` empty). From
there it is either opted out, or completed by `save_feedback` or
`mark_target_failed`. A completed or opted-out target cannot be completed
again. See `docs/architecture/06-submission-target-state.puml`.

### Errors

All errors derive from `RepositoryError`:

- `NotFoundError`: the submission or assignment does not exist.
- `UnknownTargetError`: the target name is not registered (AD-3). The
  message lists the registered names, for the API to show.
- `ConflictError`: the request is valid but the row's state does not
  allow it, such as saving feedback for an opted-out or completed target.

Bad input, such as an invalid test status or a rubric from a different
target, raises `ValueError`.

## Tests

The web tests live in `tests/unit/web_test/` (see `tests/README.md` for
installing their requirements). `test_repository.py` runs every test
against both implementations. The SQL run needs a test database in the
compose Postgres; create it once:

```bash
docker compose up -d db
docker compose exec db createdb -U autograder autograder_test
```

Then:

```bash
cd tests/unit/web_test
pytest
```

Without that database the SQL tests are skipped, not failed, so check
the summary for skips. Point `TEST_DATABASE_URL` elsewhere to use a
different database.

## Status

Done: the schema and migrations, the full repository (SQL and in
memory) with tests, the sample data script, the internal endpoints the
nightly trigger calls (shared-token auth), authenticated assignment creation and
configuration reads, read-only instructor pages, and the Gradescope-to-database
feedback write-back path. The AI feedback API selects Vertex, Pitt or Gemini API
from PostgreSQL configuration, retains assignment tokens/quota/replay state in
JSON, and persists target feedback plus the final submission completion guard.

Assignment context: an instructor can attach notes and documents (PDFs or
UTF-8 text files, 5 MB each, 20 files, 60,000 characters of text in all) to
an assignment, on the new-assignment form or the assignment page. Only the
extracted text is stored (`assignment_context_file`, migration 0008), and
`read_assignment_config` appends it to every target's background, so both
`/v1/feedback` and the container's `/internal/assignments/{id}/config` see
it. Scanned PDFs without a text layer are refused. New dependency: `pypdf`.

Not done yet in the instructor UI: the NIGHTLY-only review-before-release
gate (OQ-3, answered but not built).

## Known limitations

- A `NIGHTLY` submission whose targets were all opted out still gets
  DEFER, though there is nothing to grade.
- Provider configuration is global, so every assignment uses the same
  providers.
- `list_submissions_pending_ai` also skips inactive assignments, which
  the sequence diagram does not show.

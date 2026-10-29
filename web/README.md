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
```

Then open http://localhost:8000. Click an assignment to see its
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

The combined Web service reads assignment, target, rubric and primary engine/model
configuration from PostgreSQL. The container fetches collection rules from the
backend and displays feedback in Gradescope. This path does not write submissions,
test results or feedback to DB or participate in nightly scheduling. JSON stores
backend policy, assignment token hashes, call quota and replay responses.

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

Create an assignment through `/assignments/new`, add targets, then choose
**Configure target** to save the rubric and analysis settings. The same form edits
source/context patterns, testplan, expected IDs, points, file limits and runtime
evidence. Existing DB-injected targets use this form and the same configuration
reader. Global provider settings and assignment tokens are managed separately.

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| `POST /internal/assignments` | `X-Internal-Token` | Create an assignment, targets and current rubrics atomically |
| `GET /internal/assignments/{id}/config` | `X-Internal-Token` | Read full AI configuration for internal services |
| `GET /v1/assignment/config` | Assignment Bearer token | Read the token's target collection rules and revision, without using call quota |
| `POST /v1/feedback` | Assignment Bearer token | Generate or retrieve feedback |

Use `/docs` for the internal API request schema. Creation accepts either an
existing `course_id` or a new `course`; duplicate platform assignment IDs return
409. The assignment UUID binds its token, and target UUIDs identify feedback
requests. These are DB UUIDs, not Gradescope assignment numbers or filenames.
Targets without a current rubric are excluded from AI execution.

Migration `0003` adds the provider engine; `0004` adds target analysis configuration.
The Web image applies migrations at startup. Packages contain only the backend
URL in `config.json` and an assignment token, not a duplicated assignment JSON.

### Request lifecycle

The shared connector binds each request to the configuration revision it fetched.
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
than the configured generation timeout. Instructor forms have no application-level
login, so restrict them using the deployment's existing access controls. The
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
     --max-calls 10 --expires-in-hours 168
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
Use one Web worker/container per state volume. Adding another assignment requires
stopping Web and running `seed_feedback --add-assignment` with the existing state
path, the new assignment UUID and new client output paths. Additions inherit the
existing model policy and limits; omit those flags.

### Change engine or model

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
configuration endpoint to confirm the selection. The instructor forms edit target
configuration and rubrics, but do not edit global providers or token policy.

### Current limits and separate-process use

- Schema-2 state upgrades to schema 3 while retaining tokens, quotas and replay
  results; schema-1 state is incompatible. Token rotation is not implemented.
- Pitt bypasses the upstream gateway cache. Backend replay remains in effect.
- To run AI as a separate local process, set `ASSIGNMENT_API_URL` and
  `INTEGRATION_API_TOKEN`; see `python3 -m web.app.feedback --help` from the repository
  root. Keep its state separate from the combined Web service. Export provider
  credentials in the process environment; standalone Python does not load
  `web/.env`. API readers accept HTTPS or loopback HTTP origins only.

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

Runtime, written as submissions are tested and graded:

| Table | One row per | Notes |
| --- | --- | --- |
| `submission` | Student submission | Unique per assignment by `platform_submission_id`, so reruns reuse the row. `ai_authorized_at` is set by the nightly trigger; `ai_graded_submitted_at` is the AD-9 "already graded" guard. |
| `submission_target` | Target within a submission | `ai_needed` false means opted out. `completed_at` set means done, with `last_error` set if it failed (AD-8). Also holds the provider agreement and why the feedback was selected. |
| `test_result` | Test outcome against a target | `status` is PASS, FAIL, ERROR or SKIP (AD-10). |
| `ai_feedback` | Provider response for a target | Records the rubric, model, prompt version, score, confidence and tokens. The one sent back to the student has `is_selected` (at most one per target). |
| `ai_feedback_review` | Instructor rating of a response | Not written by anything yet. |
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
rather than the JSON API, so each operation is written once. Read-only
so far, with no login yet (OQ-9):

| Page | Shows |
| --- | --- |
| `/` | Every assignment, with its mode, language and target and submission counts. |
| `/assignments/{id}` | Its submissions (newest first, with each one's status and targets counted by state), its targets with their current rubric, and the providers. |
| `/submissions/{id}` | Each target's test results, and every provider's feedback side by side with the one sent to the student marked. Opted-out, waiting and failed targets each say why there is no feedback. |

The routes get the repository from `get_repository`, which the tests in
`test_ui.py` override with an `InMemoryRepository`. LLM output is
rendered with Jinja2's autoescaping, never as raw HTML.

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
| `authorize_for_grading(submission_id)` | Sets `ai_authorized_at` so the next run is GRADE. The nightly trigger calls it before rerunning. |
| `get_test_results(submission_id, target)` | A target's test results, oldest first, for the prompt. |
| `save_feedback(submission_id, target, result)` | Stores every provider's response, marks the selected one and completes the target. |
| `mark_target_failed(submission_id, target, error)` | Completes the target with `last_error` set (AD-8), so the nightly trigger does not retry it forever. |
| `mark_ai_complete_if_all_done(submission_id)` | Sets the AD-9 "already graded" guard once every needed target is completed. Returns whether the guard is set. |

Reads:

| Method | What it does |
| --- | --- |
| `get_assignment_config(assignment_id)` | The assignment, its targets in display order with their current rubric, and the enabled providers (primary first). |
| `get_selected_feedback(submission_id)` | For REEMIT: the selected feedback or error for each needed target. Opted-out targets are left out. |
| `list_submissions_pending_ai(mode)` | For the nightly trigger: submissions in that grading mode that still have a needed, incomplete target and are not graded. Oldest first. |

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
configuration reads, and instructor forms for assignments, targets and rubrics. The optional AI
feedback API selects Vertex, Pitt or Gemini API from PostgreSQL configuration
and retains assignment tokens, quota and replay state in JSON (see above). It
does not persist feedback or participate in the submission/nightly lifecycle.

Not done yet: instructor feedback rating and the results dashboard (waiting on
OQ-1); instructor login (OQ-9); and deployment.

## Known limitations

- A `NIGHTLY` rerun runs the test suite again, so it records duplicate
  test results.
- A `NIGHTLY` submission whose targets were all opted out still gets
  DEFER, though there is nothing to grade.
- Provider configuration is global, so every assignment uses the same
  providers.
- `list_submissions_pending_ai` also skips inactive assignments, which
  the sequence diagram does not show.

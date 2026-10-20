# Web app (Aiden)

FastAPI backend + Jinja2/HTMX instructor UI, backed by PostgreSQL. See
`docs/architecture/DECISIONS.md` (AD-13) for why this stack.

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
`docker compose up -d web` after changing `.env`. For server deployment with Vertex, follow
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

## Vertex feedback API (database configuration)

`POST /v1/feedback` uses an assignment Bearer token. The backend reads the
assignment, targets, current rubrics and primary Google model from PostgreSQL,
then calls Vertex with its own credentials. Feedback returns to Gradescope as a
zero-point row; this path does not create submissions, test results or feedback
in the database and does not participate in nightly scheduling.

| Data | Source |
| --- | --- |
| Assignment active status, targets, current rubric | PostgreSQL |
| Model | Enabled primary `GOOGLE` row in `llm_provider_config` (global) |
| Google project/identity, model allowlist, request limits | Backend runtime JSON |
| Assignment token hashes, call quota, request status and replay cache | Backend runtime JSON |
| Feedback displayed to the student | Gradescope `results.json` |

The JSON replay cache still contains completed responses so retries do not call
Vertex again. It is backend state, not a Web feedback history or SQL store.

### Prepare the database configuration

Have the Web/DB owner configure the global `llm_provider_config` first. Then use
the authenticated creation API below to insert an assignment, its targets and
current rubrics into the existing tables. No migration is required. The general
`app.seed` command also creates submissions and feedback; do not use it for a
configuration-only setup on a shared database.

Requirements:

- The assignment is active, with at least one target. Each target has a current
  rubric with nonblank `criteria_text`; `feedback_guidance` may be empty.
- Exactly one enabled provider is primary, with provider `GOOGLE` and a Gemini
  model allowed by the AI backend. Other providers are not invoked by this path.
- The assignment token binds the **database assignment UUID**, not the Gradescope
  assignment number. The client's `target_id` is the **database target UUID**.
- For a `FILE` target, use its filename as `identifier`, such as
  `SortedCollection.java`. The DB has no separate source path; method/class
  targets use the neutral prompt source label `submitted-source`. Actual source
  selection remains in the container's `targets[].source`.

`POST /internal/assignments` creates an active assignment atomically, with targets
in request order and version 1 current rubrics. Supply either an existing
`course_id` UUID or an explicit `course` object with a required `name` and optional
`term`. An explicit course is created in the same transaction; names are never
used to find or overwrite an existing course.

After the Web/DB teammate deploys the configuration endpoints, set the API origin
and internal token, then create the configuration:

```bash
export ASSIGNMENT_API_URL=https://YOUR-WEB-HOST
export INTEGRATION_API_TOKEN=YOUR_INTERNAL_TOKEN
curl --fail -i -H "X-Internal-Token: $INTEGRATION_API_TOKEN" \
  -H "Content-Type: application/json" --data-binary @- \
  "$ASSIGNMENT_API_URL/internal/assignments" <<'JSON'
{
  "course": {"name": "AI Feedback Integration", "term": "Fall 2026"},
  "platform_assignment_id": "YOUR_GRADESCOPE_ASSIGNMENT_ID",
  "name": "Sorted Collection Feedback",
  "language": "JAVA",
  "targets": [
    {
      "identifier": "SortedCollection.java",
      "kind": "FILE",
      "rubric": {
        "criteria_text": "Review correctness and clarity using the supplied functional test diagnostics.",
        "feedback_guidance": "Give concise, actionable feedback. Use SCORE: NONE; do not assign a grade."
      }
    }
  ]
}
JSON
```

To reuse a course, replace `course` with `"course_id": "YOUR_COURSE_UUID"`.
`platform` defaults to `GRADESCOPE`; `grading_mode` defaults to `IMMEDIATE` and
also accepts `NIGHTLY`. The request accepts 1–64 targets with unique, nonblank
identifiers (up to 256 characters); `kind` defaults to `FILE` and also accepts
`METHOD` or `CLASS`. Assignment name and platform assignment ID are required,
nonblank and limited to 256 characters; language is required and limited to 64.
Rubric criteria must be nonblank and at most 16,000 characters; guidance defaults
to empty and allows 8,000 characters. Client IDs, rubric versions, scores and
provider settings are not accepted.

Success returns **201** with the same configuration as the GET endpoint below,
plus `course_id`. Save `id` as the assignment UUID and the `targets` object keys
as target UUIDs; each rubric includes its generated ID. `Location` points to the
read endpoint, and the response uses `Cache-Control: no-store`.

A duplicate `(platform, platform_assignment_id)` returns **409** without
overwriting anything; a missing course returns **404**. Invalid input returns
**422**, configuration larger than 1 MiB returns **413**, and a missing or
unsupported enabled primary Google provider returns **503**. Creation leaves
the global provider configuration unchanged. It does not issue assignment tokens,
write submissions or feedback, or connect this flow to nightly execution.

The protected read endpoint is:

```text
GET /internal/assignments/{assignment_id}/config
X-Internal-Token: <INTEGRATION_API_TOKEN>
```

It returns `id`, `enabled`, `model`, and a `targets` object keyed by target UUID.
Each target includes its identifier, kind, source label and current rubric
(id, version, criteria and guidance). Responses are not HTTP-cached. Missing or
inactive assignments return 403; incomplete or unsupported configuration returns
503. These failures do not reserve an AI call.

The local reader accepts HTTPS or loopback HTTP. For the currently shared
HTTP server address, use a teammate-provided HTTPS origin or an authorized local
port forward before sending the internal token. The internal token stays on the
backend and never enters the Gradescope package.

### Deploy the combined Web and AI backend

The Docker image includes `ai_grading`. The existing Web service serves both
configuration endpoints and `POST /v1/feedback` on port 8000; the feedback router
reads SQL directly, so it needs no `ASSIGNMENT_API_URL` or additional backend port.
Google credentials stay on the server. The team manages its public HTTPS origin
and reverse proxy to this service; the public HTTP IP address alone is not accepted
by the Gradescope client. Allow enough proxy timeout for the configured AI request
limit (30 seconds by default).

1. Check out the reviewed branch/commit on the server and use the existing `web/`
   directory and Compose project name. Preserve `.env`, `db_data`, and any existing
   `feedback_data` volume. Never run `docker compose down -v` for an update. If this
   is the first setup, copy `.env.example` only when `.env` does not exist.
2. In `.env`, set a private, nondefault `INTEGRATION_API_TOKEN`; retain the token
   already shared with nightly automation if it is configured. For the first AI
   setup, leave `FEEDBACK_STATE_PATH=` empty. `GOOGLE_ADC_DIR` identifies a host
   directory mounted read-only at `/run/google`; its default is
   `../ai_grading/.local-vertex/google` relative to `web/`.
3. Prepare **impersonation ADC** for the chosen service account, using an authorized
   workstation or server login:

   ```bash
   gcloud auth application-default login --impersonate-service-account=YOUR_SERVICE_ACCOUNT
   ```

   Securely transfer the resulting `application_default_credentials.json` to the
   server, then install it in the configured directory. For the default path,
   from `web/`:

   ```bash
   install -d -m 700 ../ai_grading/.local-vertex/google
   install -m 600 /secure/path/application_default_credentials.json \
     ../ai_grading/.local-vertex/google/application_default_credentials.json
   ```

   The account must match `--service-account` below and have Vertex access in the
   configured project. The identity creating impersonation ADC needs Service
   Account Token Creator permission on that account. This implementation accepts
   impersonation ADC; ordinary user ADC and service-account keys are not supported.
   Credentials are never copied into the image or Gradescope ZIP. See Google's
   [impersonation ADC setup](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment).
4. Build and start Web/DB with feedback still disabled:

   ```bash
   docker compose up -d --build web
   curl --fail https://YOUR-SERVER/health
   ```

   Wait until `/health` returns 200 so migrations and Web startup have finished,
   then inspect the provider configuration:

   ```bash
   docker compose exec db psql -U autograder -d autograder -c \
     'SELECT provider, model_name, is_enabled, is_primary FROM llm_provider_config;'
   ```

   Exactly one enabled primary `GOOGLE` provider is required. Use its existing
   Gemini model in the state allowlist. If a different or disabled primary exists,
   coordinate any global change with the DB owner. **Only when no primary exists**,
   the DB owner can initialize one with the following conditional insert, replacing
   the example model with one available in the chosen Vertex project/location:

   ```bash
   docker compose exec -T db psql -U autograder -d autograder <<'SQL'
   INSERT INTO llm_provider_config (id, provider, model_name, is_enabled, is_primary)
   SELECT gen_random_uuid(), 'GOOGLE', 'gemini-3.5-flash-lite', true, true
   WHERE NOT EXISTS (SELECT 1 FROM llm_provider_config WHERE is_primary);
   SQL
   ```

   This leaves existing rows unchanged. Do not run `app.seed` on the shared DB.
5. Use [Prepare the database configuration](#prepare-the-database-configuration)
   to call `POST /internal/assignments`, then GET its configuration. Save the returned
   assignment UUID and target UUID. From `web/`, initialize the runtime volume once:

   ```bash
   docker compose exec web python -m app.seed_feedback \
     --assignment-id YOUR_ASSIGNMENT_UUID \
     --state /var/lib/ai-feedback/state.json \
     --token-file /var/lib/ai-feedback/client/assignment.token \
     --client-config /var/lib/ai-feedback/client/config.json \
     --backend-url https://YOUR-SERVER \
     --project YOUR_GOOGLE_PROJECT --location global \
     --service-account YOUR_SERVICE_ACCOUNT \
     --allowed-model YOUR_DB_GEMINI_MODEL \
     --max-calls 10 --expires-in-hours 168
   ```

   Choose a location supporting the selected model; repeat `--allowed-model` only
   for additional approved Gemini models. This command creates no DB rows and
   prints no token. Export only the client token for packaging:

   ```bash
   install -d -m 700 ../ai_grading/.local-vertex/server-client
   docker compose cp web:/var/lib/ai-feedback/client/assignment.token \
     ../ai_grading/.local-vertex/server-client/assignment.token
   chmod 600 ../ai_grading/.local-vertex/server-client/assignment.token
   ```

   Securely transfer that token to the packaging workstation at the same
   repository-relative path, owned by the packaging user with mode `600`. Do not
   transfer Google credentials, the internal API token, or backend state to Gradescope.
6. Set `FEEDBACK_STATE_PATH=/var/lib/ai-feedback/state.json` in `.env`, then enable
   feedback without restarting the database:

   ```bash
   docker compose up -d --no-deps web
   curl --fail https://YOUR-SERVER/health
   ```

   `/docs` should now include `POST /v1/feedback`. `/health` checks DB connectivity,
   not Vertex authentication. From the packaging workstation's repository root,
   make one real request with the exported token and target UUID:

   ```bash
   python3 -I -S ai_grading/local_client.py \
     --backend-url https://YOUR-SERVER \
     --token-file ai_grading/.local-vertex/server-client/assignment.token \
     --code-file /absolute/path/to/SortedCollection.java \
     --target-id YOUR_TARGET_UUID --json
   ```

   This request consumes one call. After it succeeds, build the Gradescope ZIP
   using the same server URL and token as described below.

For subsequent code updates, preserve `.env` and both named volumes, then run
`docker compose up -d --build web` from the same Compose project. Do not rerun the
initializer or recreate state: quota, token hashes and replay history must survive.
The state directory is mounted because writes also need a sibling lock file and
atomic replacements. Run exactly one Web worker/container against this volume.

The initializer creates one assignment/token in new state and refuses existing
outputs. Adding another assignment or rotating an expired token in existing state
is not yet implemented; this release supports the first controlled assignment test,
not unattended multi-assignment administration. Set a suitable test expiry and call
allowance before issuing the token. State schema 1 demo files are incompatible;
use new schema 2 state without deleting old experiments.

DB configuration is read on every valid request. Each new request ID reserves one
call before Vertex runs; failures/timeouts retain it. A replay returns its cached
response even if the rubric/model has changed. Use a new request ID for deliberate
reevaluation. Configuration edits neither reset quota nor invalidate cache; DB
outages prevent replays. Stop Web before editing JSON policy.

### Package and run in Gradescope

Keep the existing functional grader and connector. In
`ai_grading/autograder-vertex-feedback/ai_feedback.json`, use the target UUID
returned by the configuration endpoint:

```json
{
  "targets": [
    {
      "source": "**/SortedCollection.java",
      "target_id": "YOUR_TARGET_UUID"
    }
  ]
}
```

Keep the rubric in DB. For feedback-only use, request `SCORE: NONE` and no numerical
grade. The connector sends visible test diagnostics and appends `score: 0`,
`max_score: 0` AI rows to `results.json`. AI failure preserves functional scores;
compilation failure currently exits before AI.

From the repository root on the packaging workstation, rebuild using the exported
token and an existing functional-grader checkout (previously verified revision:
`15425db409b8f938b8cc3959f21e5d5dc419265d`):

```bash
python3 ai_grading/autograder-vertex-feedback/build.py \
  --backend-url https://YOUR-SERVER \
  --token-file ai_grading/.local-vertex/server-client/assignment.token \
  --functional-source /absolute/path/to/functional-grader
```

Upload `ai_grading/autograder-vertex-feedback/vertex autograder.zip` to that
Gradescope assignment and submit `SortedCollection.java`. The build refreshes
packaged `local_client.py` from its canonical `ai_grading/local_client.py` source.
The ZIP/token remain Git-ignored. This package still contains the original
SortedCollection/Java grader; it is not an arbitrary-assignment build system.
A deployed HTTPS backend needs no temporary tunnel.

| Flow | Implemented | Deployment prerequisite |
| --- | --- | --- |
| Create assignment, targets and rubrics | `POST /internal/assignments` | Internal token; enabled primary Google model |
| Read current DB configuration | `GET /internal/assignments/{id}/config`; direct SQL inside Web | Valid assignment and target UUIDs; current rubric |
| Generate feedback | `POST /v1/feedback` in the same image | Persistent initialized state; matching ADC; permitted Vertex model |
| Display feedback in Gradescope | Existing client and connector | Public HTTPS; unexpired assignment token; rebuilt ZIP with target UUID |

These interfaces cover a real single-assignment test without DB feedback writes.
Server deployment, live server-to-Vertex access and the final Gradescope run still
need verification; a healthy Web page alone does not establish that flow.

### Optional separate local AI backend

Use this only when running the AI process on your workstation against the Web API.
From the repository root, install `web/requirements.txt` in your Python environment
and configure impersonation ADC as above. Set `ASSIGNMENT_API_URL` to the deployed
HTTPS origin (or a loopback port forward) and `INTEGRATION_API_TOKEN` to its shared
secret. The current public HTTP IP address cannot be used for authenticated calls.

```bash
python3 -m web.app.seed_feedback \
  --assignment-id YOUR_ASSIGNMENT_UUID \
  --state ai_grading/.local-vertex/db/backend/state.json \
  --token-file ai_grading/.local-vertex/db/client/token \
  --client-config ai_grading/.local-vertex/db/client/config.json \
  --project YOUR_GOOGLE_PROJECT --location global \
  --service-account YOUR_SERVICE_ACCOUNT --allowed-model YOUR_DB_GEMINI_MODEL \
  --backend-url http://127.0.0.1:8001 --max-calls 10 --expires-in-hours 24
python3 -m web.app.feedback \
  --state ai_grading/.local-vertex/db/backend/state.json --port 8001
```

This process reads configuration through the protected Web API and needs no local
PostgreSQL connection. Its `/health` checks process health only. Use the client
command above with `http://127.0.0.1:8001` and the local token path. To reach this
local process from Gradescope, run `cloudflared tunnel --url http://127.0.0.1:8001
--no-autoupdate --protocol http2` and build the ZIP with the tunnel HTTPS origin
and local token. A restarted tunnel needs a rebuilt ZIP. Keep this local state
separate from the server volume. `integration/` is unchanged.

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
| `grading_target` | Method, class or file graded within an assignment | `identifier` is unique within the assignment (AD-3); `display_order` orders feedback. |
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
  vertex.py      Backend-owned Vertex client using the existing GeminiProvider
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
configuration reads, and the read-only instructor pages. The optional Vertex
feedback API reads assignment configuration from PostgreSQL
and retains assignment tokens, quota and replay state in JSON (see above). It
does not persist feedback or participate in the submission/nightly lifecycle.

Not done yet: the REST API the container calls, with per-assignment token
auth (the schema has no token column yet); in the instructor UI, the
forms for assignments, targets and rubrics, feedback rating and the
results dashboard (waiting on OQ-1); instructor login (OQ-9); and
deployment.

## Known limitations

- A `NIGHTLY` rerun runs the test suite again, so it records duplicate
  test results.
- A `NIGHTLY` submission whose targets were all opted out still gets
  DEFER, though there is nothing to grade.
- Provider configuration is global, so every assignment uses the same
  providers.
- `list_submissions_pending_ai` also skips inactive assignments, which
  the sequence diagram does not show.

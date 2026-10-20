# Web app (Aiden)

FastAPI backend + Jinja2/HTMX instructor UI, backed by PostgreSQL. See
`docs/architecture/DECISIONS.md` (AD-13) for why this stack.

## Quick start: see the instructor UI

Needs Docker Desktop installed and running. From the repository root:

```bash
cd web
cp .env.example .env
docker compose up --build -d
docker compose exec web python -m app.seed
```

Then open http://localhost:8000. Click an assignment to see its
submissions, targets and rubrics, then a submission to see its test
results and each provider's feedback. `bob@example.edu` on Project 1 has
a target in each state: feedback given, failed and opted out.

If you already had the containers running, `git pull` is enough: the
server reloads `app/` on change. Rerun `docker compose up --build -d`
only if `requirements.txt` or the `Dockerfile` changed.

## Run with Docker (recommended)

From the repository root:

```bash
cd web
cp .env.example .env
docker compose up --build -d
```

Then visit http://localhost:8000 for the instructor UI and
http://localhost:8000/health (which round-trips a query to Postgres to
confirm the DB connection).
The API docs are at http://localhost:8000/docs. `docker compose logs -f web`
follows the server output, and `docker compose down` stops everything
(the database volume is kept).

`.env.example` sets `INTEGRATION_API_TOKEN` to `local-development-token`.
The `/internal/nightly/...` endpoints return 401 unless requests send
that value in an `X-Internal-Token` header, so the nightly trigger in
`integration/` must use the same token. Restart the containers after
changing `.env`.

### Sample data

The database starts empty. To load sample data, with the containers running:

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

## Vertex feedback API (local prototype)

`POST /v1/feedback` uses an assignment Bearer token. The backend determines the
assignment, checks target scope and limits, builds the prompt with `ai_grading`,
calls Vertex with its own credentials, and saves the result to JSON. Client
parameters cannot change the model, Google identity, endpoint or allowance.
Pages, PostgreSQL data and nightly endpoints keep their existing behavior.

From the repository root, install the backend dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r web/requirements.txt
```

Non-secret defaults are project `project-bcedce55-e972-4a7b-bef`, location `global`,
model `gemini-3.5-flash-lite`, and the service account below. The backend uses
impersonation ADC and never reads legacy `.gemini.json`. If needed, install
`gcloud` and complete its browser login:

```bash
brew install --cask gcloud-cli
gcloud auth application-default login \
  --impersonate-service-account=gradescope-vertex-test@project-bcedce55-e972-4a7b-bef.iam.gserviceaccount.com
```

Your identity needs permission to impersonate the account, which needs Vertex
access. Initialize **once**; skip this step to keep an existing experiment:

```bash
.venv/bin/python -m web.app.seed_feedback \
  --state .local-vertex/backend/state.json \
  --token-file .local-vertex/client/token \
  --client-config .local-vertex/client/config.json \
  --backend-url http://127.0.0.1:8001 --max-calls 5 --expires-in-hours 24
```

This creates assignment `demo`, target `example`, a short rubric, and a random
token. JSON holds only the token hash; the raw token goes into the private client
file (mode `600`) and is never printed. The client config contains only
`{"backend_url":"http://127.0.0.1:8001"}`. `.local-vertex/` is Git-ignored.
Initialization refuses to overwrite files, so it cannot reset existing usage.

Start the local entry point, which needs **no PostgreSQL**, from the root:

```bash
.venv/bin/python -m web.app.feedback \
  --state .local-vertex/backend/state.json --port 8001
```

It exposes status-only `GET /health` and the same feedback router used by the web
app. To enable that router in the existing PostgreSQL-backed app instead, run:

```bash
cd web
FEEDBACK_STATE_PATH=../.local-vertex/backend/state.json PYTHONPATH=.. \
  ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001 --workers 1
```

Without `FEEDBACK_STATE_PATH`, the app does not load feedback. Use one backend
process/worker per JSON file; stop it before editing configuration and restart.

In another terminal at the repository root, run the standard-library client.
`--code-file` is required: replace the path below with your own UTF-8 source file
and use a configured target whose rubric matches that code.

```bash
env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S ai_grading/local_client.py \
  --backend-url http://127.0.0.1:8001 --token-file .local-vertex/client/token \
  --code-file /absolute/path/to/submission.py --target-id example \
  --guidance 'Explain one edge case and suggest a small improvement.' --json
```

The clean environment and `-I -S` exclude inherited Google settings and SDKs.
The client only reads its assignment token and address, and calls HTTPS or
loopback HTTP; it never discovers Google credentials or falls back to Google. The request
contains `request_id`, `target_id`, `code`, optional `guidance` and optional
`functional_test_feedback` (a list of diagnostic strings); the response
contains `request_id`, `feedback_text`, actual `model_name` and available `usage`.

Repeat the identical command to reuse its stable request ID and saved result.
Different content with the same ID is rejected. `--request-id` can set an explicit
ID; choose a new one only for an intentional new attempt. Each model attempt
persists a quota reservation first. Failures, timeouts, missing backend ADC and
interruptions keep that reservation; failed/interrupted IDs are not retried.
Restart preserves quota and results, including completed responses, in
`.local-vertex/backend/state.json`. Locks and atomic writes protect this file.

This prototype is single-worker, uses cumulative call counts, and has no
deployment or SQL feedback store. It separates credentials at the code/interface
boundary, not at the OS boundary: the same user or a local administrator can
still read backend files.

### Gradescope through a temporary HTTPS tunnel

The package sources and generated `vertex autograder.zip` are in the independent
`ai_grading/autograder-vertex-feedback/` directory, alongside the existing
`ai_grading/autograder-gemini-feedback/` directory.
The ZIP includes the original Java functional grader under `github/`, its
compilation-error templates, `setup.sh`, `run_autograder`, `vertex_feedback.py`,
`ai_feedback.json`, `local_client.py`, `config.json` and `assignment.token`. Every ZIP member is also
present as a loose file at the same relative path in that directory.
The ZIP and token are Git-ignored and mode `600`. Google credentials, Google SDKs,
GitHub deploy keys, SSH configuration and backend state are excluded.
`build.py` refreshes the client copy from `ai_grading/local_client.py` and generates
the URL config and token file. Generated copies and downloaded grader files stay
out of Git; the original functional tests are reused without editing them.

The feedback configuration keeps the Gemini grader's filename and source syntax:

```json
{
  "targets": [
    {
      "source": "**/SortedCollection.java",
      "target_id": "sorted-collection"
    }
  ]
}
```

`source` selects exactly one submitted file for each target. `target_id` selects
an authorized backend target and its rubric; it does not select an assignment.
The adapter processes each configured target and appends a zero-point feedback
row, always using the original visible test diagnostics. The rubric's
`criteria_text` and `feedback_guidance` retain their Gemini meanings but now live
in the backend JSON. `config.json` holds only the backend URL, and
`assignment.token` is the container's only authentication secret.

Only the feedback adapter is configurable across source files. This upload package
remains specific to SortedCollection: the compilation entry, Java functional
tests, JDK 11 setup and build whitelist must still be adapted for another assignment.
The backend and HTTP client can serve other configured assignments and targets;
they do not infer an assignment's build or testing procedure.

From the repository root, initialize a separate experiment **once**:

```bash
.venv/bin/python -m web.app.seed_feedback \
  --state .local-vertex/gradescope/backend/state.json \
  --token-file .local-vertex/gradescope/client/token \
  --client-config .local-vertex/gradescope/client/config.json \
  --max-calls 10 --expires-in-hours 24
```

Before starting the backend, add this target under `assignments.demo.targets`
in `.local-vertex/gradescope/backend/state.json`. It corresponds to the existing
CS101 assignment's `SortedCollection.java` submission. Keep the token records,
request records and `used_calls` unchanged when updating an existing experiment.
The rubric belongs only in this backend JSON, not in the upload package.

```json
"sorted-collection": {
  "identifier": "SortedCollection.java",
  "kind": "FILE",
  "source_path": "SortedCollection.java",
  "rubric": {
    "id": "sorted-collection-rubric",
    "version": 2,
    "criteria_text": "Explain the reported functional-test failures for SortedCollection using the submitted code. Consider ordered removal, handling invalid arguments, and removal from an empty collection only where supported by the visible test results. If all visible tests pass, acknowledge that and suggest useful code-quality improvements.",
    "feedback_guidance": "Give concise, actionable feedback. Point to relevant methods when possible. Distinguish confirmed failures from likely causes. Do not provide a complete replacement solution. Provide feedback only. Use SCORE: NONE; do not assign a grade."
  }
}
```

Start the backend (stop it before any later JSON configuration edits):

```bash
.venv/bin/python -m web.app.feedback \
  --state .local-vertex/gradescope/backend/state.json --port 8001
```

In another terminal, start a [Cloudflare Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/):

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8001 --no-autoupdate --protocol http2
```

Keep both processes running. Obtain the original functional grader once on the
build machine. This command uses the previously authorized local deploy key only
to fetch source; the key is not included in the upload package:

```bash
GIT_SSH_COMMAND='ssh -i ai_grading/autograder-gemini-feedback/deploy_key -o IdentitiesOnly=yes' \
  git clone --depth 1 --branch master \
  git@github.com:wonsunahn/CS1632_Autograder_JavaAssessment.git \
  .local-vertex/gradescope/functional-source
```

Skip cloning if that checkout already exists. The current package uses revision
`15425db409b8f938b8cc3959f21e5d5dc419265d`. `build.py` includes only the original
compile/run scripts, two Java grading files, four JARs, license and error templates.
The Gradescope container uses this snapshot instead of pulling GitHub at runtime,
so its only authentication secret is the assignment token. To refresh functional
tests, update this local checkout, rebuild and re-upload the ZIP.

Copy the printed HTTPS origin into the build command:

```bash
python3 ai_grading/autograder-vertex-feedback/build.py \
  --backend-url https://YOUR-TUNNEL.trycloudflare.com
```

On the existing CS101 assignment, upload
`ai_grading/autograder-vertex-feedback/vertex autograder.zip` on Configure
Autograder (Ubuntu 22.04). For Test Autograder, upload the previous
**`SortedCollection.java`** submission; a containing folder is also supported.
The submission must contain exactly one file with that name. The original flow
copies it, removes its package declaration, compiles with Java 11, then runs the
unchanged functional tests to produce `results.json`. Compilation errors use the
original templates and exit before any AI request.

After functional testing, `vertex_feedback.py` sends each configured source file
and the original visible test diagnostics to the backend. It appends one visible
AI feedback row per target with `score: 0` and `max_score: 0`, preserving all original result fields and
functional-test scores. AI failure only changes that row to an unavailable
message; it never replaces the original grade. The result update uses an atomic
file replacement. Hidden test diagnostics are not sent, matching the Gemini grader.
The same source and diagnostics produce the same request ID, so reruns reuse
saved feedback; changed test results produce a new request.

The tunnel URL changes when restarted: rebuild and upload the package with the
new URL, keeping the same backend state and token. The token expires 24 hours
after initialization. To issue a new experiment, use new state/token/config
paths and pass the new token path to `build.py --token-file`; never reset an
existing experiment to restore quota. Stop the backend and tunnel when finished.

## Database schema

Tables are defined in `app/models.py`, which mirrors
`docs/architecture/02-data-model.puml`. Migrations live in
`migrations/versions/` and are applied automatically when the Docker
container starts.

### Tables

Instructor configuration, set up per course (directly or by
`app.seed` until the instructor UI exists):

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
  feedback_config.py  JSON configuration and request record types
  feedback_store.py   JSON storage, quota reservation and idempotency
  main.py        FastAPI app: health, nightly, UI and optional feedback router
  models.py      ORM tables (02-data-model.puml)
  repository/    Data access used by the grading pipeline (below)
  seed.py        Sample data (see "Sample data" above)
  seed_feedback.py  Initialize JSON feedback state and a private assignment token
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
nightly trigger calls (shared-token auth), and the read-only instructor
pages. The optional Vertex feedback API uses assignment tokens and JSON storage
(see above); it is separate from the PostgreSQL grading pipeline.

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

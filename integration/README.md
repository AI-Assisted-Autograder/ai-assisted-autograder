# Gradescope Integration

## Overview

The Gradescope Integration automates nightly autograder reruns using Playwright and the Web API. It retrieves eligible submissions from the database, authorizes each submission for grading, triggers a rerun in Gradescope, and records the outcome.

The integration separates browser automation from grading logic through shared interfaces, production adapters, and test doubles. It also includes a runner for executing nightly cycles and scripts for scheduling them on Windows.

## System Components

- **Gradescope Platform:** Uses Playwright to authenticate, navigate to a submission, and trigger an autograder rerun.
- **Web API Client:** Retrieves eligible submissions, authorizes grading, and records nightly run results through protected API endpoints.
- **Nightly Trigger:** Coordinates the workflow by authorizing each submission before triggering its rerun.
- **Interfaces and Test Doubles:** Define component boundaries and support browser-free orchestration tests.
- **Nightly Runner and Scheduler:** Execute a trigger cycle manually or through scheduled automation.

## Documentation Guide

- [Current Status](#current-status) — Review implemented functionality and the current integration workflow.
- [Files](#files) — Find the main source files and their responsibilities.
- [Safety Requirements](#safety-requirements) — Review credential handling and safeguards for live reruns.
- [Direct Rerun Test](#direct-rerun-test) — Test a rerun against a specifically selected submission.
- [Database Bridge](#database-bridge) — Configure the Web API, database connection, and nightly submission workflow.
- [Verification](#verification) — Run tests, check types, and validate the nightly trigger.
- [Windows Nightly Scheduling](#windows-nightly-scheduling) — Configure or remove the scheduled nightly task.

## Getting Started

Start by reviewing the [Safety Requirements](#safety-requirements). To test the integration locally, follow [Database Bridge](#database-bridge) to start the Web API and database, then use [Verification](#verification) to check the implementation and run a controlled nightly cycle.

## Current status

The current implementation is a rerun prototype:

- `GradescopePlatform` can authenticate with a non-SSO account, open an
  explicitly selected submission, and click `Rerun Autograder`.
- `ILearningPlatform` and `IGradingClient` define the boundaries used by the
  trigger, with `GradescopePlatform` and `HttpGradingClient` as production
  adapters.
- `FakePlatform` and `FakeGradingClient` provide browser-free test doubles for
  orchestration tests.
- `NightlyTrigger` authorizes and submits every pending rerun first, then polls
  the accepted reruns in a separate completion phase.
- The runner supports two sources: PENDING_NIGHTLY for the initial nightly
  batch and AI_FAILURES for retrying only submissions returned by the
  backend's AI-failure endpoint.
- Each submission failure now includes a phase (authorization, trigger, or
  completion) and a coarse kind such as DATABASE, TIMEOUT, AI_FEEDBACK, or
  AUTOGRADER. These labels help operations triage failures; the backend remains
  the source of truth for AI grading state.
- A successful Gradescope click is counted as a submitted rerun even if the
  follow-up audit write fails. The summary exposes that problem as auditErrors
  so a database/API compatibility issue cannot be reported as a false
  Gradescope failure.
- The runner also emits an outcome timeline per submission:
  AUTHORIZED -> RERUN_SUBMITTED -> RERUN_SUCCEEDED on the normal path.
  Failures are labeled AUTHORIZATION_FAILED, RERUN_TRIGGER_FAILED, or
  RERUN_COMPLETION_FAILED. RERUN_SUCCEEDED means the Gradescope/autograder
  phase completed; it does not by itself prove that every AI target produced
  feedback.
- `GradingApiClient` reads pending `NIGHTLY` submissions, starts an audit run,
  authorizes submissions, and writes the aggregate result back to the Web API.
- The intended batch flow is database-driven: the Web API returns each eligible
  submission's assignment, submission ID, and platform URL. The trigger first
  submits all accepted records, records `RERUN_SUBMITTED`, and only then waits
  for completion. A slow autograder therefore does not block submission of the
  remaining batch.
- The Web API permits only one `RUNNING` nightly cycle at a time and records
  each submission's authorization, success, or failure separately.
- `nightly-runner.ts` is the scheduler entrypoint. A cron or GitHub Actions
  workflow can invoke it later.

## Files

| File | Responsibility |
| --- | --- |
| `gradescope-platform.ts` | Gradescope login, submission navigation, selectors, and rerun click |
| `interfaces.ts` | `ILearningPlatform`, `IGradingClient`, and shared integration types |
| `nightly-trigger.ts` | Authorize-then-rerun orchestration and run summary |
| `grading-api-client.ts` | `HttpGradingClient`, the HTTP bridge to the Web API and SQL repository |
| `fake-platform.ts` | Browser-free platform test double |
| `fake-grading-client.ts` | In-memory database/API test double |
| `nightly-runner.ts` | Creates Chromium and runs one nightly trigger cycle |

## Safety requirements

Credentials are read only from environment variables. Never put them in a
TypeScript file, `.env` committed to Git, test output, or a commit.

Live reruns are locked unless the caller explicitly sets:

```powershell
$env:GRADESCOPE_ALLOW_RERUN="1"
```

The platform adapter also accepts only HTTPS URLs under
`www.gradescope.com`.

After clicking `Rerun Autograder`, the adapter waits for a visible success
message before recording `RERUN_SUBMITTED`; the click itself is not treated as
proof that Gradescope accepted the request. Completion polling happens after
the submission phase. If the site uses different wording, set a regular
expression in `GRADESCOPE_RERUN_SUCCESS_TEXT`.

## Direct rerun test

From the repository root, set the credentials and the exact submission URL:

```powershell
$env:GRADESCOPE_USERNAME="your non-SSO email"
$env:GRADESCOPE_PASSWORD="your password"
$env:GRADESCOPE_SUBMISSION_URL="https://www.gradescope.com/courses/.../assignments/.../submissions/..."
$env:GRADESCOPE_ALLOW_RERUN="1"

npm run test:gradescope-rerun
```

The test is skipped unless both the submission URL and the explicit rerun
authorization flag are present.

## Database bridge

The Web application connects to the PostgreSQL service defined in
`web/docker-compose.yml`. The first Docker startup runs the Alembic migrations
automatically. The integration API is protected by `X-Internal-Token` and
exposes the operations needed before and after the browser action:

```text
GET  /internal/nightly/submissions?mode=NIGHTLY
GET  /internal/ai-failures/submissions
POST /internal/nightly/submissions/{submission_id}/authorize
POST /internal/nightly/runs
POST /internal/nightly/runs/{run_id}/submissions/{submission_id}
POST /internal/nightly/runs/{run_id}/finish
```

Configure the client with:

```powershell
$env:GRADING_API_URL="http://localhost:8000"
$env:INTEGRATION_API_TOKEN="the same token configured for the Web API"
$env:GRADESCOPE_COURSE_ID="your Gradescope course id"
```

The API query filters for active `NIGHTLY` assignments, active submissions,
AI-needed targets that are not completed, and submissions that have not
already been AI-graded.

For an AI retry cycle, set NIGHTLY_TRIGGER_SOURCE=AI_FAILURES. The runner then
calls GET /internal/ai-failures/submissions and reruns only the returned
submissions. The endpoint is the backend source of truth for ai_status=FAILED;
the Playwright layer does not infer AI failure from the Gradescope page.

The database is the source of the work list, but it does not itself perform a
Gradescope rerun. A submission row represents the work to process; the actual
rerun still happens through the authenticated Gradescope platform adapter. For
each eligible row, the batch flow is:

```text
database query
  -> submission and assignment identifiers / platform URL
  -> authorize the submission in the API
  -> open the Gradescope submission with Playwright
  -> More -> Rerun Autograder -> confirm
  -> record RERUN_SUBMITTED
  -> poll all accepted reruns
  -> record final success or failure for each submission
```

Gradescope reruns are submission-level operations: the browser can rerun a
whole submission, not one individual AI target. A target is the backend's
logical grading scope (for example, one configured file or group of files),
not a separate Gradescope rerun button. The current integration therefore
supports selecting and rerunning submissions, while the Web API/backend owns
target-level AI status and retry decisions. Target-only retry is intentionally
not claimed until the backend exposes a target-level failed-query and retry
contract.

For that reason, a platform outcome and an AI outcome must not be conflated:
the integration can report that Gradescope finished, while the backend must
report whether each AI target succeeded, failed, or needs retry. The current
HTTP contract still writes the per-submission error message and aggregate run
counts; persistent failure categories and target retry metadata require the
corresponding backend fields and endpoints.

After Gradescope starts the autograder, the AI hook completes the other half of
the flow. It reads `/autograder/submission_metadata.json`, sends the platform
submission ID and timestamp with each `/v1/feedback` request, and the Web API
stores the selected `AiFeedback` plus the target completion state. When all
configured targets have either succeeded or been recorded as failed, the hook
calls:

```text
POST /v1/submissions/{platform_submission_id}/ai-complete
```

The endpoint sets the database `ai_graded_submitted_at` guard only when no
AI-needed target is still waiting. This is what allows the Web UI to show the
submission as graded and prevents the nightly query from selecting it again.
The endpoint is protected by the same per-assignment Bearer token used by
`/v1/feedback`; it is not a public unauthenticated write endpoint.

The course ID scopes the query, while the assignment ID and platform
submission URL identify the exact Gradescope page. Credentials are shared by
the authenticated browser session and are never stored in submission records.

### Start the local database and Web API

From the repository root, run:

```powershell
$env:INTEGRATION_API_TOKEN="local-development-token"
docker compose -f web\docker-compose.yml up --build -d
docker compose -f web\docker-compose.yml ps
```

The API should then be available at `http://localhost:8000/docs`. The local
database starts with the schema but no course, assignment, or submission
records. Records must be imported or created before a nightly run can find
work; starting Docker does not synchronize data from Gradescope.

For local testing, load the sample data, which includes two submissions
the nightly query returns (see "Sample data" in `web/README.md`):

```powershell
docker compose -f web\docker-compose.yml exec web python -m app.seed
```

`web/.env.example` sets the token to `local-development-token`, so the
`$env:INTEGRATION_API_TOKEN` line above is only needed to override it.

To stop the services:

```powershell
docker compose -f web\docker-compose.yml down
```

## Verification

List the configured Playwright tests:

```powershell
npx playwright test --list
```

Run the TypeScript check:

```powershell
npm run typecheck
```

Run one complete nightly cycle:

```powershell
$env:GRADESCOPE_USERNAME="your non-SSO email"
$env:GRADESCOPE_PASSWORD="your password"
$env:GRADING_API_URL="http://localhost:8000"
$env:INTEGRATION_API_TOKEN="the same token configured for the Web API"
$env:GRADESCOPE_COURSE_ID="your Gradescope course id"
$env:GRADESCOPE_RERUN_SUCCESS_TEXT="optional regex matching the Gradescope success message"
$env:GRADESCOPE_ALLOW_RERUN="1"
$env:NIGHTLY_TRIGGER_SOURCE="PENDING_NIGHTLY"

npm run nightly:trigger
```

This command may rerun every eligible `NIGHTLY` submission returned by the
database. Test it against a controlled database before scheduling it.

The single-submission Playwright rerun has been validated against a real
Gradescope submission. The database-driven query and API orchestration have
also been validated with local sample records, but sample IDs are not real
Gradescope submissions. A full production batch run requires real imported
submission records and should be tested with a controlled assignment first.

The unit tests also verify that the trigger submits all pending submissions
before waiting, continues after an individual failure, classifies failures by
phase, and does not turn a post-click audit-write error into a false rerun
failure.

After clicking `Rerun Autograder`, the adapter polls the submission page until
Gradescope shows completed autograder results. A queued or started message is
not recorded as final success. The default wait is 30 minutes because a real
rerun includes the full autograder and AI feedback phases. Configure it with
`GRADESCOPE_RERUN_TIMEOUT_MS` (default `1800000`) and
`GRADESCOPE_RERUN_POLL_INTERVAL_MS` (default five seconds).

The login test is read-only with respect to Gradescope. The rerun test changes
Gradescope state and must be run only for an intentionally selected submission.

For an AI-failure retry cycle, set:

    $env:NIGHTLY_TRIGGER_SOURCE="AI_FAILURES"
    npm run nightly:trigger

This reruns only the submissions returned by
/internal/ai-failures/submissions. Test either mode against a controlled
assignment before scheduling it.

## Running the nightly trigger on another machine

The nightly trigger can run from any machine that has Node.js, Chromium, and
network access to both the Web API and Gradescope. The Web API/database and the
Playwright runner are separate components; starting one does not automatically
start the other.

Install the runner dependencies from the repository root:

```powershell
npm ci
npx playwright install chromium
```

Configure the runner through environment variables. Do not commit these values
or place them in a tracked README or TypeScript file:

```powershell
$env:GRADING_API_URL="http://127.0.0.1:8000"
$env:INTEGRATION_API_TOKEN="the Web API internal token"
$env:GRADESCOPE_COURSE_ID="the course ID used to build submission URLs"
$env:GRADESCOPE_ALLOW_RERUN="1"
```

For a headless server, use a server-side authenticated Playwright state file:

```powershell
$env:GRADESCOPE_STORAGE_STATE="/run/secrets/gradescope.json"
$env:HEADFUL="0"
```

The state file must be created securely on the server by running the
one-time login helper. Never commit `.auth/gradescope.json`, copy it into the
repository, or print its contents. The Gradescope account must have instructor
access to the selected course and assignment.

Run one controlled cycle manually:

```powershell
npm run typecheck
npm run nightly:trigger
```

The command prints a JSON summary containing the number found, authorized,
submitted, and failed, plus per-submission outcomes. It may trigger real
Gradescope reruns when `GRADESCOPE_ALLOW_RERUN=1` is set, so use a controlled
NIGHTLY assignment first.

For a server deployment, run only one nightly runner at a time and redirect
output to a protected log file:

```bash
mkdir -p logs
npm run nightly:trigger >> logs/nightly-trigger.log 2>&1
```

The Windows Task Scheduler scripts in `scripts/` can register the same command
on Windows. On Linux/Oracle, use the server's approved cron or systemd
configuration instead of creating duplicate schedulers.

The log currently contains the detailed integration outcome timeline. The
database stores the existing submission/run status and error message, but it
does not yet persist every `failure_kind`, phase, or retry field. Those fields
require the corresponding backend schema and API changes before they can be
displayed reliably to all users.

## Windows nightly scheduling

The repository includes a configurable Windows Task Scheduler wrapper. Copy
the local environment template and fill in the credentials and course ID:

```powershell
Copy-Item .env.local.example .env.local
```

Register the trigger for 2:00 AM local time:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register-nightly-trigger.ps1 -At "02:00"
```

The time is configurable in 24-hour local time. For example, to change it to
1:30 AM, run the same command with `-At "01:30"`. The task runs
`scripts/run-nightly-trigger.ps1`, which loads `.env.local`, invokes
`npm run nightly:trigger`, and appends output to `logs/nightly-trigger.log`.

The task requires Docker Desktop and the Web API/database services to be
running. Remove it with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\unregister-nightly-trigger.ps1
```

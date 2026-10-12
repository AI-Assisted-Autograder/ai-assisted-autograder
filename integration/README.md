# Gradescope integration

Playwright-based integration code for the Gradescope platform boundary. This
directory owns browser-specific behavior and trigger orchestration; it does
not contain AI provider code or the instructor's autograder implementation.

## Current status

The current implementation is a rerun prototype:

- `GradescopePlatform` can authenticate with a non-SSO account, open an
  explicitly selected submission, and click `Rerun Autograder`.
- `NightlyTrigger` runs the orchestration order: authorize a submission first,
  then click the rerun button.
- `GradingApiClient` reads pending `NIGHTLY` submissions, starts an audit run,
  authorizes submissions, and writes the aggregate result back to the Web API.
- `nightly-runner.ts` is the scheduler entrypoint. A cron or GitHub Actions
  workflow can invoke it later.

## Files

| File | Responsibility |
| --- | --- |
| `gradescope-platform.ts` | Gradescope login, submission navigation, selectors, and rerun click |
| `nightly-trigger.ts` | Authorize-then-rerun orchestration and run summary |
| `grading-api-client.ts` | HTTP bridge to the Web API and SQL repository |
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
POST /internal/nightly/submissions/{submission_id}/authorize
POST /internal/nightly/runs
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
$env:GRADESCOPE_ALLOW_RERUN="1"

npm run nightly:trigger
```

This command may rerun every eligible `NIGHTLY` submission returned by the
database. Test it against a controlled database before scheduling it.

The login test is read-only with respect to Gradescope. The rerun test changes
Gradescope state and must be run only for an intentionally selected submission.

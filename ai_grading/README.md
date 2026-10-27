# AI Grading

## Overview

The AI Grading component generates AI-assisted feedback for student code submissions. It combines a reusable Python core with a Gradescope client and integrates with the Web backend to process submissions using configurable AI models.

The Web backend manages assignment configuration, authentication, provider credentials, and request limits. Gradescope sends source code and relevant test or coverage evidence to the backend, which returns feedback and optional AI-generated scores. Supported engines are Vertex, Pitt, and the Gemini Developer API.

## System Components

- **Python Core:** Resolves source files, builds prompts, coordinates AI providers, and selects feedback.
- **Web Backend:** Retrieves assignment and rubric configuration, authenticates requests, enforces limits, and calls the configured AI model.
- **Gradescope Client:** Sends source code and test evidence to the backend using an assignment-specific token.
- **Autograder Packages:** Integrate AI feedback into grading workflows while preserving functional scores when AI feedback is unavailable.
- **AI Provider Adapters:** Support Vertex, Pitt, Gemini Developer API, and an offline fake provider for testing.

Provider credentials remain on the backend; Gradescope packages cannot select models or call providers directly.

## Documentation Guide

- [Current Flow](#current-flow) — Understand how feedback requests are processed.
- [Use the Backend](#use-the-backend) — Send test requests and configure the Gradescope client.
- [RentACat Feedback](#rentacat-feedback) — Review the RentACat integrations and scoring behavior.
- [Module Layout](#module-layout) — Find key files and their responsibilities.
- [Reuse the Python Core](#reuse-the-python-core) — Integrate the grading logic into other applications.
- [Current Limits](#current-limits) — Review known limitations and outstanding verification.

For backend configuration and deployment, see the [Web deployment guide](../web/README.md#deploy-the-combined-web-and-ai-backend).

## Getting Started

Configure an assignment, AI engine, model, and assignment token using the [Web deployment guide](../web/README.md#deploy-the-combined-web-and-ai-backend). Then follow [Use the Backend](#use-the-backend) to test the API or configure a Gradescope package.

## Current flow

1. The Gradescope connector sends source files, a DB target UUID and selected
   runtime/coverage evidence to `POST /v1/feedback`, using an assignment token.
2. The Web backend authenticates the token, reads assignment/target/rubric/model
   configuration from PostgreSQL, checks limits and calls the selected engine.
3. Gradescope receives feedback rows and, when configured in DB, bounded AI scores. This path
   does not write submissions, test results or feedback to DB. Backend JSON keeps
   token hashes, call quota and replay responses.

All provider credentials stay on the backend. The Gradescope package contains
only an assignment token; it cannot choose an engine/model or call providers directly.

| Engine | Backend credentials | Adapter and model selection |
| --- | --- | --- |
| `vertex` | Google IAM through impersonation ADC | `VertexProvider` reuses `GeminiProvider`; Vertex Gemini model ID |
| `pitt` | `PITT_API_KEY` | `PittProvider`; full project-authorized `@.../...` model ID |
| `gemini_api` | `GEMINI_API_KEY` | `GeminiProvider`; Gemini Developer API model ID |

Pitt uses `POST https://api.portkey.ai/v1/chat/completions` with
`x-portkey-api-key`, a `model` and `messages`. One adapter supports the project's
GPT, Claude and Gemini routes. Gemini API free-tier access depends on the account,
model and quota; `gemini_api` also supports paid access. The old direct OpenAI and
Claude adapters have been removed.

## Use the backend

Follow the [Web deployment guide](../web/README.md#deploy-the-combined-web-and-ai-backend)
to configure the DB assignment, engine/model, credentials and assignment token.
For a local backend process, install `web/requirements.txt`; Docker installs those
dependencies during its build.

For Vertex Gemini 3.8 Flash, stop the backend before adding that model to the
active state's `config.allowed_models.vertex`, using `location=global`; preserve
tokens, quota and replay records. Select `engine=vertex`, `provider=GOOGLE`,
`model_name=gemini-3.8-flash` in the primary `llm_provider_config` row, then start
the backend with the updated adapter. This selection applies to all assignments.

The state's global `config.max_output_tokens` defaults to 16,384 (maximum 65,536)
for all assignments and targets. Gemini thinking and visible feedback share this
budget.

The global model timeout defaults to 240 seconds. The client waits up to 270
seconds and polls the same authenticated request while the backend generates
feedback, avoiding a long idle HTTPS request without reserving another call.

The HTTP client uses only the Python standard library. From the repository root:

```bash
python3 -I -S ai_grading/local_client.py \
  --backend-url https://YOUR-SERVER \
  --token-file ai_grading/.local-vertex/server-client/assignment.token \
  --code-file /absolute/path/to/SortedCollection.java \
  --target-id YOUR_TARGET_UUID --json
```

Supply `--code-file` or `--files-json` (an array of `{path, content}`), with optional
`--evidence-json`; `--target-id` is the DB target UUID. Public backend
URLs must use HTTPS.

## RentACat feedback

- Playwright: testplan analysis of 11 tests, 5 AI points per
  test, alongside the unchanged functional grader. The backend derives scores
  from verdicts; an insufficient-evidence verdict remains unscored.
- Java: feedback for unit, integration, implementation and
  system-test targets, using runtime diagnostics and aggregate JaCoCo coverage.
  Its AI feedback is unscored; existing functional and coverage scores remain intact.

The shared hook collects configured artifacts after functional grading, including error paths,
and preserves functional scores when AI is unavailable. Instructor evidence is
kept in `results/ai_evidence.json` and the autograder log, outside student rows.
Playwright requires one matching primary source; ambiguous copies receive no AI
score. The AI phase defaults to a 300-second total budget, with each request
limited by the remaining time. The server Java package gives its five sequential
requests a shared 480-second budget within the assignment's 10-minute limit.
Set Gradescope's Outline maximum explicitly; adding AI rows does not change it.

The response identifies `engine`, vendor `provider`, `requested_model`, returned
`model_name`, feedback and available usage. For each independent repeat or model
comparison, supply a new `--request-id`, for example `pitt-gpt51-trial-001`.
Reuse the same ID only to retry/check the same request. The default content hash
replays cached feedback even after an engine, model or rubric change. Fix the code,
rubric and prompt inputs when comparing predictability across repeated generations.
Pitt requests force a fresh gateway call with `x-portkey-cache-force-refresh: true`.
The two server assignment packages use new Gemini 3.8 Flash request namespaces.
Upload their updated ZIPs to regenerate feedback for unchanged submissions; retries
within the same namespace still reuse their original results.

## Module layout

| Location | Responsibility |
| --- | --- |
| [local_client.py](local_client.py) | Assignment-token HTTP client and CLI |
| [gradescope_feedback.py](gradescope_feedback.py) | Shared evidence collection and result rendering |
| [autograder-vertex-feedback/](autograder-vertex-feedback/) | Legacy Gradescope connector and functional grader |
| [models.py](models.py) | Typed source, target, rubric, request and response contracts |
| [FileResolver.py](FileResolver.py) | Resolve a target to one source file without executing student code |
| [PromptBuilder.py](PromptBuilder.py) | Build prompts from code, rubric and test diagnostics |
| [ILLMProvider/](ILLMProvider/) | Vertex, shared Gemini, Pitt Chat Completions and offline FakeProvider adapters |
| [AIGradingComponent.py](AIGradingComponent.py) | Reusable provider orchestration, retries and result selection |
| [web/app/](../web/app/) | HTTP routes, DB configuration, token/quota state and backend credentials |

## Reuse the Python core

Install `ai_grading/requirements.txt` to use the core and provider adapters directly.
Construct a `FeedbackRequest` with `target`, `rubric`, `resolved` source and
`functional_test_feedback`. `FileResolver.resolve(...)` can supply the source;
`AIGradingComponent(providers).grade(request)` returns provider responses and a
selected result. The default selector chooses the first provider, without quality
ranking; `FakeProvider` supports offline use.

The Web route selects a backend provider using the configured engine and model;
`VertexProvider` owns the IAM client and delegates to `GeminiProvider`; Gemini API
uses `GeminiProvider` directly, while Pitt uses `PittProvider`. All adapters live
in `ILLMProvider/` and receive explicit settings without depending on Web types.
`AIGradingComponent` is available for other integrations. Optional target scores
and configured per-test scores reach the HTTP client; comparison fields remain
outside this flow. Structured responses separate student feedback and teacher evidence.

## Current limits

- The legacy Vertex ZIP uses SortedCollection. The two RentACat packages use the
  shared connector; other assignments still supply their own functional grader and hook.
- AI failure preserves functional scores. Only the legacy SortedCollection package
  skips AI on compilation failure.
- A local-backend Gradescope-to-Vertex run was verified. Deployment and the full
  server DB-to-Vertex-to-Gradescope flow still need verification.
- Local Docker feedback through all three engines has been verified with live
  models. Each deployment still needs its own credential/access check.
  The historical `autograder-gemini-feedback/` package calls Google
  directly; use the backend client above for the selectable-engine architecture.

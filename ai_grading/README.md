# AI grading (Ivan)

AI feedback for submitted code, with a reusable Python core and a Gradescope
client. The current integration uses the Web backend and Google Vertex AI.

## Current flow

1. The Gradescope connector sends source code, a DB target UUID and visible
   functional-test diagnostics to `POST /v1/feedback`, using an assignment token.
2. The Web backend authenticates the token, reads assignment/target/rubric/model
   configuration from PostgreSQL, checks limits and calls Vertex.
3. Feedback is appended to Gradescope `results.json` as a `0/0` row. This path
   does not write submissions, test results or feedback to DB. Backend JSON keeps
   token hashes, call quota and replay responses.

Google credentials stay on the backend, which uses service-account impersonation
ADC. The Gradescope package contains only an assignment token for authentication;
it does not need Google credentials or a provider SDK.

## Use the backend

Follow the [Web deployment guide](../web/README.md#deploy-the-combined-web-and-ai-backend)
to configure the DB assignment, model, Google identity and assignment token.
For a local backend process, install `web/requirements.txt`; Docker installs those
dependencies during its build.

The HTTP client uses only the Python standard library. From the repository root:

```bash
python3 -I -S ai_grading/local_client.py \
  --backend-url https://YOUR-SERVER \
  --token-file ai_grading/.local-vertex/server-client/assignment.token \
  --code-file /absolute/path/to/SortedCollection.java \
  --target-id YOUR_TARGET_UUID --json
```

`--code-file` is required; `--target-id` is the DB target UUID. Public backend
URLs must use HTTPS. For Gradescope, map `targets[].source` to the submitted file
and `targets[].target_id` to that UUID, then follow the
[packaging steps](../web/README.md#package-and-run-in-gradescope).
The build copies the canonical `local_client.py` into the ZIP; keep tokens and
generated ZIPs out of Git.

## Module layout

| Location | Responsibility |
| --- | --- |
| [local_client.py](local_client.py) | Assignment-token HTTP client and CLI |
| [autograder-vertex-feedback/](autograder-vertex-feedback/) | Gradescope connector, functional-grader package and ZIP builder |
| [models.py](models.py) | Typed source, target, rubric, request and response contracts |
| [FileResolver.py](FileResolver.py) | Resolve a target to one source file without executing student code |
| [PromptBuilder.py](PromptBuilder.py) | Build prompts from code, rubric and test diagnostics |
| [ILLMProvider/](ILLMProvider/) | Gemini, OpenAI, Claude and offline FakeProvider adapters |
| [AIGradingComponent.py](AIGradingComponent.py) | Reusable provider orchestration, retries and result selection |
| [web/app/](../web/app/) | HTTP routes, DB configuration, token/quota state and Vertex credentials |

## Reuse the Python core

Install `ai_grading/requirements.txt` to use the core and provider adapters directly.
Construct a `FeedbackRequest` with `target`, `rubric`, `resolved` source and
`functional_test_feedback`. `FileResolver.resolve(...)` can supply the source;
`AIGradingComponent(providers).grade(request)` returns provider responses and a
selected result. The default selector chooses the first provider, without quality
ranking; `FakeProvider` supports offline use.

The current Web route calls `VertexProvider` → `GeminiProvider` directly.
`AIGradingComponent` is available for other integrations; its optional scores and
comparison fields are not exposed by the current Gradescope feedback flow.

## Current limits

- The supplied ZIP uses the SortedCollection/Java grader. Other assignments need
  an instructor-provided functional grader and invocation hook.
- AI failure preserves functional scores; compilation failure currently skips AI.
- A local-backend Gradescope-to-Vertex run was verified. Deployment and the full
  server DB-to-Vertex-to-Gradescope flow still need verification.
- OpenAI/Claude adapters and the older `autograder-gemini-feedback/` package remain
  in the repository. They are not the current Vertex integration path; their live
  integrations are not all verified.

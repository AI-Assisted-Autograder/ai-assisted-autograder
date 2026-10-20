# Architecture

UML documentation for the Automated Code Feedback system. Sources are
PlantUML (`.puml`); rendered SVG and PNG are committed under `rendered/`
by the `Render UML` GitHub Actions workflow.

On `vertex-backend-refactor`, the [web Vertex feedback
API](../../web/README.md#vertex-feedback-api-local-prototype) supersedes AD-4 for its client path:
clients hold assignment tokens, and the backend owns Google authentication,
proxies Vertex calls, and persists quota and results in JSON. The diagrams below
still describe the earlier architecture; this local experiment does not change
the existing nightly or container flow.

Edit the `.puml` sources, never the images. The workflow re-renders on
every push to `main` and fails a pull request whose committed images are
stale.

## Diagrams

| File | Type | What it answers |
| --- | --- | --- |
| `01-component.puml` | Component | The modules, who owns each, and the interfaces between them |
| `02-data-model.puml` | ERD | The database schema, and what deliberately is not in it |
| `03-sequence-immediate.puml` | Sequence | A submission run in IMMEDIATE mode |
| `04-sequence-nightly.puml` | Sequence | NIGHTLY mode, across submission and triggered rerun |
| `05-sequence-instructor.puml` | Sequence | Registering targets and reviewing results |
| `06-submission-target-state.puml` | State machine | Which targets want AI feedback, and how retries work |
| `07-interfaces.puml` | Class | The exact signatures each student codes against |

Read `03` first; it is the whole system in one page. Then `02` and `07`,
which are the ones that constrain other people's code.

## The three things to internalise

**We ship a library, not a runner.** The instructor's own JUnit or pytest
suite *is* the autograder. Her `run_autograder` then invokes our AI phase
as a separate command, in every run. We never invoke her tests. (AD-2)

**The AI phase holds no policy.** It asks the service for one action,
`GRADE`, `DEFER` or `REEMIT`, and performs it. Grading mode, the nightly
authorization flag and the token guard are all derived server-side,
because that code ships inside the instructor's autograder zip and is the
hardest thing to update. (AD-11)

**Functional test outcomes are not stored.** They go to `results.json`
through her existing framework, and the AI phase parses them back to
build prompts. The AI phase splices feedback onto the end of that file
rather than authoring it. (AD-6)

**`ai_needed` defaults ON.** Every registered target wants AI feedback;
her suite calls `ai_not_needed(target)` to opt individual ones out,
usually because the test passed. There is no policy enum. This is
fail-open on cost, so a forgotten opt-out is expensive. (AD-7)

## Cost and correctness

`submission_target.ai_needed` asks whether a target wants feedback. It starts
TRUE and her suite turns targets off, so this is the main cost lever and
it lives in her code.

`submission.ai_graded_submitted_at` asks whether a submission is
finished. The nightly trigger filters on it, so a graded submission is
never rerun, and the AI phase re-checks it to catch reruns the trigger
did not cause. (AD-9)

`results.json` is written before grading starts, so an AI failure cannot
cost the student their test results (AD-5). We never parse it back; it is
read once at the end only to splice feedback in (AD-6).

**Her suite reports outcomes; we never derive them.**
`add_test_result(target, status, functional_test_feedback)` stores
`status` for the dashboard, which is the only functional pass/fail record
the system holds, and `functional_test_feedback` for the prompt (AD-10).
Prompts carry the rubric, the source file derived from the target
identifier (AD-12), a listing of the other files, and that feedback
string.

`functional_test_feedback` is **untrusted**: assertion messages routinely
embed the student's own output, so it is a prompt injection surface. Delimit it,
label it as test output, cap its length, and keep the rubric
authoritative.

## Failure handling

Failures are classified before retry. Transient ones (429, 5xx, quota)
are worth retrying; deterministic ones (context overflow, refusal,
unparseable output) are not, since they reach the same answer at three
times the cost.

`IMMEDIATE` never defers: after in-run retries the AI phase appends a
note to `results.json` saying feedback could not be generated. Only
`NIGHTLY` retries across nights. (AD-8)

The taxonomy in `DECISIONS.md` matters more than it looks. The most
likely failure is rate limiting during the deadline-hour spike, against
one shared instructor key, which hits many students at once.

## Ownership

| Student | Owns | Publishes | Consumes |
| --- | --- | --- | --- |
| A | AI grading phase, LLM adapters, prompts, confidence | `ILLMProvider`, `FakeProvider` | `IGradingClient` |
| B | Web frontend and backend, DB schema, migrations, repository | `IGradingClient`, `IGradingRepository`, fakes for both | -- |
| C | Reporting library (one method), AI phase entrypoint, results splicer, nightly trigger | `IReportingClient`, `ILearningPlatform`, fakes for both | `IGradingClient` |
| D | SRS, test suite, CI and cron workflows, documentation | CI workflows, fixtures | all of the above |

Two rules that follow:

1. **Student B is the only person who writes SQL.** A and C reach data
   through `IGradingClient` over HTTPS, because their code runs in the
   platform's container, not ours.
2. **`IGradingClient` and `IGradingRepository` stay separate
   interfaces.** Collapsing them means shipping database credentials in
   the autograder zip. The container holds the instructor's LLM API key
   (AD-4) but never a database credential.

## Rendering locally

```bash
./docs/architecture/render.sh          # SVG + PNG into rendered/
./docs/architecture/render.sh --check  # validate only, no output
```

Requires `java` and `graphviz`; the PlantUML jar downloads itself on
first run. You do not need any of this to contribute: edit a `.puml` and
push, and CI renders it. `--check` is worth running before pushing,
because it catches two things PlantUML itself reports only as "some
diagram description contains errors": `@startuml` names drifting from
filenames, and floating `note as` in a sequence diagram, where notes must
be attached with `note over`.

## Editor support

- VS Code: `jebbs.plantuml`, preview with `Alt+D`. Set `plantuml.render`
  to `Local` and point `plantuml.jar` at `~/.plantuml/plantuml.jar` so
  your source is not sent to a public server.
- IntelliJ: the PlantUML Integration plugin.

## Conventions

- One diagram per file, prefixed with a two-digit order. The
  `@startuml <name>` directive must match the filename, since PlantUML
  names its output from the directive rather than the file.
- Requirements a diagram implements are cited by ID in a comment at the
  top, so a reader can trace back to the SRS.
- Open decisions are yellow notes inside the diagram they affect, and
  tracked in `DECISIONS.md`. Resolving one means updating both.
- Colours are ownership, not layer. Yellow is the instructor's own code,
  which we neither write nor invoke.

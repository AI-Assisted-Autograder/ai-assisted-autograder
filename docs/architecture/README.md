# Architecture

UML documentation for the Automated Code Feedback system. Sources are
PlantUML (`.puml`); rendered SVG and PNG are committed under
`rendered/` by the `Render UML` GitHub Actions workflow.

Edit the `.puml` sources, never the images. The workflow re-renders on
every push to `main` and fails a pull request whose committed images
are stale.

## Diagrams

| File | Type | What it answers |
| --- | --- | --- |
| `01-component.puml` | Component | The modules, who owns each, and the interfaces between them |
| `02-data-model.puml` | ERD | The database schema, which every module shares |
| `03-sequence-immediate.puml` | Sequence | A submission run, with IMMEDIATE targets graded inline |
| `04-sequence-nightly.puml` | Sequence | NIGHTLY targets, graded on a per-submission rerun |
| `05-sequence-instructor.puml` | Sequence | Configuring an assignment and reviewing results |
| `06-grading-task-state.puml` | State machine | Which targets need AI feedback, who decides, and how retries work |
| `07-interfaces.puml` | Class | The exact signatures each student codes against |

Read 03 and 04 first. The two grading modes are the central design
decision (see `DECISIONS.md`, AD-1) and everything else follows from
them. After that, `02` and `07` are the two you will keep returning to,
because they constrain other people's code.

## The two things to internalise

**We ship a library, not a runner.** The instructor's own JUnit or
pytest suite is the autograder. It imports our reporting library and
calls `record_outcome` and `request_ai_feedback` with the target names
she registered in the web app. We never invoke her tests. Her
`run_autograder` then invokes our AI phase as a separate command, which
resolves the marks and writes `results.json`. See `DECISIONS.md`, AD-2.

**Feedback mode is per target.** `IMMEDIATE` targets are graded during
the submission run, `NIGHTLY` ones on a triggered rerun, and one
submission can mix both. See AD-4.

## Cost control

The AI grading component runs **inside the LMS autograder container in
both modes**. The only difference is when. The nightly job logs in and
clicks "Rerun Autograder"; it does not generate feedback.

Two fields do the cost control, and they answer different questions.

`grading_task.ai_needed` answers *does this target deserve feedback*.
The AI phase resolves it from `ai_feedback_policy`
(`AUTOGRADER`, `ALWAYS`, `NEVER`) and, under `AUTOGRADER`, from the
requests the instructor's suite made during the run. A target nobody
asked about is never sent to an LLM.

`submission.ai_graded_submitted_at` answers *is this submission
finished*. The nightly trigger filters on it, so a fully graded
submission is never rerun, and the AI phase re-checks it to catch reruns
the trigger did not cause.

## Ownership

| Student | Owns | Publishes | Consumes |
| --- | --- | --- | --- |
| A | AI grading component, LLM adapters, prompts, confidence | `ILLMProvider`, `FakeProvider` | `IGradingClient` |
| B | Web frontend and backend, DB schema, migrations, repository | `IGradingClient`, `IGradingRepository`, fakes for both | -- |
| C | Reporting library, AI phase entrypoint, packager, nightly trigger | `IReportingClient`, `ILearningPlatform`, fakes for both | `IGradingClient` |
| D | SRS, test suite, CI and cron workflows, documentation | CI workflows, fixtures | all of the above |

Two rules that follow:

1. **Student B is the only person who writes SQL.** A and C reach data
   through `IGradingClient` over HTTPS, because their code runs in
   Gradescope's container, not ours.
2. **`IGradingClient` and `IGradingRepository` stay separate
   interfaces.** Collapsing them means shipping database credentials
   inside the autograder zip. The container holds the instructor's LLM
   API key (AD-5) but never a database credential.

## Rendering locally

```bash
./docs/architecture/render.sh          # SVG + PNG into rendered/
./docs/architecture/render.sh --check  # syntax check only
```

Requires `java` and `graphviz`. The PlantUML jar downloads itself on
first run. You do not need any of this to contribute: edit a `.puml`
and push, and CI renders it.

## Editor support

- VS Code: `jebbs.plantuml`, preview with `Alt+D`. Set `plantuml.render`
  to `Local` and point `plantuml.jar` at `~/.plantuml/plantuml.jar` so
  your source is not sent to a public server.
- IntelliJ: the PlantUML Integration plugin.

## Conventions

- One diagram per file, prefixed with a two-digit order.
- Requirements a diagram implements are cited by ID in a comment at the
  top, so a reader can trace back to the SRS.
- Open decisions are yellow notes inside the diagram they affect, and
  tracked in `DECISIONS.md`. Resolving one means updating both.
- Colours are ownership, not layer. Keep them consistent.

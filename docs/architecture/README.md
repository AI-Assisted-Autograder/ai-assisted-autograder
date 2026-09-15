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
| `03-sequence-immediate.puml` | Sequence | Immediate mode: AI grading inline on submission |
| `04-sequence-nightly.puml` | Sequence | Nightly mode: tests on submission, AI grading on a per-submission rerun |
| `05-sequence-instructor.puml` | Sequence | Configuring an assignment and reviewing results |
| `06-grading-task-state.puml` | State machine | Which targets need AI feedback, who decides, and how retries work |
| `07-interfaces.puml` | Class | The exact signatures each student codes against |

Read 03 and 04 first. The two grading modes are the central design
decision (see `DECISIONS.md`, AD-1) and everything else follows from
them. After that, `02` and `07` are the two you will keep returning to,
because they constrain other people's code.

## The one thing to internalise

The AI grading component runs **inside the LMS autograder container in
both modes**. The only difference is when. The nightly job logs in and
clicks "Rerun Autograder"; it does not generate feedback.

Two fields do the cost control, and they answer different questions.

`grading_task.ai_needed` answers *does this target deserve feedback*.
The submission autograder writes it after functional testing, under
instructor control via `grading_target.ai_feedback_policy`. A target
whose tests passed is never sent to an LLM.

`submission.ai_graded_submitted_at` answers *is this submission
finished*. The nightly trigger filters on it, so a graded submission is
never rerun, and the autograder re-checks it on entry to catch reruns
the trigger did not cause.

## Ownership

| Student | Owns | Publishes | Consumes |
| --- | --- | --- | --- |
| A | AI grading component, LLM adapters, prompts, confidence | `ILLMProvider`, `FakeProvider` | `IGradingClient` |
| B | Web frontend and backend, DB schema, migrations, repository | `IGradingClient`, `IGradingRepository`, fakes for both | -- |
| C | Autograder script, test runners, nightly trigger, platform adapter | `ILearningPlatform`, `ITestRunner`, `FakePlatform` | `IGradingClient` |
| D | SRS, test suite, CI and cron workflows, documentation | CI workflows, fixtures | all of the above |

Two rules that follow:

1. **Student B is the only person who writes SQL.** A and C reach data
   through `IGradingClient` over HTTPS, because their code runs in
   Gradescope's container, not ours.
2. **`IGradingClient` and `IGradingRepository` stay separate
   interfaces.** Collapsing them means shipping database credentials
   inside the autograder zip. See OQ-11.

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

# Architecture

UML documentation for the Automated Code Feedback system. Sources are
PlantUML (`.puml`); rendered SVG and PNG are committed under
`rendered/` by the `Render UML` GitHub Actions workflow.

Edit the `.puml` sources, never the images. The workflow re-renders on
every push to `main` and fails a pull request whose committed images are
stale.

## Diagrams

| File | Type | What it answers |
| --- | --- | --- |
| `01-component.puml` | Component | What are the modules, who owns each one, and what are the interfaces between them |
| `02-data-model.puml` | ERD | The database schema, which is the contract every module shares |
| `03-sequence-nightly.puml` | Sequence | How one nightly grading run flows end to end |
| `04-sequence-instructor.puml` | Sequence | How an instructor configures an assignment and reviews results |
| `05-submission-state.puml` | State machine | When a submission counts as already graded, and how retries work |
| `06-interfaces.puml` | Class | The exact signatures each student codes against |

Read them in that order the first time. After that, `02` and `06` are
the two you will keep coming back to, because they are the ones that
constrain other people's code.

## Ownership

| Student | Owns | Publishes | Consumes |
| --- | --- | --- | --- |
| A | Feedback Engine, LLM provider adapters, prompts, confidence | `ILLMProvider`, `IFeedbackEngine`, `FakeProvider` | `IGradingRepository` |
| B | Web frontend and backend, DB schema, migrations, repository layer | `IGradingRepository`, `InMemoryRepository`, REST API | -- |
| C | Gradescope adapter, autograder client, `results.json` packaging | `ILearningPlatform`, `FakePlatform`, `FeedbackPackage` | Web API (not the DB directly) |
| D | SRS, test suite, CI and cron workflows, documentation | CI workflows, test fixtures | all of the above |

Two rules that follow from this table and are worth stating out loud:

1. **Student B is the only person who writes SQL or migrations.** A and
   C go through `IGradingRepository`. A schema change is then one pull
   request instead of four.
2. **The autograder client never holds database credentials.** It runs
   inside Gradescope's container, which is not our infrastructure. It
   talks to the Web API over the network. Student C and Student B need
   to agree the API surface early.

## Rendering locally

```bash
./docs/architecture/render.sh          # SVG + PNG into rendered/
./docs/architecture/render.sh --check  # syntax check only
```

Requires `java` and `graphviz`. The PlantUML jar downloads itself on
first run.

You do not need any of this to contribute. Editing a `.puml` and
pushing is enough; CI renders it. The local script exists for fast
iteration when you are moving boxes around.

## Editor support

- VS Code: `jebbs.plantuml` extension, preview with `Alt+D`. Set
  `plantuml.render` to `Local` and point `plantuml.jar` at
  `~/.plantuml/plantuml.jar` so your source is not sent to a public
  server.
- IntelliJ: the PlantUML Integration plugin.

## Conventions

- One diagram per file, filename prefixed with a two-digit order.
- Every requirement a diagram implements is cited by ID in a comment at
  the top of the file, so a reader can trace back to the SRS.
- Open decisions are recorded as yellow notes inside the diagram that
  they affect, and tracked in `DECISIONS.md`. When a decision is made,
  update both.
- Colours are ownership, not layer. Keep them consistent across
  diagrams.

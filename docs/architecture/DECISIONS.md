# Architecture decisions and open questions

Decisions that are settled, then questions that block implementation.
Each decision records why, because the reasoning is what a future
contributor needs when the constraint stops making obvious sense.

---

## Decisions

### AD-1. Grading mode is per assignment

`assignment.grading_mode` is `IMMEDIATE` or `NIGHTLY`. It decides *when*
AI grading happens, for the whole assignment, so a submission is wholly
one or the other.

**IMMEDIATE.** The instructor's autograder script invokes the AI phase
straight after her test suite exits. Functional tests, AI feedback,
`results.json`, done in one pass.

**NIGHTLY.** The submission run records which targets want feedback but
grades nothing. Each night a job runner logs in with browser automation
and clicks "Rerun Autograder" on every submission that still has work.
During that rerun the AI phase does the grading.

`results.json` writes follow from the mode: once for `IMMEDIATE`, twice
for `NIGHTLY` (test results on the submission run, feedback on the
rerun). In `NIGHTLY` mode the first write has no AI feedback at all,
which reads correctly to a student as "not graded yet".

For the instructor guide: `IMMEDIATE` puts LLM latency inside the
platform's autograder timeout, and submissions spike before a deadline.
Large courses should prefer `NIGHTLY`, and `IMMEDIATE` assignments are
worth capping to a single provider.

### AD-2. The instructor's test suite is the autograder

We ship a **library, not a runner**. Her own JUnit or pytest suite is the
autograder. It imports our reporting library and calls us; we never
invoke her tests.

This is an inversion of control relative to a framework, and it is the
right call for an OER. Adopting instructors already have test suites and
a way of structuring them. A system that insisted on driving them would
be a rewrite rather than an adoption.

Consequences:

- No `ITestRunner` interface. Running tests is not our job.
- The **AI phase** is a command her `run_autograder` invokes after the
  suite exits, not something called from inside her test code, so she
  never has to remember a teardown.
- Target identifiers are a contract expressed as strings across a
  process boundary. See AD-3.

### AD-3. Target identifiers are a checked contract

Identifiers are typed once into the web app and again into her test
suite, with no compiler between them. So `ai_not_needed` rejects a name
not registered on the assignment with a 4xx naming it and listing the
registered ones, and the library raises rather than swallowing it. Her
test run fails on the first bad call.

Since `ai_needed` defaults to TRUE (AD-7), there is no "registered but
never reported" case to detect — a target the suite never mentions keeps
its default and gets graded. That does leave one softer failure: a target
renamed in the web app but not in the suite will be graded with no test
outcome in the prompt, so the feedback is vague rather than absent, which
is harder to spot. That case is narrow, and mild: the target simply gets
graded, since prompts do not depend on matching anything in
`results.json`.

Recommended but not decided: the web app offers a copy-paste block of an
assignment's exact identifiers, so the second typing is a paste.

### AD-4. The autograder container holds the LLM API key

The AI phase calls the providers directly from inside the platform's
container, using the instructor's own key. Our service never proxies an
LLM call and never sees a provider key.

| Credential | Where it lives |
| --- | --- |
| LLM provider API key | The container's environment. The instructor's own key. |
| Database credential | Only our cloud host. Never in the container. |
| Per-assignment service token | The container, scoped to one assignment's rows. |

**Why this suits an OER.** Each adopting institution uses its own key and
pays its own LLM bill directly to the provider. Nobody has to run a
metered service, meter per-course usage, or handle another institution's
billing. Proxying through our service would have made us the billing
intermediary for every adopter, a far larger operational commitment than
a grading tool should carry.

**What it costs**, for the instructor guide rather than the code: the key
is visible to anyone with access to the assignment's autograder
configuration, which in practice includes TAs; rotation means touching
every assignment carrying it; and how the key reaches the container
depends on what secret facility the platform offers. If the only route is
the uploaded autograder zip, say so plainly, because a key in a zip is a
key in every backup of that zip. Confirm what Gradescope provides before
writing that section.

### AD-5. `results.json` is written before grading starts

The instructor's framework writes `results.json` with the functional test
results as Phase 1 ends. Only then does the AI phase run.

If Phase 2 dies outright, that file is what the student sees. Since test
outcomes are not stored anywhere else (AD-6), losing it to an LLM failure
would lose the test results entirely.

### AD-6. Functional test outcomes are not stored

Test results reach the student through `results.json`, written by her own
framework. Nothing about the test run reaches our database, and nothing
about it feeds a prompt either.

The prompt is built from the target's rubric, its feedback guidance, the
student's source code on the container filesystem, and any `test_context`
the instructor's suite chose to record (AD-10). `ai_needed` being true
already carries the only thing a raw outcome had to convey, namely that
this target wants feedback. The model locates the target in the source
and reasons from the rubric.

`results.json` is therefore read exactly once per run, at the end, to
splice the feedback into.

This removes the `test_outcome` table, the `submission_file` table,
`record_outcome` from the reporting library (now a one-method interface),
and any need to reconcile a database copy of the outcomes against the
file the student actually sees.

`ResultsPackager` therefore **splices rather than authors**: it reads the
file her framework wrote, appends the AI feedback, and writes the result.
That is what leaves an adopter's existing autograder setup intact.

**The model never touches `results.json`.** Its entire output is a string
and a number; parsing, inserting and writing are deterministic code. Do
not have the model emit JSON, however convenient the prompt looks: one
malformed or truncated response and the student's grade report is corrupt
or empty, and the failure lands silently on their screen rather than in a
log. Treat the model's text as untrusted input on the way in too, so
length-cap it and escape it when inserting, since it ends up in a file
the platform renders.

The cost is OQ-16.

### AD-7. `ai_needed` defaults ON; the suite opts targets out

`submission_target.ai_needed` is created **TRUE** for every registered target
when a submission first reports in. During functional testing the suite
calls `ai_not_needed(target)` for targets that need no commentary,
typically because their test passed. The AI phase grades whatever is
still true.

No policy enum, no resolution step. Earlier revisions had
`ai_feedback_policy`, then `feedback_mode`, with values like
`ON_FAILURE`, `NEVER`, `ALWAYS`, `AUTOGRADER`, plus a hook interface and
a resolve step to combine them. All of it collapsed into one boolean with
a default. `NEVER` was redundant because a target the instructor never
wants feedback on simply is not registered; the rest were redundant
because any rule she wants is one line of her own code at the opt-out
call.

**The default is fail-open on cost.** An instructor who opts nothing out
pays for feedback on every target of every submission. That is the
opposite risk profile from requiring an explicit request: nothing goes
silently unexplained, but a forgotten opt-out is expensive. Put it in the
instructor guide prominently.

### AD-8. IMMEDIATE never defers to the nightly run

Failure is handled identically in both grading modes. The AI phase
retries inside its run, with backoff on transient errors and no retry on
deterministic ones. If it still fails, it appends a note to
`results.json` saying feedback could not be generated and sets
`completed_at` with `last_error`. The student keeps their test results
and gets an honest explanation.

Nothing is carried to another night. There is no `attempt_count` and no
cross-night retry, in either mode. `IMMEDIATE` and `NIGHTLY` differ only
in *when* the AI phase runs, never in what it does when generation
fails.

**Two things follow, and both are load-bearing.**

`completed_at` is set on the failure path too. Leaving it NULL would mean
the nightly trigger picked the row up again every night forever, since
its query is exactly `ai_needed AND completed_at IS NULL`.

The trigger query also filters on `grading_mode = 'NIGHTLY'`. A failed
`IMMEDIATE` row already has `completed_at` set so it would be excluded
anyway, but a container killed before the AI phase ran would not be, and
sweeping that up would contradict the mode the instructor chose.

**What does still get retried** is work that was never attempted: the
trigger job failed, our service was down, Gradescope was. Those rows
still have `completed_at` NULL and the next night picks them up. "The
job did not run" self-heals; "grading ran and failed" does not. That is
the line, and it falls out of the schema rather than needing a rule.

**What this gives up**, honestly: a transient failure during a nightly
run is now permanent for that submission. The reason that is acceptable
is in the failure taxonomy below. The rate-limit spike that makes
`IMMEDIATE` risky is a deadline-hour phenomenon, and `NIGHTLY` grades at
3am against a serialised trigger, so the main driver of transient failure
is largely absent there.

### AD-9. The token guard

`submission.ai_graded_submitted_at` records the submission time that AI
grading last completed for. The AI phase checks it before doing any work.

A rerun re-executes the container against the same submitted files, so
`submitted_at` does not change on rerun, which makes it a reliable marker
of exactly which submission was graded. A new attempt gets a new
`platform_submission_id` and its own row, so it is graded normally; the
guard suppresses only re-grading of an already-graded attempt.

It matters because reruns happen for reasons other than our trigger: an
instructor debugging the autograder, a TA re-running a flaky test. Set it
only when every `ai_needed` row either has `completed_at` set or is out
of attempts, so one broken target cannot hold a submission in the nightly
queue forever.
Once set it is never cleared.

### AD-10. Optional test context feeds the prompt

The reporting library has a second optional call:

```
add_test_context(target, note)
```

During functional testing the instructor's suite can record any
diagnostic it already computed: the failing input, expected versus
actual, which branch was taken. It is stored in `test_context`, keyed to
the same `submission_target` row as `ai_needed`, and rendered into the prompt
by the AI phase.

This is how the prompt gets test detail without anyone parsing
`results.json`. She names the target explicitly, so the identifier check
from AD-3 covers this call exactly as it covers `ai_not_needed`, and
there is no linkage to infer.

It also gives better material than raw output would. She knows which
assertion matters and can say so in one line; we would be guessing from a
stack trace.

A child table rather than a column, because a target usually has several
tests and more than one may have something worth saying. Rows are
rendered into the prompt in `recorded_at` order.

There is no `test_name` field. Nothing would join on it or validate it,
and she can name the test inside the note if it helps the model. Keeping
it would also have invited the assumption that it must match a
`results.json` entry, which is the kind of implied contract this design
has been removing.

**Treat it as untrusted.** Assertion messages routinely embed the
student's own output, sometimes their source. A student can therefore
influence what lands in this text, which makes it a prompt injection
surface: content that reads as an instruction rather than as data. The
instructor writes the *call*; she does not control the whole *string*,
and those are different things.

Mitigations, none expensive: delimit it in the prompt and label it as
test output, keep the rubric authoritative over anything it says, cap the
length per row and per target, and do not let it take over the role of
the guidance. The length cap is also a cost control, since this text is
billed as input tokens on every provider call for that target.
### AD-11. The container asks the service what to do

The instructor's autograder script is one script, so it invokes the AI
phase in every run, including the submission run of a `NIGHTLY`
assignment. The AI phase therefore has to behave differently in
situations it cannot distinguish locally.

It does not try. It asks:

```
GET /submissions/{platform_submission_id}/action
  -> GRADE | DEFER | REEMIT
```

| Situation | Action |
| --- | --- |
| `IMMEDIATE` | `GRADE` |
| `NIGHTLY`, `ai_authorized_at` NULL | `DEFER` |
| `NIGHTLY`, `ai_authorized_at` set | `GRADE` |
| Already graded (AD-9 guard) | `REEMIT` |

`DEFER` splices a section into `results.json` saying feedback will
arrive by the next morning, and spends nothing.

**Why the container cannot work this out itself.** In `NIGHTLY` mode the
same script runs twice and the database looks identical both times: rows
with `ai_needed` set and `completed_at` NULL. The trigger cannot tell the
container either, because all the trigger does is click a button in a
web UI. So the nightly job sets `submission.ai_authorized_at` **before**
clicking rerun, and the service derives the action from the mode, that
flag and the guard together.

Order matters. Set the flag after the click and the container can start,
ask, and get `DEFER` again.

**Why server-side.** The AI phase ships inside the instructor's
autograder zip, which is the hardest artifact in the system to update and
which every adopting institution has its own copy of. Policy held there
would need a coordinated re-upload to change. Policy held in the service
changes once.

Alternatives rejected: counting autograder runs, which breaks the moment
an instructor reruns to debug something; anything time-based, which
introduces a clock dependence for no benefit.

**Failure mode to report.** If the trigger sets the flag but the rerun
never happens, tomorrow's trigger simply tries again, so that case is
harmless. If the trigger fails to set the flag at all, nothing ever
grades and it looks exactly like "no feedback needed" — the same class of
silent failure as a missed click, and it belongs in the same
`trigger_run` reporting.


---

## Why AI generation fails

Worth writing down because it determines the retry policy (OQ-5) and
because it bears on the choice of grading mode.

**Transient, worth a retry:**

1. **Rate limiting.** Every submission for an assignment uses one
   instructor API key, and students submit in the last hour before the
   deadline. A burst of concurrent containers against a single key makes
   429 the expected case, not an edge case.
2. **Provider 5xx.**
3. **Quota or billing exhausted** mid-term.

**Not worth retrying:**

4. **Context overflow** on an enormous submission.
5. **Model refuses**, or returns output the parser cannot read.
6. **Autograder timeout** kills the container, in which case nothing runs
   and AD-5's early write is what survives.

Two consequences.

**Classify before retrying.** Retries happen inside a single AI phase run
(AD-8), so this is about whether to spend a few seconds of backoff, not
about scheduling. A deterministic failure returns the same result every
attempt, so it should not be retried at all.

**Failures 1 and 6 are correlated with the deadline**, so the failure
rate is not uniform — it spikes exactly when the most students are
affected. That is the strongest argument for preferring `NIGHTLY` in a
large course, since `NIGHTLY` grades at 3am against a serialised trigger
and misses the spike entirely. It is also an argument for serialising
rather than fanning out the provider calls within a run.

---

## Open questions

| ID | Question | Blocks | Owner |
| --- | --- | --- | --- |
| OQ-16 | Test outcomes are unstored. Does the dashboard need them (FR-8/9/10), or is feedback-only enough? | B's dashboard, possibly one table | Instructor + B |
| OQ-1 | What exactly is a "deduction" (FR-10)? | B's dashboard query | Instructor |
| OQ-2 | How is confidence defined and computed (FR-28)? | A's engine, B's UI | A |
| OQ-4 | Are all submission attempts graded, or only the latest? | cost | Instructor |
| OQ-5 | In-run retry count and backoff schedule | A's provider adapters | A |
| OQ-6 | Exact Gradescope `results.json` schema | C's packager, D's tests | C |
| OQ-7 | Gradescope authentication for the trigger, and where that credential lives | C's trigger | C |
| OQ-8 | Web framework, database, cloud host | B cannot start | B |
| OQ-9 | Instructor authentication and authorization | B's backend | B |
| OQ-10 | Student data retention and FERPA posture | deployment | Instructor |
| ~~OQ-17~~ | ~~Linking results.json entries to grading targets~~ | **DISSOLVED**: prompts do not use test outcomes, so there is nothing to link | -- |

Resolved: OQ-3 (no post-hoc feedback requests), OQ-11 (AD-4),
OQ-12 (AD-3), OQ-13 (AD-2), OQ-14 and OQ-15 (dissolved by AD-7 and AD-8).

### OQ-16: the dashboard gap

Outcomes now live only in `results.json`. Gradescope shows them to the
student and the AI phase parses them for prompts, but nothing persists
them.

FR-8, FR-9 and FR-10 want a dashboard showing how students are doing and
filtering to submissions with deductions. Unstored outcomes mean that
dashboard can show AI feedback and suggested scores but not raw
functional pass/fail, and "has a deduction" can only be derived from AI
scores.

**(a) Accept it.** The dashboard's job is feedback quality (FR-11), and
Gradescope already shows the instructor her own test results.

**(b) Have the AI phase POST the outcomes it parsed**, stored alongside
the feedback as context. Costs one table and one request, and keeps the
dashboard self-contained without reintroducing Phase 1 database writes.

Needs deciding before Student B builds the dashboard, since it determines
whether there is a table to query.

### OQ-9 and OQ-10 deserve a flag

The SRS specifies no instructor authentication at all, which is fine for
a prototype on synthetic data. And in `IMMEDIATE` mode student code
reaches a third-party LLM within seconds of submission — it is no longer
retained by us (AD-6), but it does leave the institution. Retention,
access and consent need answers before a live section. Pitt has people
whose job this is.

---

## Changes to make in the SRS

Student D should revise section 3.4, which predates these decisions:

- FR-31 ("retrieve new submissions through the GradeScope integration")
  no longer applies. Nothing pulls submissions; the autograder already
  has the student's files.
- FR-34 to FR-36 collapse. There is no separate step that fetches
  feedback and routes it back, because the run that generates feedback
  splices it into `results.json`.
- Add requirements for `grading_mode`, for `ai_needed` defaulting on with
  a suite-driven opt-out, and for the token guard. All three are
  load-bearing and currently undocumented.
- Remove any requirement implying functional outcomes are stored.

---

## Naming

`submission_target` is the join row between a submission and a
registered grading target: one per `(submission_id,
grading_target_id)` pair, carrying `ai_needed` and `completed_at`. Child
tables (`test_context`, `feedback`, `feedback_comparison`) hang off it,
because everything they record is per-pair rather than per-submission or
per-target.

It was called `grading_task` for several revisions, from back when this
was a job queue with `PENDING`, `IN_PROGRESS`, atomic claiming and
competing workers. All of that was deleted, and the name outlived it. A
"task" implies a queue and a worker that no longer exist;
`submission_target` says what the row actually is.

State is derived rather than stored. `completed_at` and `last_error`
already determine whether a row is waiting, done or failed, so an
`ai_status` enum could only agree with them or contradict them. The
derived states are drawn in `06-submission-target-state.puml`.

---

## Corrections to earlier revisions

Recorded because the reasoning is in these files' git history and would
otherwise mislead.

**Rerun is per submission.** An early revision assumed "Rerun Autograder"
was a bulk action over a whole assignment and built a load-bearing skip
branch around the idea that every nightly trigger re-executed every
submission ever made. It is per submission; there is no quadratic cost
problem.

**There was never a separate delivery step.** An early revision had the
nightly job generate feedback on our infrastructure and push it back in a
second pass. The AI phase runs in the container, and the run that
generates feedback writes it.

**The task lifecycle was over-engineered.** An early revision modelled
`submission_target` as a job queue with an atomic claim step, to guard against
concurrent workers. There are no concurrent workers: rerun is per
submission, so one container works on a submission at a time.

**Grading mode was briefly per target**, which allowed mixed submissions
and created a real problem: the first `results.json` carried feedback for
some targets and silence for others, which a student reads as "those were
fine". The complication was an artifact of the misplacement.

**The policy enum was removed entirely.** See AD-7. The lesson is that a
configuration surface for something the instructor already expresses in
code is duplication, not flexibility.

**Phase 1 database writes were removed.** See AD-6. Since outcomes must
reach `results.json` regardless, a database copy was a second source of
truth for the same facts.

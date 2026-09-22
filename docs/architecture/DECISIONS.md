# Architecture decisions and open questions

Decisions that are settled, then questions that block implementation.
Each decision records why, because the reasoning is what a future
contributor needs when the constraint stops making obvious sense.

---

## Contents

- [Decisions](#decisions)
  - [AD-1. Grading mode is per assignment](#ad-1-grading-mode-is-per-assignment)
  - [AD-2. The instructor's test suite is the autograder](#ad-2-the-instructors-test-suite-is-the-autograder)
  - [AD-3. Target identifiers are a checked contract](#ad-3-target-identifiers-are-a-checked-contract)
  - [AD-4. The autograder container holds the LLM API key](#ad-4-the-autograder-container-holds-the-llm-api-key)
  - [AD-5. `results.json` is written before grading starts](#ad-5-resultsjson-is-written-before-grading-starts)
  - [AD-6. results.json is never parsed](#ad-6-resultsjson-is-never-parsed)
  - [AD-7. `ai_needed` defaults ON; the suite opts targets out](#ad-7-ai_needed-defaults-on-the-suite-opts-targets-out)
  - [AD-8. Failure is terminal, identically in both modes](#ad-8-failure-is-terminal-identically-in-both-modes)
  - [AD-9. A graded submission is never graded again](#ad-9-a-graded-submission-is-never-graded-again)
  - [AD-10. The suite reports outcomes and test feedback](#ad-10-the-suite-reports-outcomes-and-test-feedback)
  - [AD-11. The container asks the service what to do](#ad-11-the-container-asks-the-service-what-to-do)
  - [AD-12. Source files are derived from the identifier, not configured](#ad-12-source-files-are-derived-from-the-identifier-not-configured)
  - [AD-13. Platform: FastAPI, PostgreSQL, Oracle Cloud](#ad-13-platform-fastapi-postgresql-oracle-cloud)
- [Why AI generation fails](#why-ai-generation-fails)
- [Open questions](#open-questions)
  - [OQ-9 and OQ-10 deserve a flag](#oq-9-and-oq-10-deserve-a-flag)
- [Changes to make in the SRS](#changes-to-make-in-the-srs)
- [Naming](#naming)

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

`results.json` is written **twice per autograder run**: once by her
framework with the test results (AD-5), once by the AI phase after it
splices in whatever AD-11 told it to add. So `IMMEDIATE` writes it twice
in total and `NIGHTLY` four times, twice on the submission run and twice
on the rerun.

In `NIGHTLY` mode the submission run's second write carries a "feedback
deferred" section rather than feedback, so the student is told what is
coming rather than left with an unexplained gap.

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

- Running tests is not our job, so we ship no test-runner interface.
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
never reported" case to detect. A target the suite never mentions keeps
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

If Phase 2 dies outright, that file is what the student sees. Writing it
first means an LLM failure costs them their feedback but not their test
results, which had already succeeded.

Note this is about the student's view, not about durability. Outcomes her
suite reported are in `test_result` (AD-10), so the data survives either
way; what the early write protects is what appears on the student's
screen.

### AD-6. results.json is never parsed

Test results reach the student through `results.json`, written by her own
framework. We never parse that file to learn what happened.

Outcomes *are* stored, as `test_result` rows, but only because her suite
reports them (AD-10). The direction is what matters: she tells us, we do
not derive it. `results.json` remains the student-facing record and is
read only to splice feedback into.

Nothing parsed out of that file feeds a prompt. `ai_needed` being true
already carries the only thing a raw outcome had to convey, namely that
this target wants feedback; the rest of the prompt comes from the rubric,
the source file (AD-12) and `functional_test_feedback` (AD-10).

So `results.json` is read exactly once per run, at the end, to splice
feedback into. That removes the `submission_file` table, `record_outcome`
from the reporting library, and any need to reconcile a parsed copy of
the outcomes against the file the student actually sees.

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

### AD-7. `ai_needed` defaults ON; the suite opts targets out

`submission_target.ai_needed` is created **TRUE** for every registered target
when a submission first reports in. During functional testing the suite
calls `ai_not_needed(target)` for targets that need no commentary,
typically because their test passed. The AI phase grades whatever is
still true.

One boolean, no policy enum and no resolution step. A configured policy
would be redundant twice over: a target the instructor never wants
feedback on simply is not registered, and any other rule she wants is one
line of her own code at the opt-out call.

**The default is fail-open on cost.** An instructor who opts nothing out
pays for feedback on every target of every submission. That is the
opposite risk profile from requiring an explicit request: nothing goes
silently unexplained, but a forgotten opt-out is expensive. Put it in the
instructor guide prominently.

### AD-8. Failure is terminal, identically in both modes

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
3am against a serialized trigger, so the main driver of transient failure
is largely absent there.

### AD-9. A graded submission is never graded again

`submission.ai_graded_submitted_at` records the submission time that AI
grading last completed for. The AI phase checks it before doing any work.

A rerun re-executes the container against the same submitted files, so
`submitted_at` does not change on rerun, which makes it a reliable marker
of exactly which submission was graded. A new attempt gets a new
`platform_submission_id` and its own row, so it is graded normally; the
guard suppresses only re-grading of an already-graded attempt.

It matters because reruns happen for reasons other than our trigger: an
instructor debugging the autograder, a TA re-running a flaky test.

Set it once every `ai_needed` row has `completed_at` set, which includes
rows that failed. Once set it is never cleared. Because failure is
terminal (AD-8), a submission whose targets all finished, successfully or
not, is finished for good and leaves the nightly queue.

### AD-10. The suite reports outcomes and test feedback

The reporting library has a second optional call:

```
add_test_result(target, status, functional_test_feedback)
```

During functional testing her suite reports the outcome and, optionally,
any diagnostic it already computed: the failing input, expected versus
actual, which branch was taken. Stored in `test_result`, keyed to the
same `submission_target` row as `ai_needed`.

The two fields serve different consumers, which is why one call carries
both. `status` is stored for the instructor dashboard and is the only
functional pass/fail record the system holds.
`functional_test_feedback` is rendered into the prompt by the AI phase.

The name is deliberately explicit even though it stutters against the
table name. The `ai_feedback` table is named for the
same reason: two unqualified senses of "feedback" in one schema is worse
than two longer names.

Neither is inferred from `ai_not_needed`. A passing target she still
wants commented on, and a failing target she opts out of, are both
legitimate, so the outcome and the decision stay independent.

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
grades and it looks exactly like "no feedback needed", the same class of
silent failure as a missed click, and it belongs in the same
`trigger_run` reporting.

### AD-12. Source files are derived from the identifier, not configured

There is no `target_file_pattern` table. The AI phase works out which
source file to read from the target's `identifier`, its `kind`, and the
assignment's `language`:

| `kind` | identifier | derived glob |
| --- | --- | --- |
| `METHOD` | `edu.pitt.cs.Statistics#median` | `**/Statistics.java` |
| `CLASS` | `edu.pitt.cs.Statistics` | `**/Statistics.java` |
| `FILE` | `src/**/*.java` | itself, used as a glob |

A configured pattern would restate what the instructor already entered,
and the two could then disagree with her having to maintain both. This is
the same shape as the policy enum that AD-7 removed.

Letting a `FILE` identifier *be* a glob also covers the multi-file case,
which was the only argument for making the patterns a one-to-many table.

**`assignment.language` exists because of this.** The file extension and
the package-to-path convention are not inferable from the identifier
alone: `foo.bar` is ambiguous between a Python module path and a Java
package. Keep the derivation in one small per-language mapping rather
than scattered through the prompt builder, so adding a language is one
table entry rather than an archaeology exercise.

**Resolution must yield exactly one file.** Zero matches and more than
one match are both failures, taken down the AD-8 failure path: the AI
phase records `last_error`, sets `completed_at`, and appends a message to
`results.json` naming the target and the problem. The student sees it.

Deliberately strict, because both alternatives are worse. Grading with no
source file produces feedback invented from the rubric alone. Picking one
of several candidates means silently grading a file that may not be the
one the tests exercised, and the feedback then confidently describes the
wrong code. "Could not resolve a source file for `Statistics#median`" is
a configuration problem someone can fix; a plausible paragraph about the
wrong file is not.

The cause is usually the instructor's identifier or the assignment's file
layout rather than anything the student did, so the message should say so
plainly, as the unregistered-target error does (AD-3).

**Inner and nested classes.** `Outer$Inner` and `Outer.Inner` live in
`Outer.java`. Derivation stops at the outermost class rather than taking
the last segment. Getting this wrong turns every nested-class target into
a zero-match failure.

**Cross-file causes.** A `METHOD` target can fail because of a helper in
another file. Only the resolved file's contents go into the prompt, so
the prompt also carries a **listing of the other files in the
submission**, names only. That costs few tokens and lets the model say
the cause may lie outside what it was shown instead of confidently
misdiagnosing the method it can see.

**A practical risk to watch.** A student who leaves a copy of a file
somewhere in their submission turns a multiple-match into a feedback
failure for that target. If that proves common, the fix is to narrow what
counts as a candidate, for instance excluding test roots or directories
the assignment does not expect, rather than to start guessing between
candidates.

### AD-13. Platform: FastAPI, PostgreSQL, Oracle Cloud

| Choice | Notes |
| --- | --- |
| Web framework | FastAPI |
| Database | PostgreSQL, which ratifies the ERD's `timestamptz`, `uuid` and `numeric` types |
| Cloud host | Oracle Cloud Infrastructure |

The host carries only the web app and the database. The AI phase runs in
Gradescope's container and the nightly trigger runs in GitHub Actions, so
nothing here is on the critical path for local development.

**FastAPI implications.** It brings no ORM, admin, auth or templating, so
the instructor dashboard is hand-built and Student B needs SQLAlchemy
plus Alembic for migrations, and an auth library for OQ-9. That is a real
cost compared with a batteries-included framework, and it buys a good fit
for the larger consumer: the autograder container talks JSON, and the
Pydantic models map directly onto the DTOs in `07-interfaces.puml`.

**OCI implications, all verifiable before anyone commits time.**

*PostgreSQL is self-managed.* OCI's always-free database offering is
Autonomous Database, which is Oracle's own engine, not Postgres. Postgres
means running it on the compute instance. Student D inherits backups,
upgrades and process supervision, and OQ-10 inherits the question of
where student-data backups live.

*The instances are ARM.* Ampere A1 is aarch64, so container images must
be arm64. This is a per-job label in the workflow, not a repository
setting: `runs-on: ubuntu-24.04-arm`. Free and unlimited on public
repositories, and available in private ones against the plan's minute
allocation. Build jobs that produce deployment images should use it;
jobs that only run tests need not, though matching production
architecture is the safer default.

*The free allowance shrank.* Oracle reduced Always Free Ampere A1 from
4 OCPU / 24 GB to 2 OCPU / 12 GB during 2026, and instances above the new
limit were subject to termination. Still ample for a web app and a small
Postgres, but re-check the current terms rather than trusting a guide
written earlier, and note that A1 capacity is genuinely hard to obtain in
some popular regions.

*Region choice is constrained by OQ-10*, not by preference. If Pitt
requires student data to stay in a particular jurisdiction, that decides
the region before performance does.

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
rate is not uniform. It spikes exactly when the most students are
affected. That is the strongest argument for preferring `NIGHTLY` in a
large course, since `NIGHTLY` grades at 3am against a serialized trigger
and misses the spike entirely. It is also an argument for serializing
rather than fanning out the provider calls within a run.

---

## Open questions

Ordered by what they block. IDs are stable, so they are not sequential.

| ID | Question | Blocks | Owner |
| --- | --- | --- | --- |
| OQ-6 | Exact Gradescope `results.json` schema | C's packager, D's tests | C |
| OQ-7 | Gradescope authentication for the trigger, and where that credential lives | C's trigger | C |
| OQ-3 | Can the instructor edit or approve generated feedback before the student sees it? | state machine, B's dashboard, whether IMMEDIATE is offered at all | Instructor |
| OQ-4 | Are all submission attempts graded, or only the latest? | cost | Instructor |
| OQ-1 | What exactly is a "deduction" (FR-10)? | B's dashboard query | Instructor |
| OQ-2 | How is confidence defined and computed (FR-28)? | A's engine, B's UI | A |
| OQ-5 | In-run retry count and backoff schedule | A's provider adapters | A |
| OQ-9 | Instructor authentication and authorization | B's backend | B |
| OQ-10 | Student data retention and FERPA posture | deployment | Instructor |

### OQ-9 and OQ-10 deserve a flag

The SRS specifies no instructor authentication at all, which is fine for
a prototype on synthetic data. And in `IMMEDIATE` mode student code
reaches a third-party LLM within seconds of submission. We do not retain
it (AD-6), but it does leave the institution. Retention,
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
registered grading target: one row per `(submission_id,
grading_target_id)` pair, carrying `ai_needed` and `completed_at`. The
child tables `test_result` and `ai_feedback` hang off it, because
everything they record is per-pair rather than per-submission or
per-target. The FR-27 cross-provider comparison lives in columns on this
row rather than its own table, since one grading attempt per row means
exactly one comparison.

`ai_feedback` and `ai_feedback_review` carry the `ai_` prefix to contrast
with `test_result.functional_test_feedback`. Both are feedback; only one
is generated by a model. The child was renamed too rather than left as
`feedback_review`, because a child whose name does not match its parent
invites the question of which table it belongs to.

Inside `ai_feedback` the text column is `body`, not `feedback_text`,
which would stutter against the table name.

State is derived rather than stored. `completed_at` and `last_error`
already determine whether a row is waiting, done or failed, so an
`ai_status` enum could only agree with them or contradict them. The
derived states are drawn in `06-submission-target-state.puml`.

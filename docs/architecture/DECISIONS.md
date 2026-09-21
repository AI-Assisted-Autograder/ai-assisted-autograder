# Architecture decisions and open questions

---

## AD-1. How functional test outcomes and AI feedback move through the LMS

**Status: DECIDED** by the instructor, 2026. Supersedes the earlier
two-pass proposal, and supersedes SRS section 3.4, which was ambiguous
on this point and should be rewritten to match.

The instructor chooses a **grading mode per assignment** (`IMMEDIATE`
or `NIGHTLY`), and separately a **feedback mode per target** (`NEVER`,
`ALWAYS`, `AUTOGRADER`). The two answer different questions: grading
mode says *when*, feedback mode says *whether*. See AD-4.

**IMMEDIATE.** The AI phase grades the marked targets during the
submission run, invoked by the instructor's autograder script after her
test suite exits. Functional tests, AI feedback, `results.json`, done in
one pass.

**NIGHTLY.** The submission run records outcomes and marks the work but
grades nothing. Every night a job runner uses browser automation to log
in and click "Rerun Autograder" on each submission that still has marked
work. During that rerun the AI phase grades the marked targets.

### What this means structurally

The AI grading component lives **inside the autograder container in
both modes**. Only the timing differs. Three consequences that shaped
the diagrams:

1. **The nightly job runner is thin.** It logs in, finds nightly-mode
   assignments with pending work, and clicks a button. It does not call
   an LLM and does not write feedback. Anything more belongs in the
   autograder script.

2. **There is no separate delivery step.** Feedback is written into
   `results.json` in the same run that generates it, so the `delivery`
   table and the delivery states from the earlier revision are gone.

3. **The marks are per-target, so the unit with a lifecycle is the
   submission-and-target pair.** That is the `grading_task` entity. The
   trigger job queries it to decide which submissions to rerun, and the
   rerun grades only the marked targets.

### AD-2. The instructor's test suite is the autograder

**Status: DECIDED** by the instructor.

We ship a **library, not a runner**. The instructor's own JUnit or
pytest suite is the autograder. It imports our reporting library and
calls us:

```
record_outcome(target, test_name, status, output)
request_ai_feedback(target)
```

She knows the target identifiers because she registered them in the web
app. We never enumerate targets and invoke tests ourselves.

This is an inversion of control relative to a framework, and it is the
right call for an OER: adopting instructors already have test suites and
a way of structuring them, and a system that insists on driving them
would be a rewrite rather than an adoption.

Consequences:

- There is no `ITestRunner` interface. Running tests is not our job.
- The **AI phase** is a command her `run_autograder` invokes after the
  suite exits. It resolves the marks, grades what the mode and guard
  warrant, and writes `results.json`. Not called from inside her test
  code, so she never has to remember a teardown.
- Target identifiers are a **contract expressed as strings** across a
  process boundary. See OQ-12; this is the sharpest new edge.

### The AI-needed marker, and instructor control over it

`grading_task.ai_needed` is the per-submission, per-target marker,
resolved by the AI phase. It is the main token saving in the
system: a target nobody asked feedback for is never sent to an LLM.

`grading_target.feedback_mode`, set per target in the web app:

| `feedback_mode` | Behaviour |
| --- | --- |
| `AUTOGRADER` | The instructor's suite decides per submission, by calling `request_ai_feedback`. The default. |
| `ALWAYS` | Mark every submission regardless of outcome. For style or design feedback no test can detect. |
| `NEVER` | Never mark. For targets graded purely by tests. |

**There is deliberately no `ON_FAILURE` value.** Marking on failure is
one line in the instructor's suite, and any variant (a subset of
failures, a superset, a partial-credit threshold, a cross-target
condition) is equally expressible there. A policy value would only
duplicate what her code already states, and would then need a way to say
"ignore the dropdown".

`ai_needed_reason` records which rule decided (`AUTOGRADER_REQUEST`,
`POLICY_ALWAYS`, `POLICY_NEVER`, `NOT_REQUESTED`). Store it.

### The token-waste guard

Rerun is **per submission**, not a bulk action over the assignment, so
the nightly trigger clicks rerun only on submissions that still have
PENDING grading tasks. It never touches an already-graded one.

Reruns still happen for reasons outside our control, though: an
instructor debugging the autograder, a TA re-running a flaky test. None
of those should re-bill the LLM. So the autograder script carries a
guard, checked before it queries tasks and before any LLM call:

```
submission.ai_graded_submitted_at == submission.submitted_at
  -> skip AI grading, re-emit the stored feedback
```

A rerun re-executes the container against the same submitted files, so
`submitted_at` does not change on rerun. That makes it a reliable
marker of exactly which submission was graded. A new student attempt
gets a new `platform_submission_id` and its own row, so it is graded
normally; the guard suppresses only re-grading of an already-graded
attempt.

`ai_graded_submitted_at` is set **only when no task is still
`NEEDED`**, counting `GAVE_UP` as settled so one permanently broken
target cannot hold a submission in the nightly queue forever. On
partial failure it stays NULL, so the next nightly trigger picks the
submission up and retries only the tasks still marked `NEEDED`. Once
set it is never cleared.

Two fields, two questions. `ai_needed` answers *does this target
deserve feedback*, and the instructor controls it. `ai_graded_submitted_at`
answers *is this submission finished*.

### Trade-offs to communicate in the instructor guide

Immediate mode gives fast feedback but puts LLM latency inside the LMS
autograder timeout, and submissions spike before a deadline. Multiple
providers times multiple targets may not fit. Consider capping immediate
mode to one provider.

Nightly mode absorbs the spike, makes multi-provider comparison cheap,
and leaves a window for instructor review before release. Students wait
until morning and see two separate result postings for one submission.

---

## Open questions

Each is out of scope or unspecified in the SRS. Fine for a requirements
document, not survivable for four people implementing in parallel.

| ID | Question | Blocks | Owner |
| --- | --- | --- | --- |
| OQ-15 | Does the nightly trigger consider IMMEDIATE assignments, so a failed immediate grading self-heals? | C's trigger query, B's endpoint | C |
| ~~OQ-11~~ | ~~Where LLM keys live~~ | **DECIDED** (AD-5): the autograder container holds the instructor's own LLM API key. No DB credential ever. | -- |
| ~~OQ-12~~ | ~~Typo'd target identifiers~~ | **DECIDED**: the API rejects unregistered names and the library raises, failing the test run | -- |
| ~~OQ-13~~ | ~~Who invokes the AI phase~~ | **DECIDED**: the instructor's `run_autograder` invokes it as a command after her suite exits | -- |
| ~~OQ-14~~ | ~~Registered target with no outcome~~ | **DECIDED**: an error. The AI phase exits non-zero and the autograder run fails | -- |
| OQ-1 | What exactly is a "deduction" (FR-10)? | B's dashboard query | Instructor |
| OQ-2 | How is confidence defined and computed (FR-28)? | A's engine, B's UI | A |
| OQ-3 | Can the instructor edit feedback before release? Now mode-dependent. | state machine, B's dashboard | Instructor |
| OQ-4 | Are all submission attempts graded, or only the latest? | cost, C's task creation | Instructor + A |
| OQ-5 | Retry policy: attempt cap, and the transient/deterministic split (taxonomy now in AD-6) | A and C will otherwise write two | D |
| OQ-6 | Exact Gradescope autograder JSON schema | C's packager, D's tests | C |
| OQ-7 | Gradescope authentication, and where that credential lives | C's trigger job | C |
| OQ-8 | Web framework, database, cloud host | B cannot start | B |
| OQ-9 | Instructor authentication and authorization | B's backend | B |
| OQ-10 | Student code retention, privacy, FERPA | deployment | Instructor |

### Two that deserve a flag

**OQ-9.** The SRS specifies no instructor authentication at all. Fine
for a prototype on synthetic data.

**OQ-10.** In immediate mode, student source code is sent to a
third-party LLM within seconds of submission. Retention, access and
consent all need answers before this touches a live section. Pitt has
people whose job this is.

---

## Changes to make in the SRS

Student D should revise section 3.4, which predates AD-1 and describes
a workflow that pulls submissions out of Gradescope and tests them
elsewhere. Specifically:

- FR-31 ("retrieve new submissions through the GradeScope integration")
  no longer applies. Nothing pulls submissions; the autograder script
  already has the student's files.
- FR-34 to FR-36 collapse. There is no separate step that fetches
  feedback and routes it back, because the run that generates feedback
  writes it.
- Add requirements for the mode selection itself, the per-target marks,
  and the skip-if-complete rule, which are currently undocumented and
  are load-bearing.

---

## Corrections to earlier revisions of these diagrams

Recorded because the reasoning appears in git history and would
otherwise mislead.

**Rerun is per submission.** An earlier revision assumed Gradescope's
"Rerun Autograder" was a bulk action over the whole assignment, and
built a load-bearing skip branch around the idea that every nightly
trigger re-executed every submission ever made. That was wrong. The
button is per submission, the nightly trigger iterates only submissions
with pending work, and there is no quadratic cost problem. The guard
described above is still wanted, but as protection against incidental
reruns rather than against the trigger job itself.

**There was never a separate delivery step.** An earlier revision had
the nightly job generate feedback on our infrastructure and then push it
back through a second pass. AD-1 replaced that: the AI component runs in
the container, and the run that generates feedback writes it.

**The task lifecycle was over-engineered.** An earlier revision modelled
`grading_task` as a job queue with `PENDING`, `IN_PROGRESS`, `COMPLETE`,
`FAILED` and `ABANDONED`, plus an atomic claim step to guard against
concurrent workers. There are no concurrent workers: rerun is per
submission, so one container works on a submission at a time. The claim
step and `IN_PROGRESS` are gone. Retry now falls out of the design, in
that a failed task simply stays `NEEDED` and the next trigger picks it
up.

**There is no post-hoc feedback request.** An earlier revision allowed
the instructor to request AI grading on an already-graded submission,
which required clearing `ai_graded_submitted_at` in the same
transaction. That path does not exist. Instructor control is exercised
at marking time, through `feedback_mode` and their own autograder
logic.

### AD-5. The autograder container holds the LLM API key

**Status: DECIDED** by the instructor.

The AI phase calls the LLM providers directly from inside the platform's
autograder container, using the instructor's own API key. Our service
never proxies an LLM call and never sees a provider key.

The asymmetry is deliberate and worth stating in one place:

| Credential | Where it lives |
| --- | --- |
| LLM provider API key | The autograder container's environment. The instructor's own key. |
| Database credential | Only on our cloud host. Never in the container. |
| Per-assignment service token | The container, scoped to one assignment's rows. |

**Why this is the right default for an OER.** Each adopting institution
uses its own key and pays its own LLM bill directly to the provider.
Nobody has to run a metered service, meter per-course usage, or handle
another institution's billing. The alternative, proxying LLM calls
through our service, would have made us the billing intermediary for
every adopter, which is a far larger operational commitment than a
grading tool should carry.

`llm_provider_config` in the database therefore says *what* to call,
which providers and models and which is primary. The container supplies
the credential.

**What this costs, to write into the instructor guide rather than the
code.** The key is visible to anyone with access to the assignment's
autograder configuration, which in practice includes TAs. Rotation means
touching every assignment that carries it. And how the key reaches the
container depends on what secret facility the platform offers; if the
only route available is the uploaded autograder zip, say so plainly,
because a key in a zip is a key in every backup of that zip. Confirm
what Gradescope actually provides here before writing that section.

### AD-6. results.json is written twice, tests first

The AI phase writes a tests-only `results.json` **before** grading
starts, then overwrites it with the full version after. Last write wins.

The reason: if the container is killed mid-grading, the original ordering
left the student with a bare autograder error and no test results at
all, even though the tests had succeeded and were already recorded in the
database. There is no reason to lose passing test results to an LLM
failure. Costs one extra file write.

### Why an IMMEDIATE grading fails, and whether retrying helps

Worth writing down because it determines the retry policy (OQ-5) and
because it is a stronger argument about `IMMEDIATE` than latency alone.

**Transient, worth retrying overnight:**

1. **Rate limiting.** Every submission for an assignment uses one
   instructor API key, and students submit in the last hour before the
   deadline. A burst of concurrent containers against a single key makes
   429 the expected case, not an edge case.
2. **Autograder timeout kills the container mid-grading.** LLM latency
   sits inside the platform's fixed timeout, and rate limiting makes
   latency worse, during the same spike.
3. **Provider 5xx.**
4. **Quota or billing exhausted** mid-term.

**Deterministic, where retrying only burns tokens:**

5. **Context overflow.** A student submits an enormous file and the
   prompt exceeds the model's limit.
6. **Model refuses, or returns output the parser cannot read.**

Two consequences.

**Classify before retrying.** A deterministic failure returns the same
result every attempt, so three nights of retries spend three times the
tokens to reach the same `GAVE_UP`. Route those there on the first
attempt. Only transient failures should consume `attempt_count`.

**Failures 1 and 2 are correlated with the deadline.** The failure rate
under `IMMEDIATE` is not uniform; it spikes exactly when the most
students are affected. That is a sharper argument against `IMMEDIATE`
for large courses than the latency point on its own, and it is why OQ-15
matters: without overnight retry, a deadline-hour 429 means that student
never gets feedback.

### AD-3. Target identifiers are a checked contract

Target identifiers cross a process boundary as bare strings: typed once
into the web app, again into the instructor's test suite, with no
compiler between them. Both directions of mismatch are now errors rather
than silent skips.

1. `POST /outcomes` rejects an unregistered target with a 4xx naming it
   and listing the registered ones. The reporting library raises rather
   than swallowing it, so her test run fails loudly on the first call.
2. The AI phase treats a registered target that received **no** outcome
   as an error. It exits non-zero and the autograder run fails. This
   catches the opposite mistake, renamed in the web app but still the old
   name in the suite.
3. Recommended, not yet decided: the web app offers a copy-paste block of
   the exact identifiers for an assignment, so the second typing is a
   paste.

One consequence to handle in the message text, not the schema: on
Gradescope this error is visible to the student and blocks their score.
That is the right trade for a misconfigured assignment, but the
diagnostic must say plainly that the assignment is misconfigured and the
student need do nothing.

### AD-4. Grading mode is per assignment; feedback mode is per target

Two settings, easily confused, answering different questions.

| Setting | Level | Values | Question |
| --- | --- | --- | --- |
| `grading_mode` | assignment | `IMMEDIATE`, `NIGHTLY` | **When** does AI grading happen? |
| `feedback_mode` | grading target | `NEVER`, `ALWAYS`, `AUTOGRADER` | **Whether** does this target get feedback? |

Because grading mode is per assignment, a submission is wholly immediate
or wholly nightly. There is no mixing, and therefore no submission that
receives partial feedback in one write and the rest in another.

How many times `results.json` gets written follows from the grading mode
alone:

| `grading_mode` | Writes |
| --- | --- |
| `IMMEDIATE` | Once, during the submission run. The guard is set; the submission never enters the nightly queue. |
| `NIGHTLY` | Twice: test results on the submission run, feedback on the rerun. |

In `NIGHTLY` mode the first write carries no AI feedback at all, which
reads correctly to a student as "not graded yet". A one-line marker per
pending task saying feedback arrives by the next morning is a courtesy
worth adding, but nothing depends on it.

For the instructor guide: `IMMEDIATE` puts LLM latency inside the
platform's autograder timeout, and submissions spike before a deadline.
Large courses should prefer `NIGHTLY`, and `IMMEDIATE` assignments are
worth capping to a single provider, using multi-provider comparison only
at night.

### AD-6. results.json is written twice, tests first

The AI phase writes a tests-only `results.json` **before** grading
starts, then overwrites it with the full version after. Last write wins.

The reason: if the container is killed mid-grading, the original ordering
left the student with a bare autograder error and no test results at
all, even though the tests had succeeded and were already recorded in the
database. There is no reason to lose passing test results to an LLM
failure. Costs one extra file write.

### Why an IMMEDIATE grading fails, and whether retrying helps

Worth writing down because it determines the retry policy (OQ-5) and
because it is a stronger argument about `IMMEDIATE` than latency alone.

**Transient, worth retrying overnight:**

1. **Rate limiting.** Every submission for an assignment uses one
   instructor API key, and students submit in the last hour before the
   deadline. A burst of concurrent containers against a single key makes
   429 the expected case, not an edge case.
2. **Autograder timeout kills the container mid-grading.** LLM latency
   sits inside the platform's fixed timeout, and rate limiting makes
   latency worse, during the same spike.
3. **Provider 5xx.**
4. **Quota or billing exhausted** mid-term.

**Deterministic, where retrying only burns tokens:**

5. **Context overflow.** A student submits an enormous file and the
   prompt exceeds the model's limit.
6. **Model refuses, or returns output the parser cannot read.**

Two consequences.

**Classify before retrying.** A deterministic failure returns the same
result every attempt, so three nights of retries spend three times the
tokens to reach the same `GAVE_UP`. Route those there on the first
attempt. Only transient failures should consume `attempt_count`.

**Failures 1 and 2 are correlated with the deadline.** The failure rate
under `IMMEDIATE` is not uniform; it spikes exactly when the most
students are affected. That is a sharper argument against `IMMEDIATE`
for large courses than the latency point on its own, and it is why OQ-15
matters: without overnight retry, a deadline-hour 429 means that student
never gets feedback.

### AD-3. Target identifiers are a checked contract

Target identifiers cross a process boundary as bare strings: typed once
into the web app, again into the instructor's test suite, with no
compiler between them. Both directions of mismatch are now errors rather
than silent skips.

1. `POST /outcomes` rejects an unregistered target with a 4xx naming it
   and listing the registered ones. The reporting library raises rather
   than swallowing it, so her test run fails loudly on the first call.
2. The AI phase treats a registered target that received **no** outcome
   as an error. It exits non-zero and the autograder run fails. This
   catches the opposite mistake, renamed in the web app but still the old
   name in the suite.
3. Recommended, not yet decided: the web app offers a copy-paste block of
   the exact identifiers for an assignment, so the second typing is a
   paste.

One consequence to handle in the message text, not the schema: on
Gradescope this error is visible to the student and blocks their score.
That is the right trade for a misconfigured assignment, but the
diagnostic must say plainly that the assignment is misconfigured and the
student need do nothing.

### AD-4. Feedback mode is per target, not per assignment

`grading_target.feedback_mode` is `IMMEDIATE` or `NIGHTLY`. There is no
mode column on `assignment`; the web app can offer "apply to all
targets" as a convenience, which needs UI but no schema.

A single submission can therefore mix the two. How many times
`results.json` is written depends on whether **any** target is
`NIGHTLY`, not on whether the submission is mixed:

| Targets | `results.json` writes |
| --- | --- |
| All `IMMEDIATE` | Once, on the submission run. Guard is set; never enters the nightly queue. |
| Any `NIGHTLY` | Twice: once on the submission run, once on the rerun. |

This needs no extra machinery, which is the nice part. After the
immediate pass the `NIGHTLY` tasks are still `NEEDED`, so
`ai_graded_submitted_at` stays NULL and tonight's trigger picks the
submission up on its own. On the rerun, the `IMMEDIATE` tasks are
already `DONE` and get skipped, so a mixed submission never pays twice.

For the instructor guide: `IMMEDIATE` puts LLM latency inside the
platform's autograder timeout, and submissions spike before a deadline.
Large courses should prefer `NIGHTLY`. An `IMMEDIATE` assignment whose
grading fails is picked up by the nightly trigger and retried, subject to
OQ-15.

**AD-2 superseded an earlier revision of these diagrams.** They showed
our entrypoint fetching the target list and invoking a test runner per
target, with an `ITestRunner` interface and an `IMarkingHook` for
instructor logic. Control runs the other way: her suite calls us, and
the marking logic is ordinary code in that suite rather than a hook we
invoke.

### OQ-15: failed IMMEDIATE grading

If an `IMMEDIATE` submission's AI grading fails, its task stays `NEEDED`
and `ai_graded_submitted_at` is never set. What happens next depends on
one line of the trigger's query.

If the trigger filters on `grading_mode = NIGHTLY`, that task is stuck
forever: nothing retries it, and the submission stays permanently
half-graded with no feedback and no error the student can see.

If the trigger ignores `grading_mode` and selects any submission with a
task still `NEEDED`, the failure self-heals overnight. The student gets
the feedback the next morning instead of at once, which is a mild
degradation and much better than never.

**Recommend the latter**, which is what the query in
`04-sequence-nightly.puml` shows. It costs nothing and it is the
difference between a transient LLM error being invisible and being
permanent.

The failure taxonomy above makes this concrete rather than theoretical.
The most likely `IMMEDIATE` failure is rate limiting during the
deadline-hour submission spike, which is both transient and
simultaneous across many students. Without overnight retry, a single
burst of 429s silently denies feedback to everyone who submitted in that
window, and nothing in the system would say so.

**AD-4 was corrected.** An earlier revision put `IMMEDIATE`/`NIGHTLY` on
the grading target rather than the assignment, which allowed mixed-mode
submissions and created a real problem: the first `results.json` carried
feedback for some targets and silence for others, which a student reads
as "those were fine". That whole complication was an artifact of the
misplacement and is gone.

# Architecture decisions and open questions

---

## AD-1. How functional test outcomes and AI feedback move through the LMS

**Status: DECIDED** by the instructor, 2026. Supersedes the earlier
two-pass proposal, and supersedes SRS section 3.4, which was ambiguous
on this point and should be rewritten to match.

The instructor chooses a grading mode per assignment.

**Immediate mode.** The AI grading component runs as part of the
autograder script that the LMS executes on student submission. One
pass: functional tests, AI feedback, `results.json`, done. No nightly
job is involved.

**Nightly mode.** The autograder script on submission runs functional
tests only, and marks in the database which grading targets need AI
grading. Every night a job runner uses browser automation to log in and
click "Rerun Autograder". During that rerun, the same autograder script
queries the database for marked targets and runs the AI component only
on those.

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

### The AI-needed marker, and instructor control over it

`grading_task.ai_needed` is the per-submission, per-target marker. The
submission autograder writes it after functional testing. It is the
main token saving in the system: a target whose tests passed has
nothing to explain, so it is never sent to an LLM.

The instructor controls it through `grading_target.ai_feedback_policy`,
set per target in the web app:

| Policy | Behaviour |
| --- | --- |
| `ON_FAILURE` | Mark only when this target's tests fail. The default. |
| `ALWAYS` | Mark every submission, including passing code. For style or design feedback no test can detect. |
| `NEVER` | Never mark. For targets graded purely by tests. |
| `AUTOGRADER` | The instructor's own autograder logic decides. |

`AUTOGRADER` is the escape hatch for conditions no generic rule
captures: a partial-credit threshold, a target-specific heuristic, a
cross-target condition.

`ai_needed_reason` records which rule fired (`TEST_FAILURE`,
`POLICY_ALWAYS`, `AUTOGRADER_RULE`). Store it. It is how the instructor
distinguishes "correct code, nothing to say" from "feedback is late"
from "this target gave up" on the dashboard, and how the marking rules
get audited against real submissions. Without it all three render as a
blank cell.

**Marking happens only during the submission autograder run.** There is
no post-hoc path to request AI feedback on a submission that has
already been graded. Once a submission is AI-graded it is finished: the
nightly trigger stops selecting it and the guard below short-circuits
any other rerun.

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
| OQ-11 | Where do LLM API keys and DB credentials live, given the AI component runs on Gradescope's infrastructure? | A and C both; the service API surface | A + C |
| OQ-1 | What exactly is a "deduction" (FR-10)? | B's dashboard query | Instructor |
| OQ-2 | How is confidence defined and computed (FR-28)? | A's engine, B's UI | A |
| OQ-3 | Can the instructor edit feedback before release? Now mode-dependent. | state machine, B's dashboard | Instructor |
| OQ-4 | Are all submission attempts graded, or only the latest? | cost, C's task creation | Instructor + A |
| OQ-5 | Retry policy and the attempt cap before GAVE_UP | A and C will otherwise write two | D |
| OQ-6 | Exact Gradescope autograder JSON schema | C's packager, D's tests | C |
| OQ-7 | Gradescope authentication, and where that credential lives | C's trigger job | C |
| OQ-8 | Web framework, database, cloud host | B cannot start | B |
| OQ-9 | Instructor authentication and authorization | B's backend | B |
| OQ-10 | Student code retention, privacy, FERPA | deployment | Instructor |

### OQ-11 is the new one and it is the most urgent

The AI grading component executes inside the LMS autograder container,
which is not your infrastructure. Two options:

**(a) Credentials in the container.** It holds an LLM API key and talks
to the database directly. Simplest code. But every adopting instructor
must paste their own API key into Gradescope's autograder settings,
where any TA with assignment access can read it, and a database
credential would be sitting in a third party's container.

**(b) Credentials on our side.** The container holds only a scoped
per-assignment token and calls the service through `IGradingClient`.
The service does the LLM work. Keys never leave our infrastructure.
Costs a synchronous generate-feedback endpoint.

**Recommend (b)**, and the diagrams show (b). For an OER this matters
more than usual: an adopting instructor at another institution should be
able to use this without handling provider keys, and the separation of
`IGradingClient` from `IGradingRepository` in `07-interfaces.puml` is
what makes the credential boundary visible in code rather than a
deployment convention nobody remembers.

Switching to (a) is local to the AI grading package, so this is
reversible, but it needs deciding before A and C agree the API surface.

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
at marking time, through `ai_feedback_policy` and their own autograder
logic.

# Architecture decisions and open questions

Two sections. The first records a choice that is already baked into the
diagrams and should be ratified or overturned in the next team meeting.
The second lists questions the diagrams cannot answer, each of which
will otherwise be answered differently by two different students.

---

## AD-1. How functional test outcomes and feedback move through Gradescope

**Status:** proposed, needs team ratification.

The SRS supports more than one reading. FR-31 and FR-32 suggest the
nightly job pulls submissions out of Gradescope and tests them in our
own environment. The discussion notes instead describe a Gradescope
autograder script that reads feedback from the database and packages it
to JSON, which implies something quite different. These are not
compatible, and the diagrams had to pick one.

### Option A: two-pass regrade (diagrammed in `03-sequence-nightly.puml`)

Our autograder client runs inside Gradescope's container on submit,
executes the functional tests, and posts outcomes plus source files to
our Web API. Overnight, the feedback module generates feedback into the
database. The nightly job then browser-automates a regrade, the
autograder client runs a second time, fetches the finished feedback, and
writes it into `results.json`.

- Works with how Gradescope actually delivers autograder output, so no
  reverse-engineering of the manual comment UI.
- The test harness runs where Gradescope already provides the language
  runtime and the student's files, which is much less work than
  reproducing that environment.
- Nightly batching keeps LLM cost controllable and leaves room to call
  several providers and compare them without a student waiting.
- Costs a regrade trigger per submission through browser automation,
  which is the most fragile part of the system.
- Students see test results immediately and feedback the next day. Two
  separate notifications for one submission may confuse them.

### Option B: pull and test in our environment

The nightly job scrapes submission files with Playwright, runs the
functional tests on our own infrastructure, generates feedback, then
pushes everything back.

- No autograder client to write, and no network calls out of
  Gradescope's container.
- We must reproduce the full build and test environment for every
  language an assignment uses, which is most of what Gradescope's
  autograder already does for us.
- Delivering feedback without the autograder means automating
  Gradescope's per-question comment UI, which is considerably more
  fragile than triggering a regrade.

### Option C: single-pass synchronous

The autograder client runs the tests, posts outcomes, then calls our API
synchronously to generate feedback, waits, and writes one `results.json`
containing both.

- Simplest control flow by a wide margin. No regrade trigger, no
  browser automation on the delivery path, one notification, feedback
  arrives in minutes.
- Directly contradicts FR-41 and CI-3, which specify a scheduled
  workflow. Those requirements would need rewriting.
- Puts LLM latency inside Gradescope's autograder timeout, and makes
  multi-provider comparison expensive at submission peaks, which for a
  course assignment means the hour before the deadline.
- Loses the nightly batch window that makes cost and rate limits
  manageable.

### Recommendation

Go with Option A, because it matches what is written in the SRS and in
the discussion notes, and because it puts the functional tests where the
runtime already exists.

Worth a deliberate look at Option C first, though. It is a much simpler
system, and if the team decides feedback latency matters more than the
scheduled-workflow requirement, changing the SRS now is far cheaper than
discovering it in November. The deciding question is whether you want
students to get feedback in minutes or the next morning, which is a
pedagogical call, not a technical one.

---

## Open questions

Each of these is listed as out of scope or not yet specified in SRS
section 10. That is reasonable for a requirements document and not
survivable for four people implementing in parallel. Assign an owner and
a date to each.

| ID | Question | Blocks | Suggested owner |
| --- | --- | --- | --- |
| OQ-1 | What exactly is a "deduction" (FR-10)? | B's dashboard query | Instructor |
| OQ-2 | How is confidence defined and computed (FR-28)? | A's engine, B's UI rendering | A |
| OQ-3 | Can an instructor edit generated feedback before delivery, or only rate it after? | state machine, B's dashboard, nightly job blocking on a human | Instructor |
| OQ-4 | Are all submission attempts graded, or only the latest? | LLM cost, A's estimates, C's discovery logic | Instructor + A |
| OQ-5 | What is the retry and failure policy? | A and C will otherwise write two different ones | D |
| OQ-6 | Exact Gradescope autograder JSON schema | C's packager, D's integration tests | C |
| OQ-7 | Gradescope authentication mechanism, and where the credential lives | C's adapter, deployment secrets | C |
| OQ-8 | Web framework, database technology, cloud host | B cannot start without these | B |
| OQ-9 | Instructor authentication and authorization | B's backend; currently the app has no access control at all | B |
| OQ-10 | Student code retention, privacy, FERPA posture | deployment against a live course | Instructor |

OQ-1 through OQ-4 need an instructor ruling rather than a student
decision, so they are the ones to settle in the first meeting.

OQ-9 and OQ-10 deserve a flag. As specified, the system stores student
source code and identifiers in a cloud-hosted database behind a web app
with no stated authentication requirement. That is fine for a prototype
with synthetic data. Before it holds real work from a real course, the
access control story and the university's position on student data both
need to exist. Pitt has people whose job this is, and asking early costs
an email.

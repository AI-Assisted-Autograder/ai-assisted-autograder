# Test examples

`SCENARIOS` maps each name to a function returning a fresh `FeedbackRequest`.
All cases use synthetic code and test feedback

| Name | Input case |
| --- | --- |
| `empty-list` | Missing return for empty input |
| `multi-file` | One source file plus the name of an unseen helper |
| `passing-style` | Passing tests with readability issues |
| `error-skip` | Test environment errors and skipped tests |

Run one case with `python -m ai_grading.demo --scenario multi-file`, or list names
with `python -m ai_grading.demo --list-scenarios`.

`request.json` contains the `multi-file` input. `response.json` is the result from
`FakeProvider`, which returns the same placeholder text for every case.
Use these files to check the interface, not model quality.

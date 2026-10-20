import { expect, test } from '@playwright/test';

import { FakeGradingClient } from '../../../integration/fake-grading-client';
import { FakePlatform } from '../../../integration/fake-platform';
import { NightlyTrigger } from '../../../integration/nightly-trigger';
import type { SubmissionLocator } from '../../../integration/interfaces';

const submissions: SubmissionLocator[] = [
  {
    submissionId: 'submission-1',
    platformAssignmentId: 'assignment-1',
    platformSubmissionUrl: 'https://www.gradescope.com/courses/course-1/assignments/assignment-1/submissions/submission-1',
  },
  {
    submissionId: 'submission-2',
    platformAssignmentId: 'assignment-1',
    platformSubmissionUrl: 'https://www.gradescope.com/courses/course-1/assignments/assignment-1/submissions/submission-2',
  },
];

test('NightlyTrigger authorizes and reruns every pending submission', async () => {
  const client = new FakeGradingClient(submissions);
  const platform = new FakePlatform();

  const summary = await new NightlyTrigger(client, platform).run();

  expect(platform.authenticated).toBe(true);
  expect(platform.rerunCalls).toHaveLength(2);
  expect(client.authorizedSubmissionIds).toEqual(['submission-1', 'submission-2']);
  expect(summary).toEqual({ found: 2, authorized: 2, rerun: 2, failed: 0 });
  expect(client.finishedSummary).toEqual(summary);
});

test('NightlyTrigger records one failure and continues with later submissions', async () => {
  const client = new FakeGradingClient(submissions);
  const platform = new FakePlatform(new Set(['submission-1']));

  const summary = await new NightlyTrigger(client, platform).run();

  expect(platform.rerunCalls).toHaveLength(2);
  expect(summary).toEqual({ found: 2, authorized: 2, rerun: 1, failed: 1 });
  expect(client.recordedResults).toContainEqual({
    submissionId: 'submission-1',
    status: 'RERUN_FAILED',
    errorMessage: 'fake rerun failure for submission-1',
  });
});

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
  expect(summary).toMatchObject({ found: 2, authorized: 2, rerun: 2, failed: 0 });
  expect(summary.outcomes).toEqual([
    { submissionId: 'submission-1', status: 'AUTHORIZED' },
    { submissionId: 'submission-1', status: 'RERUN_SUBMITTED' },
    { submissionId: 'submission-2', status: 'AUTHORIZED' },
    { submissionId: 'submission-2', status: 'RERUN_SUBMITTED' },
    { submissionId: 'submission-1', status: 'RERUN_SUCCEEDED' },
    { submissionId: 'submission-2', status: 'RERUN_SUCCEEDED' },
  ]);
  expect(client.finishedSummary).toEqual(summary);
});

test('NightlyTrigger records one failure and continues with later submissions', async () => {
  const client = new FakeGradingClient(submissions);
  const platform = new FakePlatform(new Set(['submission-1']));

  const summary = await new NightlyTrigger(client, platform).run();

  expect(platform.rerunCalls).toHaveLength(2);
  expect(summary).toMatchObject({
    found: 2,
    authorized: 2,
    rerun: 1,
    failed: 1,
    failures: [{
      submissionId: 'submission-1',
      phase: 'trigger',
      kind: 'RERUN_TRIGGER',
      message: 'fake rerun failure for submission-1',
    }],
  });
  expect(summary.outcomes).toContainEqual({
    submissionId: 'submission-1',
    status: 'RERUN_TRIGGER_FAILED',
    kind: 'RERUN_TRIGGER',
    message: 'fake rerun failure for submission-1',
  });
  expect(client.recordedResults).toContainEqual({
    submissionId: 'submission-1',
    status: 'RERUN_FAILED',
    errorMessage: 'fake rerun failure for submission-1',
  });
});

test('NightlyTrigger keeps a real rerun successful when only its audit write fails', async () => {
  const client = new FakeGradingClient(submissions, 'RERUN_SUBMITTED');
  const platform = new FakePlatform();

  const summary = await new NightlyTrigger(client, platform).run();

  expect(platform.rerunCalls).toHaveLength(2);
  expect(summary).toMatchObject({
    found: 2,
    authorized: 2,
    rerun: 2,
    failed: 0,
    auditErrors: [
      'submission-1: fake audit failure for RERUN_SUBMITTED',
      'submission-2: fake audit failure for RERUN_SUBMITTED',
    ],
  });
  expect(summary.outcomes).toEqual([
    { submissionId: 'submission-1', status: 'AUTHORIZED' },
    { submissionId: 'submission-1', status: 'RERUN_SUBMITTED' },
    { submissionId: 'submission-2', status: 'AUTHORIZED' },
    { submissionId: 'submission-2', status: 'RERUN_SUBMITTED' },
    { submissionId: 'submission-1', status: 'RERUN_SUCCEEDED' },
    { submissionId: 'submission-2', status: 'RERUN_SUCCEEDED' },
  ]);
});

test('NightlyTrigger retries only submissions returned by the AI-failure query', async () => {
  const failedSubmission = submissions[1];
  const client = new FakeGradingClient(submissions, undefined, [failedSubmission]);
  const platform = new FakePlatform();

  const summary = await new NightlyTrigger(client, platform).run('AI_FAILURES');

  expect(platform.rerunCalls).toEqual([failedSubmission]);
  expect(client.authorizedSubmissionIds).toEqual(['submission-2']);
  expect(summary).toMatchObject({
    found: 1,
    authorized: 1,
    rerun: 1,
    failed: 0,
  });
});

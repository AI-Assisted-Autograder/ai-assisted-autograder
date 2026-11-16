import { expect, test } from '@playwright/test';
import { mock } from 'node:test';

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

test.afterEach(() => mock.restoreAll());

for (const source of ['PENDING_NIGHTLY', 'AI_FAILURES'] as const) {
  for (const phase of ['read', 'sync'] as const) {
    test(`${source} continues other assignments after a snapshot ${phase} failure`, async () => {
      const healthySubmission = { ...submissions[1], platformAssignmentId: 'assignment-2' };
      const rows = [submissions[0], healthySubmission];
      const client = new FakeGradingClient(rows);
      const platform = new FakePlatform(rows);
      const readSnapshot = platform.listActiveSubmissionIds.bind(platform);
      mock.method(platform, 'listActiveSubmissionIds', async (assignmentId: string) => {
        if (phase === 'read' && assignmentId === 'assignment-1') {
          throw new Error('Incomplete Gradescope snapshot');
        }
        return readSnapshot(assignmentId);
      });
      const sync = mock.method(client, 'syncActiveSubmissions', async (assignmentId: string) => {
        if (phase === 'sync' && assignmentId === 'db-assignment-1') {
          throw new Error('409 missing_submission_ids');
        }
      });

      const summary = await new NightlyTrigger(client, platform).run(source);

      expect(platform.rerunCalls).toEqual([healthySubmission]);
      expect(client.authorizedSubmissionIds).toEqual(['submission-2']);
      expect(summary).toMatchObject({
        found: 1, authorized: 1, rerun: 1, failed: 0,
        syncErrors: [{
          assignmentId: 'db-assignment-1',
          message: phase === 'read' ? 'Incomplete Gradescope snapshot' : '409 missing_submission_ids',
        }],
      });
      if (phase === 'read') {
        expect(sync.mock.calls.map((call) => call.arguments[0])).toEqual(['db-assignment-2']);
      }
      expect(client.finishedSummary).toEqual(summary);
    });
  }
}

test('AI_FAILURES recovers failed nightly triggers with nightly authorization and no duplicate rerun', async () => {
  const client = new FakeGradingClient(submissions, undefined, [submissions[1]]);
  const platform = new FakePlatform(submissions);
  const pending = mock.method(client, 'listSubmissionsPendingAi', async () => submissions);
  const authorize = mock.method(client, 'authorizeForGrading');

  const summary = await new NightlyTrigger(client, platform).run('AI_FAILURES');

  expect(pending.mock.calls[0].arguments).toEqual(['NIGHTLY', true]);
  expect(authorize.mock.calls.map((call) => call.arguments)).toEqual([
    ['submission-2', 'AI_FAILURES'], ['submission-1', 'PENDING_NIGHTLY'],
  ]);
  expect(platform.rerunCalls).toEqual([submissions[1], submissions[0]]);
  expect(summary).toMatchObject({ found: 2, authorized: 2, rerun: 2, failed: 0 });
});

test('NightlyTrigger authorizes and reruns every pending submission', async () => {
  const client = new FakeGradingClient(submissions);
  const platform = new FakePlatform(submissions);

  const summary = await new NightlyTrigger(client, platform).run();

  expect(platform.authenticated).toBe(true);
  expect(platform.rerunCalls).toHaveLength(2);
  expect(client.authorizedSubmissionIds).toEqual(['submission-1', 'submission-2']);
  expect(summary).toMatchObject({ found: 2, authorized: 2, rerun: 2, failed: 0 });
  expect(summary.outcomes).toEqual([
    { submissionId: 'submission-1', status: 'AUTHORIZED' },
    { submissionId: 'submission-1', status: 'RERUN_SUBMITTED', confirmationEvidence: 'status-text' },
    { submissionId: 'submission-2', status: 'AUTHORIZED' },
    { submissionId: 'submission-2', status: 'RERUN_SUBMITTED', confirmationEvidence: 'status-text' },
    { submissionId: 'submission-1', status: 'RERUN_SUCCEEDED' },
    { submissionId: 'submission-2', status: 'RERUN_SUCCEEDED' },
  ]);
  expect(client.finishedSummary).toEqual(summary);
});

test('NightlyTrigger records one failure and continues with later submissions', async () => {
  const client = new FakeGradingClient(submissions);
  const platform = new FakePlatform(submissions, new Set(['submission-1']));

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
  const platform = new FakePlatform(submissions);

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
    { submissionId: 'submission-1', status: 'RERUN_SUBMITTED', confirmationEvidence: 'status-text' },
    { submissionId: 'submission-2', status: 'AUTHORIZED' },
    { submissionId: 'submission-2', status: 'RERUN_SUBMITTED', confirmationEvidence: 'status-text' },
    { submissionId: 'submission-1', status: 'RERUN_SUCCEEDED' },
    { submissionId: 'submission-2', status: 'RERUN_SUCCEEDED' },
  ]);
});

test('NightlyTrigger retries only submissions returned by the AI-failure query', async () => {
  const failedSubmission = submissions[1];
  const client = new FakeGradingClient(submissions, undefined, [failedSubmission]);
  const platform = new FakePlatform(submissions);

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

test('NightlyTrigger can submit without waiting for completion', async () => {
  const client = new FakeGradingClient(submissions);
  const platform = new FakePlatform(submissions);

  const summary = await new NightlyTrigger(client, platform).run('PENDING_NIGHTLY', {
    waitForCompletion: false,
  });

  expect(platform.rerunCalls).toHaveLength(2);
  expect(platform.waitForRerunCalls).toHaveLength(0);
  expect(summary).toMatchObject({ found: 2, authorized: 2, rerun: 2, failed: 0 });
  expect(summary.outcomes).not.toContainEqual({
    submissionId: 'submission-1',
    status: 'RERUN_SUCCEEDED',
  });
  expect(client.recordedResults).toContainEqual({
    submissionId: 'submission-1',
    status: 'RERUN_SUBMITTED',
  });
});

test('NightlyTrigger does not report success while backend still reports an AI failure', async () => {
  const failedSubmission = submissions[0];
  const client = new FakeGradingClient(
    submissions,
    undefined,
    submissions,
    [failedSubmission],
  );
  const platform = new FakePlatform(submissions);

  const summary = await new NightlyTrigger(client, platform).run();

  expect(summary).toMatchObject({
    rerun: 2,
    failed: 1,
    failures: [{
      submissionId: 'submission-1',
      phase: 'completion',
      kind: 'AI_FEEDBACK',
    }],
  });
  expect(client.recordedResults).not.toContainEqual({
    submissionId: 'submission-1',
    status: 'RERUN_SUCCEEDED',
  });
});

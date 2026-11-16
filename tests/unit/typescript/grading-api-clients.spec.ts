import { expect, test } from '@playwright/test';
import { mock } from 'node:test';

import { HttpGradingClient } from '../../../integration/grading-api-client';
import type { TriggerRunSummary } from '../../../integration/interfaces';

const originalEnv = { ...process.env };

test.beforeEach(() => {
  process.env.GRADING_API_URL = 'http://localhost:8000';
  process.env.INTEGRATION_API_TOKEN = 'test-token';
  process.env.GRADESCOPE_COURSE_ID = 'course-123';
});

test.afterEach(() => {
  mock.restoreAll();
  process.env = { ...originalEnv };
});

function mockFetch(
  responses: Array<{ status: number; body: unknown }>,
) {
  return mock.method(
    globalThis,
    'fetch',
    async () => {
      const response = responses.shift();

      if (!response) {
        throw new Error('No mocked fetch response available');
      }

      return new Response(JSON.stringify(response.body), {
        status: response.status,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  );
}

test('startRun sends the internal token and stores the run ID', async () => {
  const fetchMock = mockFetch([
    { status: 200, body: { run_id: 'run-123' } },
  ]);

  const client = new HttpGradingClient();

  await client.startRun();

  expect(fetchMock.mock.calls).toHaveLength(1);
  expect(fetchMock.mock.calls[0].arguments[0]).toBe(
    'http://localhost:8000/internal/nightly/runs',
  );
  expect(fetchMock.mock.calls[0].arguments[1]).toMatchObject({
    method: 'POST',
    headers: { 'X-Internal-Token': 'test-token' },
  });
});

test('listSubmissionsPendingAi maps API rows to submission locators', async () => {
  mockFetch([
    {
      status: 200,
      body: [
        {
          submission_id: 'submission-1',
          platform: 'GRADESCOPE',
          platform_assignment_id: 'assignment-456',
          platform_submission_id: 'submission-789',
          student_identifier: null,
        },
      ],
    },
  ]);

  const client = new HttpGradingClient();

  const submissions = await client.listSubmissionsPendingAi('NIGHTLY');

  expect(submissions).toEqual([
    {
      submissionId: 'submission-1',
      platformAssignmentId: 'assignment-456',
      platformSubmissionUrl:
        'https://www.gradescope.com/courses/course-123/assignments/assignment-456/submissions/submission-789',
    },
  ]);
});

test('listSubmissionsPendingAi rejects a missing course ID when submissions exist', async () => {
  process.env.GRADESCOPE_COURSE_ID = '';

  mockFetch([
    {
      status: 200,
      body: [
        {
          submission_id: 'submission-1',
          platform: 'GRADESCOPE',
          platform_assignment_id: 'assignment-456',
          platform_submission_id: 'submission-789',
          student_identifier: null,
        },
      ],
    },
  ]);

  const client = new HttpGradingClient();

  await expect(
    client.listSubmissionsPendingAi('NIGHTLY'),
  ).rejects.toThrow('Set GRADESCOPE_COURSE_ID');
});

test('listSubmissionsFailedOnAi calls the AI-failure endpoint and maps rows', async () => {
  const fetchMock = mockFetch([
    {
      status: 200,
      body: [{
        submission_id: 'submission-failed',
        platform: 'GRADESCOPE',
        platform_assignment_id: 'assignment-456',
        platform_submission_id: 'submission-789',
        student_identifier: 'student@example.com',
        ai_status: 'FAILED',
      }],
    },
  ]);

  const client = new HttpGradingClient();

  await expect(client.listSubmissionsFailedOnAi()).resolves.toEqual([{
    submissionId: 'submission-failed',
    platformAssignmentId: 'assignment-456',
    platformSubmissionUrl:
      'https://www.gradescope.com/courses/course-123/assignments/assignment-456/submissions/submission-789',
  }]);
  expect(fetchMock.mock.calls[0].arguments[0]).toBe(
    'http://localhost:8000/internal/ai-failures/submissions',
  );
  expect(fetchMock.mock.calls[0].arguments[1]).toMatchObject({
    headers: { 'X-Internal-Token': 'test-token' },
  });
});

test('authorizeForGrading sends a POST request for the selected submission', async () => {
  const fetchMock = mockFetch([
    { status: 200, body: {} },
  ]);

  const client = new HttpGradingClient();

  await client.authorizeForGrading('submission-123', 'PENDING_NIGHTLY');

  expect(fetchMock.mock.calls[0].arguments[0]).toBe(
    'http://localhost:8000/internal/nightly/submissions/submission-123/authorize',
  );
  expect(fetchMock.mock.calls[0].arguments[1]).toMatchObject({
    method: 'POST',
    headers: {
      'X-Internal-Token': 'test-token',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ trigger_source: 'PENDING_NIGHTLY' }),
  });
});

test('recordSubmissionResult sends the result and error message', async () => {
  const fetchMock = mockFetch([
    { status: 200, body: { run_id: 'run-123' } },
    { status: 200, body: {} },
  ]);

  const client = new HttpGradingClient();

  await client.startRun();
  await client.recordSubmissionResult(
    'submission-123',
    'RERUN_FAILED',
    'Gradescope unavailable',
  );

  expect(fetchMock.mock.calls).toHaveLength(2);
  expect(fetchMock.mock.calls[1].arguments[0]).toBe(
    'http://localhost:8000/internal/nightly/runs/run-123/submissions/submission-123',
  );
  expect(fetchMock.mock.calls[1].arguments[1]).toMatchObject({
    method: 'POST',
    headers: {
      'X-Internal-Token': 'test-token',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      status: 'RERUN_FAILED',
      error_message: 'Gradescope unavailable',
    }),
  });
});

test('finishRun sends the aggregate summary', async () => {
  const fetchMock = mockFetch([
    { status: 200, body: { run_id: 'run-123' } },
    { status: 200, body: {} },
  ]);

  const client = new HttpGradingClient();

  await client.startRun();

  const summary: TriggerRunSummary = {
    found: 2,
    authorized: 2,
    rerun: 1,
    failed: 1,
  };

  await client.finishRun(summary);

  expect(fetchMock.mock.calls[1].arguments[0]).toBe(
    'http://localhost:8000/internal/nightly/runs/run-123/finish',
  );
  expect(fetchMock.mock.calls[1].arguments[1]).toMatchObject({
    method: 'POST',
    headers: {
      'X-Internal-Token': 'test-token',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      status: 'FAILED',
      submissions_authorized: 2,
      submissions_rerun: 1,
      submissions_failed_to_trigger: 1,
    }),
  });
});

test('throws when the API returns an error', async () => {
  mockFetch([
    { status: 500, body: { detail: 'Internal error' } },
  ]);

  const client = new HttpGradingClient();

  await expect(client.startRun()).rejects.toThrow(
    'Database API returned 500 while starting the nightly run.',
  );
});

test('throws when the internal API token is missing', async () => {
  delete process.env.INTEGRATION_API_TOKEN;

  const client = new HttpGradingClient();

  await expect(client.startRun()).rejects.toThrow(
    'Set INTEGRATION_API_TOKEN before using the grading database API.',
  );
});

test('throws when finishing a run that was never started', async () => {
  const client = new HttpGradingClient();

  await expect(
    client.finishRun({
      found: 0,
      authorized: 0,
      rerun: 0,
      failed: 0,
    }),
  ).rejects.toThrow('Cannot finish a nightly run that was not started.');
});

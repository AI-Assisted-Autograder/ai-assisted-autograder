import { afterEach, describe, expect, it } from 'vitest';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { once } from 'node:events';

import { HttpGradingClient } from '../../../integration/grading-api-client';
import { FakePlatform } from '../../../integration/fake-platform';
import { NightlyTrigger } from '../../../integration/nightly-trigger';

const submissionId = '11111111-1111-4111-8111-111111111111';
const assignmentId = '22222222-2222-4222-8222-222222222222';
const platformAssignmentId = '98765';
const platformSubmissionId = '12345';

let server: Server | undefined;
let previousEnv: Record<string, string | undefined> = {};

function json(response: ServerResponse, status: number, body: unknown) {
  response.writeHead(status, { 'content-type': 'application/json' });
  response.end(JSON.stringify(body));
}

async function startApiDouble(authorizationStatus = 200) {
  const calls: Array<{ method: string; path: string; body?: unknown; token?: string }> = [];
  server = createServer(async (request: IncomingMessage, response: ServerResponse) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const raw = Buffer.concat(chunks).toString('utf8');
    const body = raw ? JSON.parse(raw) : undefined;
    calls.push({
      method: request.method ?? 'GET',
      path: request.url ?? '/',
      body,
      token: request.headers['x-internal-token'] as string | undefined,
    });

    if (request.url === '/internal/nightly/runs' && request.method === 'POST') {
      return json(response, 200, { run_id: '33333333-3333-4333-8333-333333333333' });
    }
    if (request.url === '/internal/assignments') {
      return json(response, 200, [{ assignment_id: assignmentId, platform_assignment_id: platformAssignmentId }]);
    }
    if (request.url === `/internal/assignments/${assignmentId}/active-submissions`) {
      return json(response, 200, { synchronized: true });
    }
    if (request.url === '/internal/ai-failures/submissions') return json(response, 200, []);
    if (request.url === '/internal/nightly/submissions?mode=NIGHTLY') {
      return json(response, 200, [{
        submission_id: submissionId,
        platform: 'GRADESCOPE',
        platform_assignment_id: platformAssignmentId,
        platform_submission_id: platformSubmissionId,
        student_identifier: null,
      }]);
    }
    if (request.url === `/internal/nightly/submissions/${submissionId}/authorize`) {
      return json(response, authorizationStatus, { detail: 'authorization response' });
    }
    if (request.url?.startsWith('/internal/nightly/runs/') &&
        (request.url.includes('/submissions/') || request.url.endsWith('/finish'))) {
      return json(response, 200, { recorded: true });
    }
    return json(response, 404, { detail: 'not found' });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Test API did not bind a TCP port');
  return { calls, baseUrl: `http://127.0.0.1:${address.port}` };
}

function configureClient(baseUrl: string) {
  for (const key of ['GRADING_API_URL', 'INTEGRATION_API_TOKEN', 'GRADESCOPE_COURSE_ID']) {
    if (!(key in previousEnv)) previousEnv[key] = process.env[key];
  }
  process.env.GRADING_API_URL = baseUrl;
  process.env.INTEGRATION_API_TOKEN = 'integration-test-secret';
  process.env.GRADESCOPE_COURSE_ID = '456';
}

function platform() {
  return new FakePlatform([{
    submissionId,
    platformAssignmentId,
    platformSubmissionUrl: `https://www.gradescope.com/courses/456/assignments/${platformAssignmentId}/submissions/${platformSubmissionId}`,
  }]);
}

afterEach(async () => {
  if (server) {
    const closing = server;
    server = undefined;
    closing.close();
    await once(closing, 'close');
  }
  for (const [key, value] of Object.entries(previousEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  previousEnv = {};
});

describe('nightly trigger + HTTP grading client integration', () => {
  it('syncs platform state, authorizes before rerun, and persists the final outcome', async () => {
    const api = await startApiDouble();
    configureClient(api.baseUrl);
    const learningPlatform = platform();

    const summary = await new NightlyTrigger(new HttpGradingClient(), learningPlatform)
      .run('PENDING_NIGHTLY');

    expect(summary).toMatchObject({ found: 1, authorized: 1, rerun: 1, failed: 0 });
    expect(learningPlatform.authenticated).toBe(true);
    expect(learningPlatform.rerunCalls).toHaveLength(1);
    expect(learningPlatform.waitForRerunCalls).toHaveLength(1);

    const authorizeIndex = api.calls.findIndex((call) => call.path.endsWith('/authorize'));
    const submittedIndex = api.calls.findIndex((call) =>
      call.body && (call.body as { status?: string }).status === 'RERUN_SUBMITTED');
    const finishIndex = api.calls.findIndex((call) => call.path.endsWith('/finish'));
    expect(authorizeIndex).toBeGreaterThan(-1);
    expect(submittedIndex).toBeGreaterThan(authorizeIndex);
    expect(finishIndex).toBeGreaterThan(submittedIndex);
    expect(api.calls.every((call) => call.token === 'integration-test-secret')).toBe(true);
    expect(api.calls.some((call) => call.path.includes('/active-submissions'))).toBe(true);
    expect(api.calls.some((call) =>
      call.body && (call.body as { status?: string }).status === 'RERUN_SUCCEEDED')).toBe(true);
  });

  it('does not call the learning platform when the API refuses authorization', async () => {
    const api = await startApiDouble(409);
    configureClient(api.baseUrl);
    const learningPlatform = platform();

    const summary = await new NightlyTrigger(new HttpGradingClient(), learningPlatform)
      .run('PENDING_NIGHTLY');

    expect(summary.failed).toBe(1);
    expect(summary.failures?.[0]?.phase).toBe('authorization');
    expect(learningPlatform.rerunCalls).toHaveLength(0);
  });
});

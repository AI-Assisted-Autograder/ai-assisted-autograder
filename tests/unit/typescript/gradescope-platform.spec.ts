import { expect, test } from '@playwright/test';

import {
  GradescopePlatform,
} from '../../../integration/gradescope-platform';

import type { SubmissionLocator } from '../../../integration/interfaces';

const originalEnv = { ...process.env };

const submission: SubmissionLocator = {
  submissionId: 'submission-123',
  platformAssignmentId: 'assignment-456',
  platformSubmissionUrl:
    'https://www.gradescope.com/courses/123/assignments/456/submissions/789',
};

test.beforeEach(() => {
  process.env.GRADESCOPE_USERNAME = 'test@example.com';
  process.env.GRADESCOPE_PASSWORD = 'test-password';
  delete process.env.GRADESCOPE_ALLOW_RERUN;
  delete process.env.GRADESCOPE_RERUN_SUCCESS_TEXT;
});

test.afterEach(() => {
  process.env = { ...originalEnv };
});

test('authenticate rejects missing credentials', async ({ page }) => {
  delete process.env.GRADESCOPE_USERNAME;
  await page.route('**/account', (route) => route.fulfill({
    status: 302,
    headers: { location: '/login' },
  }));
  await page.route('**/login', (route) => route.fulfill({ body: '<h1>Log in</h1>' }));

  const platform = new GradescopePlatform(page);

  await expect(platform.authenticate()).rejects.toThrow(
    'Set GRADESCOPE_USERNAME and GRADESCOPE_PASSWORD before authenticating.',
  );
});

test('triggerRerun rejects when live reruns are not explicitly enabled', async ({
  page,
}) => {
  const platform = new GradescopePlatform(page);

  await expect(platform.triggerRerun(submission)).rejects.toThrow(
    'Rerun is locked.',
  );
});

test('triggerRerun rejects non-HTTPS and unapproved URLs', async ({ page }) => {
  process.env.GRADESCOPE_ALLOW_RERUN = '1';

  const platform = new GradescopePlatform(page);

  const invalidUrls = [
    'http://www.gradescope.com/courses/123',
    'https://example.com/courses/123',
    'https://www.gradescope.com.evil.example/courses/123',
    'https://gradescope.com/courses/123',
  ];

  for (const url of invalidUrls) {
    await expect(
      platform.triggerRerun({
        ...submission,
        platformSubmissionUrl: url,
      }),
    ).rejects.toThrow(
      'platformSubmissionUrl must be an https://www.gradescope.com/ URL.',
    );
  }
});

test('triggerRerun rejects a URL without a trailing slash after the hostname', async ({
  page,
}) => {
  process.env.GRADESCOPE_ALLOW_RERUN = '1';

  const platform = new GradescopePlatform(page);

  await expect(
    platform.triggerRerun({
      ...submission,
      platformSubmissionUrl: 'https://www.gradescope.com',
    }),
  ).rejects.toThrow(
    'platformSubmissionUrl must be an https://www.gradescope.com/ URL.',
  );
});

test('triggerRerun requires the live rerun flag before navigating', async ({
  page,
}) => {
  let navigated = false;

  page.on('request', (request) => {
    if (request.url().includes('/courses/123/')) {
      navigated = true;
    }
  });

  const platform = new GradescopePlatform(page);

  await expect(platform.triggerRerun(submission)).rejects.toThrow(
    'Rerun is locked.',
  );

  expect(navigated).toBe(false);
});

test('triggerRerun leaves an already running autograder untouched', async ({ page }) => {
  process.env.GRADESCOPE_ALLOW_RERUN = '1';
  await page.route(submission.platformSubmissionUrl, (route) => route.fulfill({
    contentType: 'text/html',
    body: `<div aria-label="submission rubric and results">
      <div class="submissionOutlineHeader--statusContainer">Running</div>
    </div><button onclick="document.body.dataset.clicked = 'yes'">Rerun Autograder</button>`,
  }));

  await expect(new GradescopePlatform(page).triggerRerun(submission)).rejects.toThrow(
    'Gradescope autograder is already running; no rerun was triggered.',
  );
  await expect(page.locator('body')).not.toHaveAttribute('data-clicked', 'yes');
});

test('triggerRerun opens More and ignores the Regrade Requests link', async ({ page }) => {
  process.env.GRADESCOPE_ALLOW_RERUN = '1';
  process.env.GRADESCOPE_RERUN_CONFIRMATION_TIMEOUT_MS = '500';
  await page.route(submission.platformSubmissionUrl, (route) => route.fulfill({
    contentType: 'text/html',
    body: `<a href="#requests" onclick="document.body.dataset.wrongControl = 'yes'">Regrade Requests</a>
      <div aria-label="submission rubric and results">
        <div class="submissionOutlineHeader--statusContainer">Graded</div>
      </div>
      <button onclick="document.getElementById('rerun').hidden = false">More</button>
      <button id="rerun" hidden onclick="document.querySelector('.submissionOutlineHeader--statusContainer').textContent = 'Running'">Rerun Autograder</button>`,
  }));

  const result = await new GradescopePlatform(page).triggerRerun(submission);

  expect(result.confirmationEvidence).toBe('status-text');
  await expect(page).toHaveURL(submission.platformSubmissionUrl);
  await expect(page.locator('body')).not.toHaveAttribute('data-wrong-control', 'yes');
});

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
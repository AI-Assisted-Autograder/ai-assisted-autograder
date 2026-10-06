import { test } from '@playwright/test';
import { GradescopePlatform } from '../../integration/gradescope-platform';

const submissionUrl = process.env.GRADESCOPE_SUBMISSION_URL;
const rerunAuthorized = process.env.GRADESCOPE_ALLOW_RERUN === '1';

test('reruns one explicitly selected Gradescope submission', async ({ page }) => {
  test.skip(
    !submissionUrl || !rerunAuthorized,
    'Set GRADESCOPE_SUBMISSION_URL and GRADESCOPE_ALLOW_RERUN=1 to authorize a live rerun.',
  );

  const platform = new GradescopePlatform(page);
  await platform.authenticate();
  await platform.triggerRerun({
    submissionId: process.env.GRADESCOPE_SUBMISSION_ID ?? 'manual-selection',
    platformAssignmentId: process.env.GRADESCOPE_ASSIGNMENT_ID ?? 'manual-selection',
    platformSubmissionUrl: submissionUrl!,
  });
});

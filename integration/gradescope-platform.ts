import { expect, Page } from '@playwright/test';
import type { ILearningPlatform, RerunResult, SubmissionLocator } from './interfaces';

export type { RerunResult, SubmissionLocator } from './interfaces';

/** UI selectors for Gradescope live in one place so a reskin has one repair point. */
export const selectorMap = {
  loginEmail: /email|username/i,
  loginPassword: /password/i,
  loginButton: /log in|sign in/i,
  rerunAutograder: /rerun autograder/i,
  confirmationDialog: '[role="dialog"]',
  confirmRerun: /confirm|rerun autograder/i,
  rerunSuccess: /autograder.*(queued|started|success)|rerun.*(queued|started|success)/i,
} as const;

export class GradescopePlatform implements ILearningPlatform {
  public constructor(private readonly page: Page) {}

  public async authenticate(): Promise<void> {
    const username = process.env.GRADESCOPE_USERNAME;
    const password = process.env.GRADESCOPE_PASSWORD;
    if (!username || !password) {
      throw new Error('Set GRADESCOPE_USERNAME and GRADESCOPE_PASSWORD before authenticating.');
    }

    await this.page.goto('/login');
    await this.page.getByLabel(selectorMap.loginEmail).first().fill(username);
    await this.page.getByLabel(selectorMap.loginPassword).first().fill(password);
    await this.page.getByRole('button', { name: selectorMap.loginButton }).click();
    await expect(this.page).toHaveURL(/\/account(?:[/?#]|$)/, { timeout: 30_000 });
  }

  public async triggerRerun(locator: SubmissionLocator): Promise<RerunResult> {
    if (process.env.GRADESCOPE_ALLOW_RERUN !== '1') {
      throw new Error(
        'Rerun is locked. Set GRADESCOPE_ALLOW_RERUN=1 only when you explicitly authorize a live rerun.',
      );
    }
    if (!locator.platformSubmissionUrl.startsWith('https://www.gradescope.com/')) {
      throw new Error('platformSubmissionUrl must be an https://www.gradescope.com/ URL.');
    }

    await this.page.goto(locator.platformSubmissionUrl);
    const rerunButton = this.page
      .locator('button, a')
      .filter({ hasText: selectorMap.rerunAutograder })
      .first();
    await expect(rerunButton).toBeVisible();
    await rerunButton.click();

    let confirmed = false;
    const dialog = this.page.locator(selectorMap.confirmationDialog);
    if (await dialog.isVisible().catch(() => false)) {
      const confirmButton = dialog
        .locator('button, a')
        .filter({ hasText: selectorMap.confirmRerun })
        .first();
      await expect(confirmButton).toBeVisible();
      await confirmButton.click();
      confirmed = true;
    }

    const configuredSuccessText = process.env.GRADESCOPE_RERUN_SUCCESS_TEXT;
    const successPattern = configuredSuccessText
      ? new RegExp(configuredSuccessText, 'i')
      : selectorMap.rerunSuccess;
    await expect(this.page.getByText(successPattern).first()).toBeVisible({ timeout: 15_000 });

    return {
      submissionId: locator.submissionId,
      clicked: true,
      confirmed,
      url: this.page.url(),
    };
  }
}

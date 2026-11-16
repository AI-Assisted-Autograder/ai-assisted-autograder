import { Dialog, expect, Page } from '@playwright/test';
import type { ILearningPlatform, RerunResult, SubmissionLocator } from './interfaces';

export type { RerunResult, SubmissionLocator } from './interfaces';

/** UI selectors for Gradescope live in one place so a reskin has one repair point. */
export const selectorMap = {
  loginEmail: /email|username/i,
  loginPassword: /password/i,
  loginButton: /log in|sign in/i,
  rerunAutograder: /rerun autograder|regrade/i,
  moreButton: /^more$/i,
  confirmationDialog: '[role="dialog"]',
  confirmRerun: /confirm|rerun autograder|regrade/i,
  rerunSuccess:
    /(?:autograder|grader|regrade|rerun).*(?:queued|queue|started|start|submitted|processing|running|success)|(?:queued|queue|started|start|submitted|processing|running|success).*(?:autograder|grader|regrade|rerun)/i,
} as const;

export type AutograderState = 'RUNNING' | 'COMPLETE' | 'FAILED' | 'UNKNOWN';

/** Classifies the visible submission-page state without treating a click as completion. */
export function classifyAutograderState(bodyText: string): AutograderState {
  if (/(?:autograder|grader).*(?:failed|error)/i.test(bodyText)) {
    return 'FAILED';
  }
  if (/autograder.*(?:hasn't finished running|still running|is running|queued|started)/i.test(bodyText)) {
    return 'RUNNING';
  }
  if (/autograder results/i.test(bodyText)) {
    return 'COMPLETE';
  }
  return 'UNKNOWN';
}

export class GradescopePlatform implements ILearningPlatform {
  public constructor(private readonly page: Page) {}

  public async authenticate(): Promise<void> {
    // Reuse a saved Playwright session when available. If it has expired,
    // navigating to /account redirects to /login and the credential fallback
    // below refreshes the session.
    await this.page.goto('/account');
    if (/\/account(?:[/?#]|$)/.test(this.page.url())) {
      return;
    }

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
    const visibleControls = this.page.locator(
      'button:visible, a:visible, [role="button"]:visible, [role="menuitem"]:visible',
    );
    let rerunButton = visibleControls
      .filter({ hasText: selectorMap.rerunAutograder })
      .first();
    if (!(await rerunButton.isVisible().catch(() => false))) {
      const moreButton = visibleControls
        .filter({ hasText: selectorMap.moreButton })
        .first();
      if (await moreButton.isVisible().catch(() => false)) {
        await moreButton.click();
        await this.page.waitForTimeout(250);
      }
    }
    rerunButton = visibleControls
      .filter({ hasText: selectorMap.rerunAutograder })
      .first();
    if (!(await rerunButton.isVisible().catch(() => false))) {
      const controls = (await visibleControls.allTextContents())
        .map((text) => text.trim())
        .filter(Boolean)
        .slice(-20)
        .join(' | ');
      throw new Error(
        'Gradescope rerun control was not found at ' + this.page.url() + '. ' +
        'Visible controls: ' + (controls || '(none)'),
      );
    }
    let nativeDialogAccepted = false;
    const nativeDialogHandler = async (dialog: Dialog) => {
      if (dialog.type() === 'confirm') {
        nativeDialogAccepted = true;
        await dialog.accept();
      } else {
        await dialog.dismiss();
      }
    };
    this.page.on('dialog', nativeDialogHandler);

    try {
      await rerunButton.click();
    } finally {
      this.page.off('dialog', nativeDialogHandler);
    }

    let confirmed = false;
    const dialog = this.page.locator(selectorMap.confirmationDialog);
    // The confirmation dialog can be rendered asynchronously after the first
    // click. Give it a short grace period before deciding that this is a
    // direct-action button.
    await dialog.waitFor({ state: 'visible', timeout: 1_500 }).catch(() => undefined);
    if (await dialog.isVisible().catch(() => false)) {
      const confirmButton = dialog
        .locator('button:visible, a:visible, [role="button"]:visible, [role="menuitem"]:visible')
        .filter({ hasText: selectorMap.confirmRerun })
        .first();
      await expect(confirmButton).toBeVisible();
      await confirmButton.click();
      confirmed = true;
    }

    const evidence = await this.waitForRerunAcceptance(
      dialog,
      confirmed,
      nativeDialogAccepted,
    );

    return {
      submissionId: locator.submissionId,
      clicked: true,
      confirmed,
      confirmationEvidence: evidence,
      url: this.page.url(),
    };
  }

  private async waitForRerunAcceptance(
    dialog: ReturnType<Page['locator']>,
    confirmed: boolean,
    nativeDialogAccepted: boolean,
  ): Promise<RerunResult['confirmationEvidence']> {
    const timeoutMs = this.readPositiveInteger(
      'GRADESCOPE_RERUN_CONFIRMATION_TIMEOUT_MS',
      10_000,
    );
    const configuredSuccessText = process.env.GRADESCOPE_RERUN_SUCCESS_TEXT;
    const successPattern = configuredSuccessText
      ? new RegExp(configuredSuccessText, 'i')
      : selectorMap.rerunSuccess;
    const deadline = Date.now() + timeoutMs;

    while (Date.now() <= deadline) {
      const bodyText = await this.page.locator('body').innerText().catch(() => '');
      if (successPattern.test(bodyText)) {
        return 'status-text';
      }

      // A confirmed dialog disappearing is the actual browser-level evidence
      // that Gradescope accepted the request. Do not require a particular
      // English toast, because that text varies across Gradescope pages.
      if ((confirmed || nativeDialogAccepted) && !(await dialog.isVisible().catch(() => false))) {
        return nativeDialogAccepted ? 'native-dialog' : 'confirmation-dialog';
      }

      await this.page.waitForTimeout(250);
    }

    // Some Gradescope pages use a direct action with no dialog or toast. The
    // click itself completed without navigation/error, so record it as a
    // submitted trigger and let waitForRerun verify the actual autograder run.
    if (!confirmed && !nativeDialogAccepted) {
      return 'direct-click';
    }

    throw new Error(
      `Gradescope did not confirm the rerun request within ${timeoutMs} ms at ${this.page.url()}.`,
    );
  }

  public async waitForRerun(locator: SubmissionLocator): Promise<void> {
    await this.page.goto(locator.platformSubmissionUrl);
    await this.waitForAutograderCompletion();
  }

  private async waitForAutograderCompletion(): Promise<void> {
    // Gradescope reruns the full autograder, including dependency setup,
    // functional tests, and the AI feedback hook. Five minutes was too short
    // for the real Exercise 4 batch, so the default is now 30 minutes while
    // remaining configurable for controlled tests.
    const timeoutMs = this.readPositiveInteger('GRADESCOPE_RERUN_TIMEOUT_MS', 1_800_000);
    const pollIntervalMs = this.readPositiveInteger('GRADESCOPE_RERUN_POLL_INTERVAL_MS', 5_000);
    const deadline = Date.now() + timeoutMs;

    while (Date.now() <= deadline) {
      const bodyText = await this.page.locator('body').innerText();
      const state = classifyAutograderState(bodyText);
      if (state === 'FAILED') {
        throw new Error('Gradescope autograder reported a failure while processing the rerun.');
      }
      if (state === 'COMPLETE') {
        return;
      }

      if (Date.now() + pollIntervalMs > deadline) {
        break;
      }
      await this.page.waitForTimeout(pollIntervalMs);
      await this.page.reload({ waitUntil: 'domcontentloaded' });
    }

    throw new Error(`Gradescope autograder did not finish within ${timeoutMs} ms.`);
  }

  private readPositiveInteger(name: string, fallback: number): number {
    const value = Number.parseInt(process.env[name] ?? '', 10);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  }
}

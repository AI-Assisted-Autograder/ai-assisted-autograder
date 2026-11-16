import { Dialog, expect, Page } from '@playwright/test';
import type { ILearningPlatform, RerunResult, SubmissionLocator } from './interfaces';

export type { RerunResult, SubmissionLocator } from './interfaces';

/** UI selectors for Gradescope live in one place so a reskin has one repair point. */
export const selectorMap = {
  loginEmail: /email|username/i,
  loginPassword: /password/i,
  loginButton: /log in|sign in/i,
  rerunAutograder: /^\s*(?:rerun autograder|regrade)\s*$/i,
  moreButton: /^more$/i,
  confirmationDialog: '[role="dialog"]',
  confirmRerun: /confirm|rerun autograder|regrade/i,
} as const;

export type AutograderState = 'RUNNING' | 'COMPLETE' | 'FAILED' | 'UNKNOWN';

/** Classifies Gradescope status text, excluding student tests and autograder output. */
export function classifyAutograderState(statusText: string): AutograderState {
  const lines = statusText.split(/\r?\n/).map((line) => line.trim());
  if (lines.some((line) => /^(?:failed|error|grading failed)$/i.test(line) ||
    /^(?:the )?autograder\b.*(?:failed|error)/i.test(line))) {
    return 'FAILED';
  }
  if (lines.some((line) =>
    /^(?:autograding|grading|running|queued|in queue|processing|in progress|grading in progress)$/i.test(line) ||
    /^(?:the )?autograder\b.*(?:hasn't finished running|still running|is running|queued|started)/i.test(line),
  )) {
    return 'RUNNING';
  }
  if (lines.some((line) => /^graded$/i.test(line))) {
    return 'COMPLETE';
  }
  return 'UNKNOWN';
}

type AutograderSnapshot = {
  state: AutograderState;
  statusText: string;
  runId?: string;
};

type RerunObservation = {
  baseline: AutograderSnapshot;
  sawNewExecution: boolean;
};

export class GradescopePlatform implements ILearningPlatform {
  private readonly rerunObservations = new Map<string, RerunObservation>();

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

  public async listAssignmentIds(): Promise<string[]> {
    const courseId = this.courseId();
    await this.page.goto(`/courses/${courseId}/assignments`);
    await expect(this.page).toHaveURL(new RegExp(`/courses/${courseId}/assignments/?(?:[?#].*)?$`));
    return this.readTableIds(`/courses/${courseId}/assignments`, false);
  }

  public async listActiveSubmissionIds(platformAssignmentId: string): Promise<string[]> {
    const courseId = this.courseId();
    if (!/^\d+$/.test(platformAssignmentId)) {
      throw new Error('Gradescope assignment IDs must contain only digits.');
    }
    const assignmentPath = `/courses/${courseId}/assignments/${platformAssignmentId}`;
    await this.page.goto(`${assignmentPath}/submissions`);
    await expect(this.page).toHaveURL(new RegExp(`${assignmentPath}/submissions/?(?:[?#].*)?$`));
    await expect(this.page.getByRole('heading', { name: 'Manage Submissions', exact: true })).toBeVisible();
    return this.readTableIds(`${assignmentPath}/submissions`, true);
  }

  private courseId(): string {
    const courseId = process.env.GRADESCOPE_COURSE_ID;
    if (!courseId || !/^\d+$/.test(courseId)) {
      throw new Error('Set GRADESCOPE_COURSE_ID to the numeric Gradescope course ID.');
    }
    return courseId;
  }

  private async readTableIds(pathPrefix: string, requireTotal: boolean): Promise<string[]> {
    const table = this.page.getByRole('table').filter({ visible: true });
    await expect(table).toHaveCount(1);
    const searches = this.page.locator('input[type="search"]:visible');
    for (let index = 0; index < await searches.count(); index += 1) {
      await searches.nth(index).fill('');
    }

    await expect(table).toBeVisible();
    const ids = new Set<string>();
    let expectedTotal: number | undefined;
    let rowsRead = 0;

    while (true) {
      const snapshot = await table.evaluate((element, prefix) => {
        const rows = Array.from(element.querySelectorAll('tbody tr'))
          .filter((row) => row.querySelector('td'));
        return {
          rows: rows.map((row) => ({
            text: (row.textContent ?? '').trim(),
            ids: Array.from(new Set(Array.from(row.querySelectorAll('a[href]'))
              .map((link) => new URL((link as HTMLAnchorElement).href).pathname)
              .filter((path) => path.startsWith(`${prefix}/`))
              .map((path) => path.slice(prefix.length + 1))
              .filter((id) => /^\d+\/?$/.test(id))
              .map((id) => id.replace(/\/$/, '')))),
          })),
        };
      }, pathPrefix);
      const bodyText = await this.page.locator('body').innerText();
      const info = bodyText.match(/Showing\s+([\d,]+)\s+to\s+([\d,]+)\s+of\s+([\d,]+)\s+entries(?:\s*\(([^)]*)\))?/i);
      if (info?.[4]) {
        throw new Error('Gradescope listing is still filtered after clearing its search.');
      }
      const empty = snapshot.rows.length === 1 &&
        /^no (?:data available in (?:the )?table|matching records found|assignments(?: yet)?|submissions(?: yet)?)[.!]?$/i
          .test(snapshot.rows[0].text);
      const rows = snapshot.rows.filter((row) => row.ids.length > 0);
      if (snapshot.rows.some((row) => row.ids.length !== 1 && !(empty && row.ids.length === 0))) {
        throw new Error(`Gradescope listing contains an unrecognized row at ${this.page.url()}.`);
      }
      if (rows.length === 0 && !empty) {
        throw new Error(`Gradescope listing did not explicitly report an empty table at ${this.page.url()}.`);
      }

      if (info) {
        const [start, end, total] = info.slice(1, 4).map((value) => Number(value.replace(/,/g, '')));
        if ((expectedTotal !== undefined && total !== expectedTotal) ||
          start !== (total === 0 ? 0 : rowsRead + 1) ||
          end - start + (total === 0 ? 0 : 1) !== rows.length || end > total) {
          throw new Error('Gradescope listing pagination is incomplete or changed during collection.');
        }
        expectedTotal = total;
      } else if (requireTotal && !empty) {
        throw new Error('Gradescope submission listing has no readable total.');
      }

      rows.forEach((row) => ids.add(row.ids[0]));
      rowsRead += rows.length;
      const next = this.page.locator('a:visible, button:visible')
        .filter({ hasText: /^Next$/i });
      const canAdvance = await next.count() === 1 && !(await next.evaluate((element) =>
        element.matches(':disabled, [aria-disabled="true"], .disabled') ||
        !!element.closest('[aria-disabled="true"], .disabled'),
      ));
      if (!canAdvance) {
        if (expectedTotal !== undefined && rowsRead !== expectedTotal) {
          throw new Error('Gradescope listing ended before its displayed total was collected.');
        }
        return [...ids];
      }
      if (!info || rowsRead >= (expectedTotal ?? 0)) {
        throw new Error('Gradescope listing pagination cannot be verified.');
      }
      await next.click();
      await expect.poll(async () => (await this.page.locator('body').innerText())
        .match(/Showing\s+[\d,]+\s+to\s+[\d,]+\s+of\s+[\d,]+\s+entries/i)?.[0])
        .not.toBe(info[0]);
    }
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

    const configuredSuccessText = process.env.GRADESCOPE_RERUN_SUCCESS_TEXT;
    const successPattern = configuredSuccessText ? new RegExp(configuredSuccessText, 'i') : undefined;
    await this.page.goto(locator.platformSubmissionUrl);
    this.rerunObservations.delete(locator.submissionId);
    let baseline!: AutograderSnapshot;
    await expect.poll(async () => {
      baseline = await this.readAutograderSnapshot(locator);
      return baseline.state;
    }, {
      timeout: this.readPositiveInteger('GRADESCOPE_RERUN_CONFIRMATION_TIMEOUT_MS', 10_000),
      message: 'Gradescope status must be readable before clicking Rerun Autograder.',
    }).not.toBe('UNKNOWN');
    if (baseline.state === 'RUNNING') {
      throw new Error('Gradescope autograder is already running; no rerun was triggered.');
    }
    const observation: RerunObservation = { baseline, sawNewExecution: false };
    const visibleControls = this.page.locator(
      'button:visible, a:visible, [role="button"]:visible, [role="menuitem"]:visible',
    );
    let rerunButton = visibleControls
      .filter({ hasText: selectorMap.rerunAutograder })
      .first();
    if (!(await rerunButton.isVisible().catch(() => false))) {
      const moreButton = this.page
        .getByRole('button', { name: selectorMap.moreButton })
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
    const nativeDialogHandler = async (dialog: Dialog) => {
      if (dialog.type() === 'confirm') {
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

    const evidence = await this.waitForRerunAcceptance(locator, observation, successPattern);
    this.rerunObservations.set(locator.submissionId, observation);

    return {
      submissionId: locator.submissionId,
      clicked: true,
      confirmed,
      confirmationEvidence: evidence,
      url: this.page.url(),
    };
  }

  private async waitForRerunAcceptance(
    locator: SubmissionLocator,
    observation: RerunObservation,
    successPattern?: RegExp,
  ): Promise<RerunResult['confirmationEvidence']> {
    const timeoutMs = this.readPositiveInteger(
      'GRADESCOPE_RERUN_CONFIRMATION_TIMEOUT_MS',
      10_000,
    );
    const deadline = Date.now() + timeoutMs;

    while (Date.now() <= deadline) {
      const snapshot = await this.readAutograderSnapshot(locator);
      if (observation.baseline.runId && snapshot.runId &&
        snapshot.runId !== observation.baseline.runId) {
        observation.sawNewExecution = true;
        return 'run-id';
      }
      if ((observation.baseline.state === 'COMPLETE' || observation.baseline.state === 'FAILED') &&
        snapshot.state === 'RUNNING') {
        observation.sawNewExecution = true;
        return 'status-text';
      }
      // Custom confirmation text must newly appear in Gradescope's own status.
      if ((observation.baseline.state === 'COMPLETE' || observation.baseline.state === 'FAILED') &&
        successPattern && snapshot.statusText &&
        !successPattern.test(observation.baseline.statusText) && successPattern.test(snapshot.statusText)) {
        observation.sawNewExecution = true;
        return 'status-text';
      }
      await this.page.waitForTimeout(250);
    }

    throw new Error(
      `Gradescope did not confirm the rerun request within ${timeoutMs} ms at ${this.page.url()}.`,
    );
  }

  public async waitForRerun(locator: SubmissionLocator): Promise<void> {
    const observation = this.rerunObservations.get(locator.submissionId);
    if (!observation?.sawNewExecution) {
      throw new Error('Cannot wait for a rerun without evidence that a new execution was accepted.');
    }
    await this.page.goto(locator.platformSubmissionUrl);
    await this.waitForAutograderCompletion(locator, observation);
  }

  private async waitForAutograderCompletion(
    locator: SubmissionLocator,
    observation: RerunObservation,
  ): Promise<void> {
    // Gradescope reruns the full autograder, including dependency setup,
    // functional tests, and the AI feedback hook. Five minutes was too short
    // for the real Exercise 4 batch, so the default is now 30 minutes while
    // remaining configurable for controlled tests.
    const timeoutMs = this.readPositiveInteger('GRADESCOPE_RERUN_TIMEOUT_MS', 1_800_000);
    const pollIntervalMs = this.readPositiveInteger('GRADESCOPE_RERUN_POLL_INTERVAL_MS', 5_000);
    const deadline = Date.now() + timeoutMs;

    while (Date.now() <= deadline) {
      const { state } = await this.readAutograderSnapshot(locator);
      if (state === 'FAILED' && observation.sawNewExecution) {
        throw new Error('Gradescope autograder reported a failure while processing the rerun.');
      }
      if (state === 'COMPLETE' && observation.sawNewExecution) {
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

  private async readAutograderSnapshot(locator: SubmissionLocator): Promise<AutograderSnapshot> {
    const status = this.page.locator(
      '[aria-label="submission rubric and results"] .submissionOutlineHeader--statusContainer',
    );
    const statusText = (await status.innerText({ timeout: 500 }).catch(() => '')).trim();
    const output = this.page.locator('#view-switcher-tabpanel-results .testCase')
      .filter({ has: this.page.locator('.testCase--header')
        .filter({ hasText: /^Autograder Output \(hidden from students\)$/ }) })
      .locator('.testCase--body');
    const outputText = await output.count() === 1
      ? await output.innerText({ timeout: 500 }).catch(() => '')
      : '';
    const platformSubmissionId = new URL(locator.platformSubmissionUrl).pathname
      .match(/\/submissions\/(\d+)\/?$/)?.[1];
    let runId: string | undefined;
    for (const match of outputText.matchAll(/Instructor AI run:\s*(\{[^\r\n]*?\})/g)) {
      try {
        const context = JSON.parse(match[1]) as { submission_id?: unknown; run_id?: unknown };
        if (String(context.submission_id) === platformSubmissionId &&
          typeof context.run_id === 'string' &&
          /^(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i.test(context.run_id)) {
          runId = context.run_id;
        }
      } catch {
        // A partial output line is not evidence of a new execution.
      }
    }
    return { state: classifyAutograderState(statusText), statusText, runId };
  }

  private readPositiveInteger(name: string, fallback: number): number {
    const value = Number.parseInt(process.env[name] ?? '', 10);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  }
}

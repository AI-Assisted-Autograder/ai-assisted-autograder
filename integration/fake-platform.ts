import type { ILearningPlatform, RerunResult, SubmissionLocator } from './interfaces';

/** Test double for the browser boundary. It never opens a browser. */
export class FakePlatform implements ILearningPlatform {
  public readonly rerunCalls: SubmissionLocator[] = [];
  public authenticated = false;

  public constructor(private readonly failingSubmissionIds = new Set<string>()) {}

  public async authenticate(): Promise<void> {
    this.authenticated = true;
  }

  public async triggerRerun(locator: SubmissionLocator): Promise<RerunResult> {
    this.rerunCalls.push(locator);
    if (this.failingSubmissionIds.has(locator.submissionId)) {
      throw new Error(`fake rerun failure for ${locator.submissionId}`);
    }
    return {
      submissionId: locator.submissionId,
      clicked: true,
      confirmed: true,
      url: locator.platformSubmissionUrl,
    };
  }

  public async waitForRerun(locator: SubmissionLocator): Promise<void> {
    if (this.failingSubmissionIds.has(locator.submissionId)) {
      throw new Error(`fake rerun failure for ${locator.submissionId}`);
    }
  }
}

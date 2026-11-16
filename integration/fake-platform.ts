import type { ILearningPlatform, RerunResult, SubmissionLocator } from './interfaces';

/** Test double for the browser boundary. It never opens a browser. */
export class FakePlatform implements ILearningPlatform {
  public readonly rerunCalls: SubmissionLocator[] = [];
  public readonly waitForRerunCalls: SubmissionLocator[] = [];
  public authenticated = false;

  public constructor(
    private readonly submissions: SubmissionLocator[],
    private readonly failingSubmissionIds = new Set<string>(),
  ) {}

  public async authenticate(): Promise<void> {
    this.authenticated = true;
  }

  public async listAssignmentIds(): Promise<string[]> {
    return [...new Set(this.submissions.map((row) => row.platformAssignmentId))];
  }

  public async listActiveSubmissionIds(platformAssignmentId: string): Promise<string[]> {
    return this.submissions
      .filter((row) => row.platformAssignmentId === platformAssignmentId)
      .map((row) => new URL(row.platformSubmissionUrl).pathname.split('/').pop()!);
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
      confirmationEvidence: 'status-text',
      url: locator.platformSubmissionUrl,
    };
  }

  public async waitForRerun(locator: SubmissionLocator): Promise<void> {
    this.waitForRerunCalls.push(locator);
    if (this.failingSubmissionIds.has(locator.submissionId)) {
      throw new Error(`fake rerun failure for ${locator.submissionId}`);
    }
  }
}

import { GradescopePlatform, SubmissionLocator } from './gradescope-platform';

export interface PendingSubmissionRepository {
  startRun(): Promise<void>;
  listSubmissionsPendingAi(mode: 'NIGHTLY'): Promise<SubmissionLocator[]>;
  authorizeForGrading(submissionId: string): Promise<void>;
  finishRun(summary: TriggerRunSummary): Promise<void>;
}

export type TriggerRunSummary = {
  found: number;
  authorized: number;
  rerun: number;
  failed: number;
};

/** Coordinates the service authorization and browser action. It never calls an LLM. */
export class NightlyTrigger {
  public constructor(
    private readonly repository: PendingSubmissionRepository,
    private readonly platform: GradescopePlatform,
  ) {}

  public async run(): Promise<TriggerRunSummary> {
    const summary: TriggerRunSummary = { found: 0, authorized: 0, rerun: 0, failed: 0 };
    await this.repository.startRun();

    try {
      await this.platform.authenticate();
      const pending = await this.repository.listSubmissionsPendingAi('NIGHTLY');
      summary.found = pending.length;

      for (const submission of pending) {
        try {
          // Authorization must happen before the browser click to avoid a DEFER race.
          await this.repository.authorizeForGrading(submission.submissionId);
          summary.authorized += 1;
          await this.platform.triggerRerun(submission);
          summary.rerun += 1;
        } catch {
          summary.failed += 1;
        }
      }
    } finally {
      await this.repository.finishRun(summary);
    }

    return summary;
  }
}

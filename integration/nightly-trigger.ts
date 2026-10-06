import { GradescopePlatform, RerunResult, SubmissionLocator } from './gradescope-platform';

export interface PendingSubmissionRepository {
  listSubmissionsPendingAi(mode: 'NIGHTLY'): Promise<SubmissionLocator[]>;
  authorizeForGrading(submissionId: string): Promise<void>;
  recordRerun(submissionId: string, result: RerunResult): Promise<void>;
  recordRerunFailure(submissionId: string, error: string): Promise<void>;
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
    const pending = await this.repository.listSubmissionsPendingAi('NIGHTLY');
    const summary: TriggerRunSummary = { found: pending.length, authorized: 0, rerun: 0, failed: 0 };

    for (const submission of pending) {
      try {
        // Authorization must happen before the browser click to avoid a DEFER race.
        await this.repository.authorizeForGrading(submission.submissionId);
        summary.authorized += 1;

        const result = await this.platform.triggerRerun(submission);
        await this.repository.recordRerun(submission.submissionId, result);
        summary.rerun += 1;
      } catch (error) {
        summary.failed += 1;
        await this.repository.recordRerunFailure(
          submission.submissionId,
          error instanceof Error ? error.message : String(error),
        );
      }
    }

    return summary;
  }
}

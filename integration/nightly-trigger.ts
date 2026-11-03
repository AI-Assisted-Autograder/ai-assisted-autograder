import type { IGradingClient, ILearningPlatform, SubmissionLocator, TriggerRunSummary } from './interfaces';

export type { TriggerRunSummary } from './interfaces';

/** Coordinates the service authorization and browser action. It never calls an LLM. */
export class NightlyTrigger {
  public constructor(
    private readonly client: IGradingClient,
    private readonly platform: ILearningPlatform,
  ) {}

  public async run(): Promise<TriggerRunSummary> {
    const summary: TriggerRunSummary = { found: 0, authorized: 0, rerun: 0, failed: 0 };
    await this.client.startRun();
    let runError: unknown;
    const submitted: SubmissionLocator[] = [];

    try {
      await this.platform.authenticate();
      const pending: SubmissionLocator[] = await this.client.listSubmissionsPendingAi('NIGHTLY');
      summary.found = pending.length;

      for (const submission of pending) {
        try {
          // Authorization must happen before the browser click to avoid a DEFER race.
          await this.client.authorizeForGrading(submission.submissionId);
          summary.authorized += 1;
          await this.client.recordSubmissionResult(submission.submissionId, 'AUTHORIZED');
          await this.platform.triggerRerun(submission);
          submitted.push(submission);
          summary.rerun += 1;
          await this.client.recordSubmissionResult(submission.submissionId, 'RERUN_SUBMITTED');
        } catch (error) {
          summary.failed += 1;
          const message = error instanceof Error ? error.message : String(error);
          try {
            await this.client.recordSubmissionResult(
              submission.submissionId,
              'RERUN_FAILED',
              message,
            );
          } catch (recordError) {
            console.error('Could not record submission failure:', recordError);
          }
        }
      }

      // Submit every accepted rerun before waiting for any one autograder.
      for (const submission of submitted) {
        try {
          await this.platform.waitForRerun(submission);
          await this.client.recordSubmissionResult(submission.submissionId, 'RERUN_SUCCEEDED');
        } catch (error) {
          summary.failed += 1;
          const message = error instanceof Error ? error.message : String(error);
          try {
            await this.client.recordSubmissionResult(
              submission.submissionId,
              'RERUN_FAILED',
              message,
            );
          } catch (recordError) {
            console.error('Could not record submission failure:', recordError);
          }
        }
      }
    } catch (error) {
      runError = error;
      summary.fatalError = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      try {
        await this.client.finishRun(summary);
      } catch (finishError) {
        if (runError === undefined) {
          throw finishError;
        }
        console.error('Could not finish the nightly run audit:', finishError);
      }
    }

    return summary;
  }
}

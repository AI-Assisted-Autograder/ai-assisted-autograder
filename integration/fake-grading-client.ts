import type {
  IGradingClient,
  SubmissionLocator,
  TriggerRunSummary,
} from './interfaces';

/** In-memory test double for the database/API boundary. */
export class FakeGradingClient implements IGradingClient {
  public readonly authorizedSubmissionIds: string[] = [];
  public readonly recordedResults: Array<{
    submissionId: string;
    status: 'AUTHORIZED' | 'RERUN_SUBMITTED' | 'RERUN_SUCCEEDED' | 'RERUN_FAILED';
    errorMessage?: string;
  }> = [];
  public finishedSummary: TriggerRunSummary | undefined;
  public started = false;

  public constructor(private readonly pending: SubmissionLocator[]) {}

  public async startRun(): Promise<void> {
    this.started = true;
  }

  public async listSubmissionsPendingAi(): Promise<SubmissionLocator[]> {
    return this.pending;
  }

  public async authorizeForGrading(submissionId: string): Promise<void> {
    this.authorizedSubmissionIds.push(submissionId);
  }

  public async recordSubmissionResult(
    submissionId: string,
    status: 'AUTHORIZED' | 'RERUN_SUBMITTED' | 'RERUN_SUCCEEDED' | 'RERUN_FAILED',
    errorMessage?: string,
  ): Promise<void> {
    this.recordedResults.push({ submissionId, status, errorMessage });
  }

  public async finishRun(summary: TriggerRunSummary): Promise<void> {
    this.finishedSummary = summary;
  }
}

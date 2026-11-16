import type {
  AssignmentLocator,
  IGradingClient,
  SubmissionLocator,
  TriggerSource,
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

  public constructor(
    private readonly pending: SubmissionLocator[],
    private readonly failRecordingStatus?: 'AUTHORIZED' | 'RERUN_SUBMITTED' | 'RERUN_SUCCEEDED' | 'RERUN_FAILED',
    private readonly failedAi: SubmissionLocator[] = pending,
    private readonly failedAiAfterRerun: SubmissionLocator[] = [],
  ) {}

  public async startRun(): Promise<void> {
    this.started = true;
  }

  public async listAssignments(): Promise<AssignmentLocator[]> {
    return [...new Set([...this.pending, ...this.failedAi].map((row) => row.platformAssignmentId))]
      .map((id) => ({ assignmentId: `db-${id}`, platformAssignmentId: id }));
  }

  public async syncActiveSubmissions(_assignmentId: string, _platformSubmissionIds: string[]): Promise<void> {}

  public async listSubmissionsPendingAi(_mode: 'NIGHTLY', retryFailed = false): Promise<SubmissionLocator[]> {
    return retryFailed ? [] : this.pending;
  }

  public async listSubmissionsFailedOnAi(): Promise<SubmissionLocator[]> {
    return this.failedAi;
  }

  public async hasAiFailure(submissionId: string): Promise<boolean> {
    return this.failedAiAfterRerun.some((submission) => submission.submissionId === submissionId);
  }

  public async authorizeForGrading(submissionId: string, _source: TriggerSource): Promise<void> {
    this.authorizedSubmissionIds.push(submissionId);
  }

  public async recordSubmissionResult(
    submissionId: string,
    status: 'AUTHORIZED' | 'RERUN_SUBMITTED' | 'RERUN_SUCCEEDED' | 'RERUN_FAILED',
    errorMessage?: string,
  ): Promise<void> {
    if (status === this.failRecordingStatus) {
      throw new Error(`fake audit failure for ${status}`);
    }
    this.recordedResults.push({ submissionId, status, errorMessage });
  }

  public async finishRun(summary: TriggerRunSummary): Promise<void> {
    this.finishedSummary = summary;
  }
}

export type SubmissionLocator = {
  submissionId: string;
  platformAssignmentId: string;
  platformSubmissionUrl: string;
};

export type RerunResult = {
  submissionId: string;
  clicked: boolean;
  confirmed: boolean;
  url: string;
};

export interface ILearningPlatform {
  authenticate(): Promise<void>;
  triggerRerun(locator: SubmissionLocator): Promise<RerunResult>;
  waitForRerun(locator: SubmissionLocator): Promise<void>;
}

export type TriggerRunSummary = {
  found: number;
  authorized: number;
  rerun: number;
  failed: number;
  fatalError?: string;
};

export interface IGradingClient {
  startRun(): Promise<void>;
  listSubmissionsPendingAi(mode: 'NIGHTLY'): Promise<SubmissionLocator[]>;
  authorizeForGrading(submissionId: string): Promise<void>;
  recordSubmissionResult(
    submissionId: string,
    status: 'AUTHORIZED' | 'RERUN_SUBMITTED' | 'RERUN_SUCCEEDED' | 'RERUN_FAILED',
    errorMessage?: string,
  ): Promise<void>;
  finishRun(summary: TriggerRunSummary): Promise<void>;
}

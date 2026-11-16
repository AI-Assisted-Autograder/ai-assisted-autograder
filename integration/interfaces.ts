export type AssignmentLocator = {
  assignmentId: string;
  platformAssignmentId: string;
};

export type SubmissionLocator = {
  submissionId: string;
  platformAssignmentId: string;
  platformSubmissionUrl: string;
};

export type TriggerSource = 'PENDING_NIGHTLY' | 'AI_FAILURES';

export type TriggerOptions = {
  waitForCompletion?: boolean;
};

export type RerunResult = {
  submissionId: string;
  clicked: boolean;
  confirmed: boolean;
  confirmationEvidence: 'status-text' | 'run-id';
  url: string;
};

export type FailureKind =
  | 'AUTHENTICATION'
  | 'DATABASE'
  | 'AI_FEEDBACK'
  | 'AUTOGRADER'
  | 'TIMEOUT'
  | 'RERUN_TRIGGER'
  | 'UNKNOWN';

export type SubmissionFailure = {
  submissionId: string;
  phase: 'authorization' | 'trigger' | 'completion';
  kind: FailureKind;
  message: string;
};

export type SubmissionOutcomeStatus =
  | 'AUTHORIZED'
  | 'RERUN_SUBMITTED'
  | 'RERUN_SUCCEEDED'
  | 'AUTHORIZATION_FAILED'
  | 'RERUN_TRIGGER_FAILED'
  | 'RERUN_COMPLETION_FAILED';

export type SubmissionOutcome = {
  submissionId: string;
  status: SubmissionOutcomeStatus;
  kind?: FailureKind;
  message?: string;
  confirmationEvidence?: RerunResult['confirmationEvidence'];
};

export interface ILearningPlatform {
  authenticate(): Promise<void>;
  listAssignmentIds(): Promise<string[]>;
  listActiveSubmissionIds(platformAssignmentId: string): Promise<string[]>;
  triggerRerun(locator: SubmissionLocator): Promise<RerunResult>;
  waitForRerun(locator: SubmissionLocator): Promise<void>;
}

export type TriggerRunSummary = {
  found: number;
  authorized: number;
  rerun: number;
  failed: number;
  fatalError?: string;
  failures?: SubmissionFailure[];
  outcomes?: SubmissionOutcome[];
  auditErrors?: string[];
  syncErrors?: Array<{ assignmentId: string; message: string }>;
};

export interface IGradingClient {
  startRun(): Promise<void>;
  listAssignments(): Promise<AssignmentLocator[]>;
  syncActiveSubmissions(assignmentId: string, platformSubmissionIds: string[]): Promise<void>;
  listSubmissionsPendingAi(mode: 'NIGHTLY', retryFailed?: boolean): Promise<SubmissionLocator[]>;
  listSubmissionsFailedOnAi(retryEligible?: boolean): Promise<SubmissionLocator[]>;
  hasAiFailure(submissionId: string): Promise<boolean>;
  authorizeForGrading(submissionId: string, source: TriggerSource): Promise<void>;
  recordSubmissionResult(
    submissionId: string,
    status: 'AUTHORIZED' | 'RERUN_SUBMITTED' | 'RERUN_SUCCEEDED' | 'RERUN_FAILED',
    errorMessage?: string,
  ): Promise<void>;
  finishRun(summary: TriggerRunSummary): Promise<void>;
}

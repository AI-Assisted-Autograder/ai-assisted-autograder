import type {
  IGradingClient,
  ILearningPlatform,
  SubmissionFailure,
  SubmissionLocator,
  SubmissionOutcome,
  SubmissionOutcomeStatus,
  TriggerOptions,
  TriggerSource,
  TriggerRunSummary,
} from './interfaces';
import { classifyFailure } from './failure-classification';

export type { TriggerRunSummary } from './interfaces';

/** Coordinates the service authorization and browser action. It never calls an LLM. */
export class NightlyTrigger {
  public constructor(
    private readonly client: IGradingClient,
    private readonly platform: ILearningPlatform,
  ) {}

  public async run(
    source: TriggerSource = 'PENDING_NIGHTLY',
    options: TriggerOptions = {},
  ): Promise<TriggerRunSummary> {
    const summary: TriggerRunSummary = { found: 0, authorized: 0, rerun: 0, failed: 0 };
    await this.client.startRun();
    let runError: unknown;
    const submitted: SubmissionLocator[] = [];

    try {
      await this.platform.authenticate();
      const courseAssignmentIds = new Set(await this.platform.listAssignmentIds());
      const assignments = (await this.client.listAssignments()).filter((assignment) =>
        courseAssignmentIds.has(assignment.platformAssignmentId),
      );
      const managedAssignmentIds = new Set<string>();
      for (const assignment of assignments) {
        try {
          const submissionIds = await this.platform.listActiveSubmissionIds(assignment.platformAssignmentId);
          await this.client.syncActiveSubmissions(assignment.assignmentId, submissionIds);
          managedAssignmentIds.add(assignment.platformAssignmentId);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          (summary.syncErrors ??= []).push({ assignmentId: assignment.assignmentId, message });
          console.error(`Could not synchronize assignment ${assignment.assignmentId}:`, message);
        }
      }
      const candidates = new Map<string, { submission: SubmissionLocator; source: TriggerSource }>();
      const submissions =
        source === 'AI_FAILURES'
          ? await this.client.listSubmissionsFailedOnAi(true)
          : await this.client.listSubmissionsPendingAi('NIGHTLY');
      for (const submission of submissions) {
        candidates.set(submission.submissionId, { submission, source });
      }
      if (source === 'AI_FAILURES') {
        // Recover failed nightly attempts without authorizing new submissions early.
        for (const submission of await this.client.listSubmissionsPendingAi('NIGHTLY', true)) {
          if (!candidates.has(submission.submissionId)) {
            candidates.set(submission.submissionId, { submission, source: 'PENDING_NIGHTLY' });
          }
        }
      }
      const pending = [...candidates.values()].filter(({ submission }) =>
        managedAssignmentIds.has(submission.platformAssignmentId),
      );
      summary.found = pending.length;

      for (const { submission, source: authorizationSource } of pending) {
        try {
          // Authorization must happen before the browser click to avoid a DEFER race.
          await this.client.authorizeForGrading(submission.submissionId, authorizationSource);
          summary.authorized += 1;
          await this.client.recordSubmissionResult(submission.submissionId, 'AUTHORIZED');
          this.recordOutcome(summary, submission.submissionId, 'AUTHORIZED');
        } catch (error) {
          await this.recordFailure(summary, submission, 'authorization', error);
          continue;
        }

        try {
          const rerunResult = await this.platform.triggerRerun(submission);
          submitted.push(submission);
          summary.rerun += 1;
          this.recordOutcome(
            summary,
            submission.submissionId,
            'RERUN_SUBMITTED',
            undefined,
            undefined,
            rerunResult.confirmationEvidence,
          );
        } catch (error) {
          await this.recordFailure(summary, submission, 'trigger', error);
          continue;
        }

        // Audit failure must not turn a confirmed Gradescope click into a
        // false rerun failure. The platform action already happened.
        try {
          await this.client.recordSubmissionResult(submission.submissionId, 'RERUN_SUBMITTED');
        } catch (error) {
          this.recordAuditError(summary, submission, error);
        }
      }

      if (options.waitForCompletion !== false) {
        // Submit every accepted rerun before waiting for any one autograder.
        for (const submission of submitted) {
          try {
            await this.platform.waitForRerun(submission);
            if (await this.client.hasAiFailure(submission.submissionId)) {
              throw new Error(
                `Backend still reports an AI feedback failure for ${submission.submissionId}.`,
              );
            }
            await this.client.recordSubmissionResult(submission.submissionId, 'RERUN_SUCCEEDED');
            this.recordOutcome(summary, submission.submissionId, 'RERUN_SUCCEEDED');
          } catch (error) {
            await this.recordFailure(summary, submission, 'completion', error);
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

  private async recordFailure(
    summary: TriggerRunSummary,
    submission: SubmissionLocator,
    phase: 'authorization' | 'trigger' | 'completion',
    error: unknown,
  ): Promise<void> {
    summary.failed += 1;
    const message = error instanceof Error ? error.message : String(error);
    (summary.failures ??= []).push({
      submissionId: submission.submissionId,
      phase,
      kind: classifyFailure(message),
      message,
    });
    const status: SubmissionOutcomeStatus =
      phase === 'authorization'
        ? 'AUTHORIZATION_FAILED'
        : phase === 'trigger'
          ? 'RERUN_TRIGGER_FAILED'
          : 'RERUN_COMPLETION_FAILED';
    this.recordOutcome(summary, submission.submissionId, status, classifyFailure(message), message);
    try {
      await this.client.recordSubmissionResult(
        submission.submissionId,
        'RERUN_FAILED',
        message,
      );
    } catch (recordError) {
      this.recordAuditError(summary, submission, recordError);
    }
  }

  private recordOutcome(
    summary: TriggerRunSummary,
    submissionId: string,
    status: SubmissionOutcomeStatus,
    kind?: SubmissionFailure['kind'],
    message?: string,
    confirmationEvidence?: SubmissionOutcome['confirmationEvidence'],
  ): void {
    const outcome = { submissionId, status } as {
      submissionId: string;
      status: SubmissionOutcomeStatus;
      kind?: SubmissionFailure['kind'];
      message?: string;
      confirmationEvidence?: SubmissionOutcome['confirmationEvidence'];
    };
    if (kind !== undefined) outcome.kind = kind;
    if (message !== undefined) outcome.message = message;
    if (confirmationEvidence !== undefined) outcome.confirmationEvidence = confirmationEvidence;
    (summary.outcomes ??= []).push(outcome);
  }

  private recordAuditError(
    summary: TriggerRunSummary,
    submission: SubmissionLocator,
    error: unknown,
  ): void {
    const message = error instanceof Error ? error.message : String(error);
    (summary.auditErrors ??= []).push(`${submission.submissionId}: ${message}`);
    console.error(`Could not record nightly audit for ${submission.submissionId}:`, message);
  }
}

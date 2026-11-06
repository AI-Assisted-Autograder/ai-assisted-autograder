import type { IGradingClient, SubmissionLocator, TriggerRunSummary } from './interfaces';

type PendingSubmissionDto = {
  submission_id: string;
  platform: string;
  platform_assignment_id: string;
  platform_submission_id: string;
  student_identifier: string | null;
};

/** HTTP bridge from the Playwright trigger to the existing SQL repository API. */
export class HttpGradingClient implements IGradingClient {
  private readonly baseUrl = (process.env.GRADING_API_URL ?? 'http://localhost:8000').replace(/\/$/, '');
  private readonly token = process.env.INTEGRATION_API_TOKEN;
  private runId: string | undefined;

  private headers(): HeadersInit {
    if (!this.token) {
      throw new Error('Set INTEGRATION_API_TOKEN before using the grading database API.');
    }
    return { 'X-Internal-Token': this.token };
  }

  public async startRun(): Promise<void> {
    const response = await fetch(`${this.baseUrl}/internal/nightly/runs`, {
      method: 'POST',
      headers: this.headers(),
    });
    if (!response.ok) {
      throw new Error(`Database API returned ${response.status} while starting the nightly run.`);
    }
    const body = (await response.json()) as { run_id: string };
    this.runId = body.run_id;
  }

  public async listSubmissionsPendingAi(mode: 'NIGHTLY'): Promise<SubmissionLocator[]> {
    if (mode !== 'NIGHTLY') {
      throw new Error('The nightly trigger only supports NIGHTLY submissions.');
    }
    const response = await fetch(`${this.baseUrl}/internal/nightly/submissions?mode=NIGHTLY`, {
      headers: this.headers(),
    });
    if (!response.ok) {
      throw new Error(`Database API returned ${response.status} while listing nightly submissions.`);
    }

    const rows = (await response.json()) as PendingSubmissionDto[];
    return this.toSubmissionLocators(rows);
  }

  public async listSubmissionsFailedOnAi(): Promise<SubmissionLocator[]> {
    const response = await fetch(`${this.baseUrl}/internal/ai-failures/submissions`, {
      headers: this.headers(),
    });
    if (!response.ok) {
      throw new Error(`Database API returned ${response.status} while listing AI-failed submissions.`);
    }

    const rows = (await response.json()) as PendingSubmissionDto[] | PendingSubmissionDto;
    return this.toSubmissionLocators(rows);
  }

  private toSubmissionLocators(
    response: PendingSubmissionDto[] | PendingSubmissionDto,
  ): SubmissionLocator[] {
    const rows = Array.isArray(response) ? response : [response];
    const courseId = process.env.GRADESCOPE_COURSE_ID;
    if (!courseId && rows.length > 0) {
      throw new Error('Set GRADESCOPE_COURSE_ID to build Gradescope submission URLs.');
    }

    return rows.map((row) => ({
      submissionId: row.submission_id,
      platformAssignmentId: row.platform_assignment_id,
      platformSubmissionUrl:
        `https://www.gradescope.com/courses/${courseId}/assignments/` +
        `${row.platform_assignment_id}/submissions/${row.platform_submission_id}`,
    }));
  }

  public async authorizeForGrading(submissionId: string): Promise<void> {
    const response = await fetch(
      `${this.baseUrl}/internal/nightly/submissions/${submissionId}/authorize`,
      { method: 'POST', headers: this.headers() },
    );
    if (!response.ok) {
      throw new Error(`Database API returned ${response.status} while authorizing ${submissionId}.`);
    }
  }

  public async recordSubmissionResult(
    submissionId: string,
    status: 'AUTHORIZED' | 'RERUN_SUBMITTED' | 'RERUN_SUCCEEDED' | 'RERUN_FAILED',
    errorMessage?: string,
  ): Promise<void> {
    const response = await fetch(
      `${this.baseUrl}/internal/nightly/runs/${this.runId}/submissions/${submissionId}`,
      {
        method: 'POST',
        headers: { ...this.headers(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, error_message: errorMessage ?? null }),
      },
    );
    if (!response.ok) {
      throw new Error(`Database API returned ${response.status} while recording ${submissionId}.`);
    }
  }

  public async finishRun(summary: TriggerRunSummary): Promise<void> {
    if (!this.runId) {
      throw new Error('Cannot finish a nightly run that was not started.');
    }
    const response = await fetch(`${this.baseUrl}/internal/nightly/runs/${this.runId}/finish`, {
      method: 'POST',
      headers: { ...this.headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        status: summary.failed === 0 && !summary.fatalError ? 'SUCCEEDED' : 'FAILED',
        submissions_authorized: summary.authorized,
        submissions_rerun: summary.rerun,
        submissions_failed_to_trigger: summary.failed,
      }),
    });
    if (!response.ok) {
      throw new Error(`Database API returned ${response.status} while finishing the nightly run.`);
    }
  }
}

// Backwards-compatible name for callers from the first prototype.
export { HttpGradingClient as GradingApiClient };

import type { FailureKind } from './interfaces';

/** Classify only the integration boundary failure; AI success is decided by the backend. */
export function classifyFailure(message: string): FailureKind {
  if (/database api|internal token|grading database|assignment token/i.test(message)) {
    return 'DATABASE';
  }
  if (/login|log in|sign in|credential|authenticate|account/i.test(message)) {
    return 'AUTHENTICATION';
  }
  if (/timeout|timed out|did not finish within/i.test(message)) {
    return 'TIMEOUT';
  }
  if (/ai feedback|vertex|gemini|model request|provider/i.test(message)) {
    return 'AI_FEEDBACK';
  }
  if (/autograder|grader.*fail|functional test/i.test(message)) {
    return 'AUTOGRADER';
  }
  if (/rerun|gradescope|click/i.test(message)) {
    return 'RERUN_TRIGGER';
  }
  return 'UNKNOWN';
}

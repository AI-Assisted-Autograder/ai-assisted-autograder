import { expect, test } from '@playwright/test';

import { classifyAutograderState } from '../../integration/gradescope-platform';

test.describe('Gradescope autograder state classification', () => {
  test('keeps a running submission out of the completed state', () => {
    expect(
      classifyAutograderState(
        "Autograder Results The autograder hasn't finished running yet",
      ),
    ).toBe('RUNNING');
  });

  test('recognizes a completed autograder result', () => {
    expect(classifyAutograderState('Autograder Results Score 110.0 / 110.0')).toBe('COMPLETE');
  });

  test('recognizes an autograder failure', () => {
    expect(classifyAutograderState('Autograder error: failed to run')).toBe('FAILED');
  });
});

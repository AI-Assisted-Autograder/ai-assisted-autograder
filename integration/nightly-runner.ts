import { chromium } from '@playwright/test';

import { HttpGradingClient } from './grading-api-client';
import { GradescopePlatform } from './gradescope-platform';
import { NightlyTrigger } from './nightly-trigger';
import type { TriggerSource } from './interfaces';

async function main(): Promise<void> {
  if (process.env.GRADESCOPE_ALLOW_RERUN !== '1') {
    throw new Error('Set GRADESCOPE_ALLOW_RERUN=1 to authorize live nightly reruns.');
  }

  const browser = await chromium.launch({ headless: process.env.HEADFUL !== '1' });
  try {
    const context = await browser.newContext({
      baseURL: process.env.GRADESCOPE_BASE_URL ?? 'https://www.gradescope.com',
      storageState: process.env.GRADESCOPE_STORAGE_STATE || undefined,
    });
    const page = await context.newPage();
    const configuredSource = process.env.NIGHTLY_TRIGGER_SOURCE ?? 'PENDING_NIGHTLY';
    if (configuredSource !== 'PENDING_NIGHTLY' && configuredSource !== 'AI_FAILURES') {
      throw new Error('NIGHTLY_TRIGGER_SOURCE must be PENDING_NIGHTLY or AI_FAILURES.');
    }
    const summary = await new NightlyTrigger(
      new HttpGradingClient(),
      new GradescopePlatform(page),
    ).run(configuredSource as TriggerSource);
    console.log(JSON.stringify(summary));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

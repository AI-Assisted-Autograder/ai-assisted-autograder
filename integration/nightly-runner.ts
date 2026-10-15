import { chromium } from '@playwright/test';

import { HttpGradingClient } from './grading-api-client';
import { GradescopePlatform } from './gradescope-platform';
import { NightlyTrigger } from './nightly-trigger';

async function main(): Promise<void> {
  if (process.env.GRADESCOPE_ALLOW_RERUN !== '1') {
    throw new Error('Set GRADESCOPE_ALLOW_RERUN=1 to authorize live nightly reruns.');
  }

  const browser = await chromium.launch({ headless: process.env.HEADFUL !== '1' });
  try {
    const page = await browser.newPage({
      baseURL: process.env.GRADESCOPE_BASE_URL ?? 'https://www.gradescope.com',
    });
    const summary = await new NightlyTrigger(
      new HttpGradingClient(),
      new GradescopePlatform(page),
    ).run();
    console.log(JSON.stringify(summary));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { dirname } from 'node:path';

const storageStatePath = process.env.GRADESCOPE_STORAGE_STATE ?? '.auth/gradescope.json';

async function main(): Promise<void> {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();

  try {
    await page.goto('https://www.gradescope.com/login', { waitUntil: 'domcontentloaded' });
    console.log('Please complete the Gradescope login in the browser window.');
    await page.waitForURL(/\/account(?:[/?#]|$)/, { timeout: 120_000 });

    await mkdir(dirname(storageStatePath), { recursive: true });
    await context.storageState({ path: storageStatePath });
    console.log(`Saved Playwright login state to ${storageStatePath}`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

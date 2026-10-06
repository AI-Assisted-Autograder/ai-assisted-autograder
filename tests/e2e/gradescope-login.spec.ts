import { test, expect } from '@playwright/test';

test('logs in to the Gradescope non-SSO account', async ({ page }) => {
  const username = process.env.GRADESCOPE_USERNAME;
  const password = process.env.GRADESCOPE_PASSWORD;

  await page.goto('/login');

  const usernameInput = page.getByLabel(/email|username/i).first();
  const passwordInput = page.getByLabel(/password/i).first();
  await expect(usernameInput).toBeVisible();
  await expect(passwordInput).toBeVisible();

  if (!username || !password) {
    throw new Error(
      'Gradescope login page opened successfully, but credentials are missing. ' +
        'Set GRADESCOPE_USERNAME and GRADESCOPE_PASSWORD before running the login step. ' +
        'Credentials must not be written to source files or committed.',
    );
  }

  await usernameInput.fill(username);
  await passwordInput.fill(password);
  await page.getByRole('button', { name: /log in|sign in/i }).click();

  await expect(page).toHaveURL(/\/account(?:[/?#]|$)/, { timeout: 30_000 });
  await expect(page).toHaveURL(/gradescope\.com\/account(?:[/?#]|$)/);
});

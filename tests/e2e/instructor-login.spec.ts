
import { test, expect } from '@playwright/test';

test('logs in to the AI grading platform as an instructor', async ({ page }) => {
  const username = process.env.INSTRUCTOR_USERNAME;
  const password = process.env.INSTRUCTOR_PASSWORD;

  await page.goto('http://129.159.191.169:8000/login');

  const usernameInput = page.getByLabel(/email|username/i).first();
  const passwordInput = page.getByLabel(/password/i).first();

  await expect(usernameInput).toBeVisible();
  await expect(passwordInput).toBeVisible();

  if (!username || !password) {
    throw new Error(
      'Instructor credentials are missing. Set INSTRUCTOR_USERNAME and INSTRUCTOR_PASSWORD.',
    );
  }

  await usernameInput.fill(username);
  await passwordInput.fill(password);

  await page.getByRole('button', { name: /log in|sign in|login/i }).click();

  await expect(page).not.toHaveURL(/\/login(?:[/?#]|$)/, {
    timeout: 30_000,
  });
});

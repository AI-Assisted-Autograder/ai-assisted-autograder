# Running TypeScript Tests

These tests use Playwright Test.

## 1. Navigate to the TypeScript Test Folder

From the repository root, run:

```bash
cd tests/unit/typescript
```

## 2. Run All TypeScript Tests

```bash
npx playwright test
```

## 3. Run a Single Test File

For example:

```bash
npx playwright test nightly-trigger.spec.ts
```

# PowerShell Unit Tests

This directory contains unit tests for the PowerShell scripts used by the AI-Assisted Autograder project.

The tests use **Pester 6.2.0**, a PowerShell testing framework.

## 1. Prerequisites

Before running the tests, make sure you have:

- PowerShell 7 or later installed.
- Pester 6.2.0 installed.
- Access to the project repository.

### Check PowerShell installation

Open your terminal and run:

```bash
pwsh --version
```

### Check Pester installation

Start PowerShell:

```bash
pwsh
```

Then run:

```powershell
Get-Module Pester -ListAvailable
```

If Pester 6.2.0 is not installed, install it:

```powershell
Install-Module Pester -RequiredVersion 6.2.0 -Force -Scope CurrentUser
```

Exit PowerShell when finished:

```powershell
exit
```

## 2. Navigate to the test directory

From the project root, run:

```bash
cd tests/unit/powershell
```

All commands in the remaining sections assume you are inside this directory.

## 3. Running the tests

### Run all PowerShell unit tests

To execute every Pester test file in the current directory:

```bash
pwsh -Command "Invoke-Pester . -Output Detailed"
```

### Run an individual test file

To run only the nightly trigger tests:

```bash
pwsh -Command "Invoke-Pester ./NightlyTrigger.Tests.ps1 -Output Detailed"
```

To run only the scheduled task registration tests:

```bash
pwsh -Command "Invoke-Pester ./register-nightly-trigger.tests.ps1 -Output Detailed"
```

## 4. Understanding test results

Pester reports the results of each test.

- `[+]` Test passed.
- `[-]` Test failed.
- `Skipped` Test was intentionally skipped.

Example successful output:

```text
Describing register-nightly-trigger.ps1
  [+] uses 02:00 as the default scheduled time
  [+] accepts a custom scheduled time
  [+] rejects an invalid time format
  [+] registers the expected scheduled task name

Tests Passed: 11
Tests Failed: 0
```

A successful test run should have zero failed tests.

If a test fails, Pester provides the test name, file location, line number, and error message to help identify the problem.

## 5. What is being tested?

### Nightly trigger tests

File: `NightlyTrigger.Tests.ps1`

These tests verify:

- An error is thrown when `.env.local` is missing.
- The logs directory is created.
- Invalid environment file entries are rejected.
- Environment variables are loaded correctly.
- Failed npm execution produces an error.
- Surrounding quotes are removed from environment values.
- Comments and blank lines are ignored.

### Scheduled task registration tests

File: `register-nightly-trigger.tests.ps1`

These tests verify:

- The default scheduled time is 02:00.
- Custom scheduled times are accepted.
- Invalid time formats are rejected.
- The correct scheduled task name is registered.
- The task is configured to run daily.
- The execution time limit is two hours.
- `StartWhenAvailable` is enabled.
- Existing tasks can be replaced using `-Force`.
- The correct nightly trigger script is configured.
- The scheduled task description is correct.

## 6. Important testing notes

### Windows-specific functionality

The scheduled task registration script uses Windows Task Scheduler commands. These commands are not available natively on macOS or Linux.

The unit tests mock these commands, allowing the scheduling configuration to be tested without actually registering a Windows scheduled task.

### Nightly trigger execution

The nightly trigger tests mock `npm.cmd` so that the actual nightly job is not executed during testing.

### Environment variables

The tests use temporary directories provided by Pester to avoid modifying the project's actual `.env.local` file or logs directory.

### Production scripts

The tests should not require modifications to the production scripts just to run on macOS.

## 7. Running tests in CI

These tests can also run automatically through GitHub Actions.

The PowerShell CI workflow installs PowerShell and Pester before executing the test suite.

Tests run automatically when changes are pushed to the configured branch or when a pull request is opened.
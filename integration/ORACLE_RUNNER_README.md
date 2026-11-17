# Oracle Gradescope Runner Guide

This guide explains how to access the Oracle server, configure the nightly
schedule, update the Gradescope account, and inspect runner results.

## 1. Log in from Windows PowerShell

Run this in PowerShell:

```powershell
ssh -i "$env:USERPROFILE\.ssh\oci_autograder" wuruo@129.159.191.169
```

After login:

```bash
whoami
hostname
```

The runner is installed at:

```text
/home/wuruo/gradescope-runner
```

## 2. View the current schedule

Cron runs the wrapper every minute. The wrapper checks the configured New York
time and only starts the runner at matching times.

```bash
crontab -l
grep '^NIGHTLY_' /home/wuruo/.config/gradescope-runner/runner.env
```

Important settings:

```text
NIGHTLY_LOCAL_TIME=02:00
NIGHTLY_RETRY_LOCAL_TIME=03:00
NIGHTLY_FALLBACK_LOCAL_TIMES=04:00,05:00,12:00,13:00
NIGHTLY_CHECK_TIMES=02:30,03:30,12:30,13:30
```

Times use `America/New_York`, including daylight-saving changes.

## 3. Change the schedule

Open the protected environment file:

```bash
nano /home/wuruo/.config/gradescope-runner/runner.env
```

For example, to add a 01:30 retry:

```text
NIGHTLY_FALLBACK_LOCAL_TIMES=04:00,05:00,12:00,13:00,01:30
```

Save in nano with `Ctrl+O`, press `Enter`, then exit with `Ctrl+X`.
No cron restart is required.

Verify the saved setting:

```bash
grep '^NIGHTLY_FALLBACK_LOCAL_TIMES=' /home/wuruo/.config/gradescope-runner/runner.env
```

Do not add the same time to multiple schedulers, or the same submission could
be triggered twice.

## 4. Update the Gradescope account

Open the same file:

```bash
nano /home/wuruo/.config/gradescope-runner/runner.env
```

Update only these entries:

```text
GRADESCOPE_USERNAME=the_non_SSO_email
GRADESCOPE_PASSWORD=the_password
```

The account must have instructor access to the target course. Verify only the
variable names, never the values:

```bash
grep -E '^(GRADESCOPE_USERNAME|GRADESCOPE_PASSWORD)=' /home/wuruo/.config/gradescope-runner/runner.env | sed 's/=.*$/=<configured>/'
```

Protect the file:

```bash
chmod 600 /home/wuruo/.config/gradescope-runner/runner.env
```

The file also contains the internal API token. Never paste it into GitHub,
chat, logs, README files, or shell history.

## 5. View runner logs

Main runner log:

```bash
cd /home/wuruo/gradescope-runner/logs
tail -n 50 nightly-trigger.log
```

Watch new output:

```bash
tail -f nightly-trigger.log
```

Status-check log:

```bash
tail -n 50 nightly-status.log
```

Press `Ctrl+C` to stop following a log.

## 6. Interpret results

```text
RERUN_SUBMITTED       Gradescope accepted the rerun trigger.
RERUN_TRIGGER_FAILED  Playwright could not complete the Gradescope trigger.
AUTHORIZATION_FAILED  The backend did not authorize the submission.
failed: 0             The runner recorded no trigger-stage failure.
```

`RERUN_SUBMITTED` confirms that the browser reached the Gradescope
confirmation/status state. It does not prove that the autograder or AI
feedback finished successfully. Check the backend result after completion.

Useful filters:

```bash
grep 'RERUN_SUBMITTED' nightly-trigger.log | tail -n 20
grep -E 'RERUN_TRIGGER_FAILED|AUTHORIZATION_FAILED|AI' nightly-trigger.log | tail -n 20
grep '"found"' nightly-trigger.log | tail -n 10
```

## 7. Check cron

```bash
systemctl is-active cron 2>/dev/null || systemctl is-active crond
crontab -l
stat /home/wuruo/gradescope-runner/logs/nightly-trigger.log
```

## 8. Safe operating rules

- Keep only one active scheduler for the same assignment scope.
- Confirm the assignment and submission scope before enabling live reruns.
- Keep `GRADESCOPE_ALLOW_RERUN=1` only on the intended runner environment.
- Do not edit or commit `runner.env`.
- Do not use `cat runner.env` when sharing terminal output.
- Make code changes in GitHub first, then deploy the tested commit to Oracle.
- Back up the environment file before changing schedule or credentials.

#!/usr/bin/env bash
set -euo pipefail

RUNNER_DIR="${RUNNER_DIR:-/home/wuruo/gradescope-runner}"
CONFIG_DIR="${CONFIG_DIR:-/home/wuruo/.config/gradescope-runner}"
LOG_FILE="${RUNNER_DIR}/logs/nightly-trigger.log"

mkdir -p "${RUNNER_DIR}/logs"

# Cron runs hourly in UTC on the server. This local-time guard keeps the
# schedule at 02:00 America/New_York across daylight-saving changes.
if [[ "$(TZ=America/New_York date +%H:%M)" != "02:00" ]]; then
  exit 0
fi

if [[ ! -f "${CONFIG_DIR}/runner.env" ]]; then
  echo "$(date -Is) runner.env is missing" >> "${LOG_FILE}"
  exit 1
fi

if ! docker image inspect mcr.microsoft.com/playwright:v1.63.0-noble >/dev/null 2>&1; then
  echo "$(date -Is) Playwright image is missing" >> "${LOG_FILE}"
  exit 1
fi

exec docker run --rm --network host --env-file "${CONFIG_DIR}/runner.env" -v "${RUNNER_DIR}:/work" -v "${CONFIG_DIR}:/run/secrets:ro" -w /work --name ai-assisted-autograder-nightly mcr.microsoft.com/playwright:v1.63.0-noble npm run nightly:trigger >> "${LOG_FILE}" 2>&1

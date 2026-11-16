#!/usr/bin/env bash
set -euo pipefail

RUNNER_DIR="${RUNNER_DIR:-/home/wuruo/gradescope-runner}"
CONFIG_DIR="${CONFIG_DIR:-/home/wuruo/.config/gradescope-runner}"
LOG_FILE="${RUNNER_DIR}/logs/nightly-trigger.log"
NIGHTLY_LOCAL_TIME="${NIGHTLY_LOCAL_TIME:-02:00}"
NIGHTLY_RETRY_LOCAL_TIME="${NIGHTLY_RETRY_LOCAL_TIME:-03:00}"
TRIGGER_SOURCE="${NIGHTLY_TRIGGER_SOURCE:-PENDING_NIGHTLY}"
RETRY_TRIGGER_SOURCE="${NIGHTLY_RETRY_TRIGGER_SOURCE:-AI_FAILURES}"
FALLBACK_LOCAL_TIMES="${NIGHTLY_FALLBACK_LOCAL_TIMES:-04:00,05:00}"
WAIT_FOR_COMPLETION="${NIGHTLY_WAIT_FOR_COMPLETION:-0}"

mkdir -p "${RUNNER_DIR}/logs"

if [[ -f "${CONFIG_DIR}/runner.env" ]]; then
  configured_time="$(sed -n 's/^NIGHTLY_LOCAL_TIME=//p' "${CONFIG_DIR}/runner.env" | tail -n 1)"
  if [[ -n "${configured_time}" ]]; then
    NIGHTLY_LOCAL_TIME="${configured_time}"
  fi
  configured_retry_time="$(sed -n 's/^NIGHTLY_RETRY_LOCAL_TIME=//p' "${CONFIG_DIR}/runner.env" | tail -n 1)"
  if [[ -n "${configured_retry_time}" ]]; then
    NIGHTLY_RETRY_LOCAL_TIME="${configured_retry_time}"
  fi
  configured_source="$(sed -n 's/^NIGHTLY_TRIGGER_SOURCE=//p' "${CONFIG_DIR}/runner.env" | tail -n 1)"
  if [[ -n "${configured_source}" ]]; then
    TRIGGER_SOURCE="${configured_source}"
  fi
  configured_retry_source="$(sed -n 's/^NIGHTLY_RETRY_TRIGGER_SOURCE=//p' "${CONFIG_DIR}/runner.env" | tail -n 1)"
  if [[ -n "${configured_retry_source}" ]]; then
    RETRY_TRIGGER_SOURCE="${configured_retry_source}"
  fi
  configured_fallback_times="$(sed -n 's/^NIGHTLY_FALLBACK_LOCAL_TIMES=//p' "${CONFIG_DIR}/runner.env" | tail -n 1)"
  if [[ -n "${configured_fallback_times}" ]]; then
    FALLBACK_LOCAL_TIMES="${configured_fallback_times}"
  fi
fi

# Cron runs hourly in UTC on the server. This local-time guard keeps the
# schedule at NIGHTLY_LOCAL_TIME America/New_York across daylight-saving changes.
current_local_time="$(TZ=America/New_York date +%H:%M)"
if [[ "${current_local_time}" == "${NIGHTLY_LOCAL_TIME}" ]]; then
  TRIGGER_SOURCE="PENDING_NIGHTLY"
  WAIT_FOR_COMPLETION="0"
elif [[ "${current_local_time}" == "${NIGHTLY_RETRY_LOCAL_TIME}" ]]; then
  TRIGGER_SOURCE="${RETRY_TRIGGER_SOURCE}"
  WAIT_FOR_COMPLETION="0"
elif [[ ",${FALLBACK_LOCAL_TIMES}," == *",${current_local_time},"* ]]; then
  TRIGGER_SOURCE="${RETRY_TRIGGER_SOURCE}"
  WAIT_FOR_COMPLETION="0"
else
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

exec docker run --rm --network host --env-file "${CONFIG_DIR}/runner.env" --env "NIGHTLY_TRIGGER_SOURCE=${TRIGGER_SOURCE}" --env "NIGHTLY_WAIT_FOR_COMPLETION=${WAIT_FOR_COMPLETION}" -v "${RUNNER_DIR}:/work" -v "${CONFIG_DIR}:/run/secrets:ro" -w /work --name ai-assisted-autograder-nightly mcr.microsoft.com/playwright:v1.63.0-noble npm run nightly:trigger >> "${LOG_FILE}" 2>&1

#!/usr/bin/env bash
set -euo pipefail

RUNNER_DIR="${RUNNER_DIR:-/home/wuruo/gradescope-runner}"
LOG_FILE="${RUNNER_DIR}/logs/nightly-status.log"
CHECK_TIMES="${NIGHTLY_CHECK_TIMES:-02:30,03:30}"
current_local_time="$(TZ=America/New_York date +%H:%M)"

if [[ ",${CHECK_TIMES}," != *",${current_local_time},"* ]]; then
  exit 0
fi

mkdir -p "${RUNNER_DIR}/logs"
{
  echo "$(date -Is) check_time=${current_local_time}"
  echo "Latest trigger runs:"
  docker exec web-db-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select id, status, started_at, finished_at from trigger_run order by started_at desc limit 3;"'
  echo "Latest trigger outcomes and latest reported AI outcomes (AI outcomes may predate the trigger):"
  docker exec web-db-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P null=unknown -c "select t.trigger_run_id, t.submission_id, a.platform_assignment_id, s.platform_submission_id, t.status as trigger_status, s.ai_status as latest_reported_ai_status, s.is_active_attempt, t.recorded_at from trigger_run_submission t left join submission s on s.id = t.submission_id left join assignment a on a.id = s.assignment_id order by t.recorded_at desc limit 10;"'
  echo "---"
} >> "${LOG_FILE}" 2>&1

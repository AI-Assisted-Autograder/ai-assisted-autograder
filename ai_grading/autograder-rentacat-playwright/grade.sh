#!/usr/bin/env bash
#
# Grades one submission.  Called by run_autograder.  All autograder files,
# including this one, come from the autograder zip that Gradescope extracts into
# /autograder/source.
#
# The paths below can be overridden with environment variables for local testing.

BASE_DIR="${BASE_DIR:-/autograder/source}"
SUBMISSION_DIR="${SUBMISSION_DIR:-/autograder/submission}"
export RESULTS_FILE="${RESULTS_FILE:-/autograder/results/results.json}"
NVM_SCRIPT="${NVM_SCRIPT:-/root/.nvm/nvm.sh}"
# Set to 1 to include Playwright and JUnit details in the autograder log.
export CS1632_LOG_DETAILS="${CS1632_LOG_DETAILS:-0}"

# Wrap every line this script prints (including, with CS1632_LOG_DETAILS=1,
# Playwright) at this column, breaking at spaces, so the
# Gradescope autograder log is readable.  0 turns wrapping off.  stdbuf keeps
# fold line-buffered, so nothing is lost if Gradescope stops the run early.
LOG_WIDTH="${CS1632_LOG_WIDTH:-100}"
if [ "$LOG_WIDTH" -gt 0 ] 2>/dev/null && command -v fold >/dev/null && command -v stdbuf >/dev/null; then
  exec > >(stdbuf -oL fold -s -w "$LOG_WIDTH") 2>&1
  LOG_WRAP_PID=$!
  # On exit, close our end of the pipe and let fold print its last line.
  trap 'exec >&- 2>&-; wait "$LOG_WRAP_PID" 2>/dev/null' EXIT
fi

# Elapsed-time markers for the autograder log.
stamp() {
  echo "[autograder +${SECONDS}s] $*"
}
stamp "grading started"

CORRECT_URL=https://cs1632.appspot.com/
BUGGY_URL=https://cs1632-buggier.appspot.com/

cd "$BASE_DIR" || exit 1

TESTS_DIR="$BASE_DIR/submission/tests"
REPORT_DIR="$BASE_DIR/submission/playwright-report"
SPEC="$TESTS_DIR/rentacat.spec.ts"
BUGGY_SPEC="$TESTS_DIR/rentacatbuggy.spec.ts"
NOTES="$REPORT_DIR/notes.txt"     # shown to the student at the top of the results
RUN_LOG="$REPORT_DIR/run.log"     # Playwright console output; tail shown on errors
RUN_LOG_BUGGY="$REPORT_DIR/run-buggy.log"

rm -rf "$REPORT_DIR" "$BUGGY_SPEC" "$BUGGY_SPEC-snapshots"
mkdir -p "$REPORT_DIR" "$(dirname "$RESULTS_FILE")"
: > "$NOTES"

note() {
  printf 'NOTE: %s\n\n' "$1" >> "$NOTES"
}

# ---------------------------------------------------------------------------
# 1. Compile the grader before any student code runs.
# ---------------------------------------------------------------------------
if ! bash compile.sh; then
  echo "ERROR: the grader failed to compile."
  printf '{"score": 0, "output": "The autograder failed to compile. This is not your fault; please contact the instructor."}\n' > "$RESULTS_FILE"
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. Find the student's spec file.  Prefer */tests/rentacat.spec.ts; if there
#    are several, take the one closest to the root of the submission.
# ---------------------------------------------------------------------------
find_spec() {
  local best="" count=0 f
  while IFS= read -r -d '' f; do
    count=$((count + 1))
    if [ -z "$best" ] || [ "${#f}" -lt "${#best}" ]; then
      best="$f"
    fi
  done < <(find "$SUBMISSION_DIR" -type f "$@" -not -path '*/node_modules/*' -print0 2>/dev/null)
  SPEC_SRC="$best"
  SPEC_COUNT=$count
}

find_spec -path '*/tests/rentacat.spec.ts'
if [ -z "$SPEC_SRC" ]; then
  find_spec -name 'rentacat.spec.ts'
  if [ -n "$SPEC_SRC" ]; then
    note "rentacat.spec.ts was not inside a tests/ folder. Grading ${SPEC_SRC#"$SUBMISSION_DIR"/} anyway, but please keep it at tests/rentacat.spec.ts."
  fi
fi

if [ -z "$SPEC_SRC" ]; then
  note "Could not find tests/rentacat.spec.ts in your submission. Make sure you submitted the right GitHub repository and that the file is committed and pushed."
else
  if [ "$SPEC_COUNT" -gt 1 ]; then
    note "Found $SPEC_COUNT copies of rentacat.spec.ts in your submission. Graded ${SPEC_SRC#"$SUBMISSION_DIR"/}. Please delete the extra copies."
  fi
  cp "$SPEC_SRC" "$SPEC"

  # test.only would silently stop every other test from running.
  if grep -Eq '\b(test|describe)\.only\(' "$SPEC"; then
    sed -E -i 's/\b(test|describe)\.only\(/\1(/g' "$SPEC"
    note "Your rentacat.spec.ts uses test.only (or describe.only). It was removed so that all of your tests run. Please remove it from your code."
  fi

  if grep -q 'buggier' "$SPEC"; then
    note "Your rentacat.spec.ts mentions the buggier website. Your tests should only refer to $CORRECT_URL; the autograder creates the buggier version of your tests itself."
  fi
  if ! grep -q 'cs1632\.appspot\.com' "$SPEC"; then
    note "Your rentacat.spec.ts never mentions cs1632.appspot.com. The autograder does not use your playwright.config.ts (for example a baseURL setting), so your tests must contain the full $CORRECT_URL URLs."
  fi

  # Build the version of the tests that runs against the buggier website.  It
  # gets its own copy of the screenshot baselines, because Playwright looks for
  # snapshots in a folder named after the spec file.
  sed -E 's/cs1632\.appspot/cs1632-buggier.appspot/g' "$SPEC" > "$BUGGY_SPEC"
  if [ -d "$SPEC-snapshots" ]; then
    cp -r "$SPEC-snapshots" "$BUGGY_SPEC-snapshots"
  fi

  # Wake up both App Engine instances so cold starts don't cause timeouts.
  for url in "$CORRECT_URL" "$BUGGY_URL"; do
    curl -s -o /dev/null --max-time 30 "$url" || true
  done

  # -------------------------------------------------------------------------
  # 3. Run Playwright, once per website.  Each JUnit report goes straight to a
  #    file, so nothing else printed to the console can corrupt it.
  #
  #    Retries are only used on the correct website, where a flaky failure
  #    costs the student points; on the buggier website a failure is the
  #    expected outcome, so retrying only wastes time and memory.
  # -------------------------------------------------------------------------
  if [ -s "$NVM_SCRIPT" ]; then
    # shellcheck disable=SC1090
    source "$NVM_SCRIPT"
  fi

  run_playwright() {  # <spec-file> <report-file> <log-file> <retries>
    (
      cd "$BASE_DIR/submission" &&
      PLAYWRIGHT_JUNIT_OUTPUT_FILE="$2" \
      PLAYWRIGHT_JUNIT_OUTPUT_NAME="$2" \
        npx playwright test "$1" --project=chromium --retries="$4" --reporter=list,junit
    ) > "$3" 2>&1
    echo "Playwright exit code: $?" >> "$3"
  }

  stamp "Playwright on $CORRECT_URL started"
  run_playwright tests/rentacat.spec.ts      "$REPORT_DIR/results.xml"       "$RUN_LOG"       1
  stamp "Playwright on $BUGGY_URL started"
  run_playwright tests/rentacatbuggy.spec.ts "$REPORT_DIR/results-buggy.xml" "$RUN_LOG_BUGGY" 0

  # Playwright's output goes into the Gradescope autograder log only when
  # CS1632_LOG_DETAILS=1.  Students see failures in their results either way.
  if [ "$CS1632_LOG_DETAILS" = 1 ]; then
    echo "===== Playwright output: $CORRECT_URL ====="
    cat "$RUN_LOG"
    echo "===== Playwright output: $BUGGY_URL ====="
    cat "$RUN_LOG_BUGGY"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Grade the JUnit report.
# ---------------------------------------------------------------------------
stamp "JUnit grading started"
# The JUnit grader prints every result; keep that out of the log unless details
# are on, but always show it if the grader itself fails.
if [ "$CS1632_LOG_DETAILS" = 1 ]; then
  bash run.sh
elif ! bash run.sh > "$REPORT_DIR/junit_grader.log" 2>&1; then
  echo "ERROR: the JUnit grader failed:"
  cat "$REPORT_DIR/junit_grader.log"
fi

stamp "functional grading finished"

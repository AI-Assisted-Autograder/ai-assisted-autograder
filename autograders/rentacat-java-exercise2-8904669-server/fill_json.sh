#!/usr/bin/env bash
#
# Reads Jacoco coverage statistics and writes the Gradescope test entries for
# them to coverage_results.json.
#
# code_coverage.json is the template and is never modified, so this script is
# safe to run more than once in the same working tree.

TEMPLATE=code_coverage.json
OUTPUT=coverage_results.json
CSV=target/site/jacoco/jacoco.csv

# Total of the max_score fields in code_coverage.json. Used only by the
# fallback entry below, so that the points still add up when coverage cannot
# be measured at all.
COVERAGE_MAX_SCORE=16

CATIMPL_COVERED_INSTS=0
CATIMPL_TOTAL_INSTS=0
CATIMPL_PERCENT=0
CATIMPL_SCORE=0

RENTACAT_COVERED_INSTS=0
RENTACAT_TOTAL_INSTS=0
RENTACAT_PERCENT=0
RENTACAT_SCORE=0

if [ ! -f "$TEMPLATE" ]; then
  echo "fill_json.sh: $TEMPLATE not found" >&2
  exit 1
fi

# No CSV means the test run never completed: a student test called
# System.exit(), looped until the surefire timeout, or crashed the JVM, or
# Maven itself failed. Report one zero-score entry that explains why, instead
# of leaving the template placeholders in place, which would splice invalid
# JSON into results.json.
if [ ! -f "$CSV" ]; then
  echo "fill_json.sh: $CSV not found, writing fallback entry" >&2
  cat > "$OUTPUT" << EOF
{"output":"Code coverage could not be measured because the test run did not complete.\n\nThis happens if a test calls System.exit(), loops forever, or crashes the JVM.  Please make sure your tests terminate on their own.","output_format":"simple_format","score":0,"number":"","visibility":"visible","max_score":$COVERAGE_MAX_SCORE,"name":"Jacoco Coverage Check"}
EOF
  exit 0
fi

{
  read header
  while IFS="," read -r GROUP PACKAGE CLASS INSTRUCTION_MISSED INSTRUCTION_COVERED BRANCH_MISSED BRANCH_COVERED LINE_MISSED LINE_COVERED COMPLEXITY_MISSED COMPLEXITY_COVERED METHOD_MISSED METHOD_COVERED
  do
    if [ "$CLASS" = "CatImpl" ]; then
      CATIMPL_COVERED_INSTS=$INSTRUCTION_COVERED
      CATIMPL_TOTAL_INSTS=$((INSTRUCTION_MISSED + INSTRUCTION_COVERED))
      if [ $CATIMPL_TOTAL_INSTS -gt 0 ]; then
        CATIMPL_PERCENT=$((CATIMPL_COVERED_INSTS * 100 / CATIMPL_TOTAL_INSTS))
      fi
      # 0 points at 70% or below, 1 more point per additional 5%, capped at
      # the full 4 points at 90% or above.
      if [ $CATIMPL_PERCENT -ge 90 ]; then
        CATIMPL_SCORE=4
      elif [ $CATIMPL_PERCENT -gt 70 ]; then
        CATIMPL_SCORE=$(((CATIMPL_PERCENT - 70) / 5))
      fi
    fi
    if [ "$CLASS" = "RentACatImpl" ]; then
      RENTACAT_COVERED_INSTS=$INSTRUCTION_COVERED
      RENTACAT_TOTAL_INSTS=$((INSTRUCTION_MISSED + INSTRUCTION_COVERED))
      if [ $RENTACAT_TOTAL_INSTS -gt 0 ]; then
        RENTACAT_PERCENT=$((RENTACAT_COVERED_INSTS * 100 / RENTACAT_TOTAL_INSTS))
      fi
      # 0 points at 30% or below, 1 more point per additional 5%, capped at
      # the full 12 points at 90% or above.
      if [ $RENTACAT_PERCENT -ge 90 ]; then
        RENTACAT_SCORE=12
      elif [ $RENTACAT_PERCENT -gt 30 ]; then
        RENTACAT_SCORE=$(((RENTACAT_PERCENT - 30) / 5))
      fi
    fi
  done
} < "$CSV"

cp "$TEMPLATE" "$OUTPUT"

sed -E -i "s/CATIMPL_COVERED_INSTS/$CATIMPL_COVERED_INSTS/g" "$OUTPUT"
sed -E -i "s/CATIMPL_TOTAL_INSTS/$CATIMPL_TOTAL_INSTS/g" "$OUTPUT"
sed -E -i "s/CATIMPL_PERCENT/$CATIMPL_PERCENT/g" "$OUTPUT"
sed -E -i "s/CATIMPL_SCORE/$CATIMPL_SCORE/g" "$OUTPUT"

sed -E -i "s/RENTACAT_COVERED_INSTS/$RENTACAT_COVERED_INSTS/g" "$OUTPUT"
sed -E -i "s/RENTACAT_TOTAL_INSTS/$RENTACAT_TOTAL_INSTS/g" "$OUTPUT"
sed -E -i "s/RENTACAT_PERCENT/$RENTACAT_PERCENT/g" "$OUTPUT"
sed -E -i "s/RENTACAT_SCORE/$RENTACAT_SCORE/g" "$OUTPUT"

# Catch any leftover placeholder, whether it is one of the names substituted
# above or a typo in the template that nothing substitutes. The rendered
# entries contain no uppercase underscore tokens of their own, so any match
# here is a bug in code_coverage.json. This matters most for the placeholders
# inside the message text, where a leftover would still be valid JSON and
# would otherwise be shown to the student verbatim.
if grep -E -q "[A-Z][A-Z0-9]*_[A-Z0-9_]+" "$OUTPUT"; then
  echo "fill_json.sh: unsubstituted placeholder remains in $OUTPUT" >&2
  grep -E -o "[A-Z][A-Z0-9]*_[A-Z0-9_]+" "$OUTPUT" | sort -u | sed 's/^/  /' >&2
  rm -f "$OUTPUT"
  exit 1
fi

# The entries are a comma separated list rather than a document of their own,
# so wrap them in brackets to check them. This catches any bad substitution,
# not just the placeholder names known above.
if command -v python3 > /dev/null 2>&1; then
  if ! python3 -c 'import json, sys; json.loads("[" + open(sys.argv[1]).read().strip() + "]")' "$OUTPUT" 2> /dev/null; then
    echo "fill_json.sh: $OUTPUT is not valid JSON, check $TEMPLATE" >&2
    rm -f "$OUTPUT"
    exit 1
  fi
fi

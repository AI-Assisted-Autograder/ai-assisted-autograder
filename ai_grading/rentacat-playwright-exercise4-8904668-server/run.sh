#!/usr/bin/env bash

RESULTS_FILE="${RESULTS_FILE:-/autograder/results/results.json}"
java -ea -cp "bin:Libraries/*" com.github.tkutche1.jgrade.JGrade -c MainGrader -o "$RESULTS_FILE"

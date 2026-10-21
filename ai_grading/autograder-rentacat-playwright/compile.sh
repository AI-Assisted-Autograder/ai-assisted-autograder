#!/usr/bin/env bash

mkdir -p bin
# Compile the grader (all java files in the tests directory).
java_files=$(find tests -name "*.java")
javac -encoding UTF-8 -cp "Libraries/*:bin" -d bin $java_files

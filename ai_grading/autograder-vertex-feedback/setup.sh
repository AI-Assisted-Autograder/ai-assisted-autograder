#!/usr/bin/env bash
set -e

apt-get update
apt-get install -y openjdk-11-jdk python3 ca-certificates
chmod 600 /autograder/source/assignment.token

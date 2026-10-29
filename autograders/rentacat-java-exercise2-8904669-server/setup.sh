#!/usr/bin/env bash
chmod 600 /autograder/source/assignment.token

apt-get update
apt-get -y install openjdk-11-jdk maven bash python3
apt-get install -y build-essential

# The following lines are to make sure we can install packages from maven
# See https://bugs.launchpad.net/ubuntu/+source/ca-certificates-java/+bug/1396760
# and https://github.com/docker-library/openjdk/issues/19#issuecomment-70546872
apt-get install --reinstall ca-certificates-java
update-ca-certificates -f

# All autograder files come from this zip, which Gradescope extracts into
# /autograder/source.  Nothing is cloned from GitHub, so no deploy key is needed.

# Warm Maven dependencies while building the image.
cd /autograder/source
mvn -q dependency:go-offline || true

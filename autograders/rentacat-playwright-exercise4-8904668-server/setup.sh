#!/usr/bin/env bash
chmod 600 /autograder/source/assignment.token

apt-get update
apt-get -y install openjdk-11-jdk python3 curl ca-certificates

# Install node (latest LTS rather than latest Current, for stability).
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
bash -c "source ~/.nvm/nvm.sh && nvm install --lts && nvm alias default 'lts/*'"

# All autograder files come from this zip, which Gradescope extracts into
# /autograder/source.  Nothing is cloned from GitHub, so no deploy key is needed.

# Initialize playwright.  npm ci installs exactly the versions in
# package-lock.json and never rewrites package.json / package-lock.json, so the
# Playwright version matches what students use.
source $HOME/.nvm/nvm.sh
cd /autograder/source/submission
npm ci || exit 1
npx playwright install chromium
npx playwright install-deps chromium

#!/usr/bin/env bash

apt-get update
apt-get -y install openjdk-11-jdk
apt-get install -y build-essential
pip3 install gradescope-utils
# BEGIN GEMINI FEEDBACK
# Gemini dependencies (Gradescope Ubuntu 22.04 / Python 3.10+).
apt-get install -y python3-venv
python3 -m venv /autograder/ai-venv
/autograder/ai-venv/bin/python -m pip install -r /autograder/source/requirements.txt
# END GEMINI FEEDBACK

mkdir -p /root/.ssh
cd /autograder/source
cp ssh_config /root/.ssh/config
# Make sure to include your private key here
cp deploy_key /root/.ssh/deploy_key
chmod 0600 /root/.ssh/deploy_key
# To prevent host key verification errors at runtime
ssh-keyscan -t rsa github.com >> ~/.ssh/known_hosts

# Clone autograder files
git clone git@github.com:wonsunahn/CS1632_Autograder_JavaAssessment.git /autograder/source/github

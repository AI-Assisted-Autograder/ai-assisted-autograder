# Automated Code Feedback

## Overview

This project builds a cloud-hosted, LLM-powered feedback system that provides students with automated, targeted commentary on their source code. 

Instructors use a web interface to configure grading targets, rubrics, and feedback guidance, which are stored in a backing database. The system periodically processes new Gradescope submissions, runs functional tests against instructor-defined targets, and uses the test results, grading criteria, and student code to generate personalized feedback through LLM APIs. The generated feedback is stored in the database and delivered to students through Gradescope's feedback system.

## Features

- **Instructor Configuration:** Define grading targets, rubrics, and feedback guidance.
- **Automated Testing:** Run functional tests on student submissions.
- **LLM Feedback:** Generate targeted feedback using test results, rubrics, and source code.
- **Gradescope Integration:** Automate submission processing and feedback delivery.
- **Scheduled Workflows:** Coordinate nightly processing.
- **Cloud Hosting:** Deploy the application on Oracle Cloud Infrastructure.

## Architecture

The following diagram illustrates the system's main components and their interactions.

![Component Architecture](docs/architecture/rendered/01-component.png)

[View full-size diagram](docs/architecture/rendered/01-component.png)

## Technology Stack

| Component | Technology |
|---|---|
| Backend | FastAPI (Python) |
| Database | PostgreSQL |
| LLM Integration | Anthropic Claude API and/or OpenAI API |
| Workflow Testing | Playwright (TypeScript) |
| Python Testing | pytest |
| CI/CD and Scheduling | GitHub Actions |
| Cloud Hosting | Oracle Cloud Infrastructure (OCI) |

## How It Works

1. Instructors configure grading targets and rubrics.
2. A scheduled workflow identifies pending Gradescope submissions.
3. The system runs functional tests and records the results.
4. An LLM generates feedback using the rubric, test outcomes, and student code.
5. Feedback is stored in PostgreSQL and delivered through Gradescope.

## Project Status

This project is currently in development.
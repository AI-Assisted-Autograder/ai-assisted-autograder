# Automated Code Feedback

## Overview

Automated Code Feedback is a system that uses large language models (LLMs) to generate targeted feedback on student code submitted through Gradescope. It combines student source code, functional test results, and instructor-defined rubrics to provide actionable feedback.

The project consists of a web application, an AI feedback backend, and a Gradescope integration that coordinates submission processing. PostgreSQL stores assignment configuration and grading records, while the AI backend manages its own runtime state.

## Key Features

- **Instructor Interface:** Configure assignments, grading targets and rubrics; view submissions, test results and stored AI feedback.
- **Automated Testing:** Use functional test results to inform AI-generated feedback.
- **AI Feedback:** Generate feedback using configurable AI providers and models.
- **Gradescope Integration:** Authenticate with Gradescope and trigger autograder reruns.
- **Nightly Processing:** Identify eligible submissions and coordinate scheduled grading.
- **Database-Backed Configuration:** Manage assignments, targets, rubrics, and provider settings.
- **Containerized Development:** Run the web application and PostgreSQL using Docker Compose.

## Architecture

The system has three main application components and a shared database.

| Component | Responsibility | Documentation |
|---|---|---|
| Web Application | Provides the instructor interface, API endpoints, database access, and grading workflow state. | [Web README](web/README.md) |
| AI Grading | Builds prompts, communicates with AI providers, and generates feedback. | [AI Grading README](ai_grading/README.md) |
| Gradescope Integration | Coordinates submission reruns through Playwright and communicates with the Web API. | [Integration README](integration/README.md) |
| PostgreSQL Database | Stores assignment configuration, rubrics, submissions, test results, and feedback records. | [Database Schema](web/README.md#database-schema) |

### How the Components Work Together

1. Instructors configure assignments, grading targets and rubrics through the Web forms.
2. The autograder container fetches its assignment configuration from the backend, then sends source code and runtime evidence for AI feedback.
3. The backend authenticates the assignment token, enforces quota and calls the configured provider using DB-owned grading instructions.
4. The container displays feedback and configured AI scores in Gradescope, preserving functional-test scores when AI is unavailable.

The assignment-token feedback path keeps runtime state outside PostgreSQL and does
not write submission, test-result or feedback records to the DB. The nightly
integration manages eligibility and reruns through separate Web endpoints; it is
not yet connected to this feedback path.

![Component Architecture](docs/architecture/rendered/01-component.png)

[View the full-size component diagram](docs/architecture/rendered/01-component.png)

For detailed design information, see the [Architecture Documentation](docs/architecture)
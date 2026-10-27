# Running Tests

## Test Folder Structure

The `tests/` directory contains the project's automated tests, organized by language and purpose.

```text
tests/
├── requirements.txt
├── unit/
│   ├── aigradingtest/
│   │   ...
│   ├── webtest/
│   │   ...
│   └── typescript/
```

## 1. Create the Virtual Environment

From the project root, run:

```bash
python3 -m venv .venv
```

## 2. Activate the Virtual Environment

On macOS/Linux:

```bash
source .venv/bin/activate
```

On Windows:

```powershell
.venv\Scripts\activate
```

## 3. Install the Test Requirements

From the project root, run:

```bash
pip install -r tests/requirements.txt
```

## 4. Run the AI Grading Tests

From the project root, navigate to the AI grading test directory:

```bash
cd tests/unit/aigradingtest
```

Run the tests:

```bash
pytest
```

For more detailed output:

```bash
pytest -v
```

The AI grading tests use a `FakeProvider`, so they do not make calls to real LLM providers.

## 5. Run the Web Tests

From the project root, navigate to the web test directory:

```bash
cd tests/unit/web_test
```

Run the tests:

```bash
pytest
```

For more detailed output:

```bash
pytest -v
```

The web tests include tests for the application's home page, health endpoint, static files, and repository behavior.

## 6. Run a Specific Test File

From the appropriate test directory, run the desired test file.

For example, from `tests/unit/web_test`:

```bash
pytest test_main.py
```

For more detailed output:

```bash
pytest test_main.py -v
```

## 7. Run a Single Test

Specify the test file and test function.

For example:

```bash
pytest testmain.py::testindex -v
```

To run the health endpoint test:

```bash
pytest testmain.py::testhealth -v
```

## Expected Result

A successful test run should look similar to:

```text
============================= test session starts =============================
collected 3 items

testmain.py::testindex PASSED
testmain.py::testhealth PASSED
testmain.py::teststatic_css PASSED

============================== 3 passed =======================================
```

The exact output may vary depending on the tests collected.

## Test Configuration

The test directories contain their own `pytest.ini` and `conftest.py` configuration where needed. Tests should be run from their respective test directories so that these configurations are loaded correctly.

The test configuration handles the Python paths required to import the project's `web` and `ai_grading` components.

## Database Tests

Most unit tests do not require a running PostgreSQL database.

The `/health` test uses a mocked database connection, so it does not require PostgreSQL.

The repository tests cover both the in-memory and SQL repository implementations. When PostgreSQL is unavailable, the SQL tests are skipped automatically.

When PostgreSQL is available, the repository tests use the following default test database URL:

```text
postgresql+psycopg://autograder:autograder@localhost:5432/autograder_test
```

A different database can be specified using the `TEST_DATABASE_URL` environment variable.

## GitHub Actions CI

These workflows run automatically when code is pushed to the repository or when a pull request is opened or updated.

The workflows are located in [`.github/workflows/`](.github/workflows/).

- [Python Tests](.github/workflows/tests.yml)
- [TypeScript Tests](.github/workflows/typescript-tests.yml)

### Python Tests Workflow

The CI pipeline:

1. Checks out the repository.
2. Sets up Python 3.12.
3. Creates a virtual environment.
4. Installs the dependencies from `tests/requirements.txt`.
5. Runs the AI grading tests.
6. Runs the web tests.

### TypeScript Tests Workflow

The CI pipeline:

1. Checks out the repository.
2. Sets up Node.js 22 with npm caching.
3. Installs dependencies using `npm ci`.
4. Installs Playwright Chromium and its required dependencies.
5. Runs the TypeScript type check using `npm run typecheck`.
6. Runs the TypeScript unit tests in `tests/unit/typescript` using Playwright.
# Web Tests

## Overview

The Web tests cover the application's home page, health endpoint, static files, and repository behavior.

## Running Tests

### 1. Create the Virtual Environment

From the project root, run:

```bash
python3 -m venv .venv
```

### 2. Activate the Virtual Environment

On macOS/Linux:

```bash
source .venv/bin/activate
```

On Windows:

```powershell
.venv\Scripts\activate
```

### 3. Install Test Requirements

From the project root, run:

```bash
pip install -r tests/requirements.txt
```

### 4. Run the Tests

Navigate to the web test directory:

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

### 5. Run a Specific Test File

From the web test directory, run:

```bash
pytest test_main.py
```

For more detailed output:

```bash
pytest test_main.py -v
```

### 6. Run a Single Test

Specify the test file and test function:

```bash
pytest test_main.py::test_function_name -v
```

Replace the file and function names with the actual names used in the tests.

## Test Configuration

The test directories contain their own `pytest.ini` and `conftest.py` configuration where needed.

Tests should be run from their respective test directories so that the appropriate configurations are loaded.

The test configuration handles the Python paths required to import the project's `web` components.

## Database Tests

Most unit tests do not require a running PostgreSQL database.

The health endpoint test uses a mocked database connection, so it does not require PostgreSQL.

The repository tests cover both the in-memory and SQL repository implementations. When PostgreSQL is unavailable, the SQL tests are skipped automatically.

### Test Database Configuration

When PostgreSQL is available, the repository tests use the following default test database URL:

```text
postgresql+psycopg://autograder:autograder@localhost:5432/autograder_test
```

A different database can be specified using the `TEST_DATABASE_URL` environment variable.

## GitHub Actions CI

The Web tests are included in the GitHub Actions workflow located at `.github/workflows/tests.yml`.

The workflow installs the dependencies from `tests/requirements.txt` and runs the Web tests.
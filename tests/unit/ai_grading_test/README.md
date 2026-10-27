# AI Grading Tests

## Overview

The AI Grading tests are located in `tests/unit/aigradingtest`.

The tests use a `FakeProvider`, so they do not make calls to real LLM providers.

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

Navigate to the AI Grading test directory:

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

### 5. Run a Specific Test File

From the AI Grading test directory, run:

```bash
pytest test_file.py
```

Replace `test_file.py` with the name of the test file.

For more detailed output:

```bash
pytest test_file.py -v
```

### 6. Run a Single Test

Specify the test file and test function:

```bash
pytest test_file.py::test_function_name -v
```

Replace the file and function names with the actual names used in the tests.

## Test Configuration

The test directories contain their own `pytest.ini` and `conftest.py` configuration where needed.

Tests should be run from their respective test directories so that the appropriate configurations are loaded.

The test configuration handles the Python paths required to import the project's `ai_grading` components.

## GitHub Actions CI

The AI Grading tests are included in the GitHub Actions workflow located at `.github/workflows/tests.yml`.

The workflow installs the dependencies from `tests/requirements.txt` and runs the AI Grading tests.
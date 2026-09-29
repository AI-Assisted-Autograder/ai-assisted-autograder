
## Running Tests

### 1. Create the Virtual Environment

From the project root, create a virtual environment:

```bash
python3 -m venv .venv
```

### 2. Activate the Virtual Environment

On macOS/Linux:

```bash
source .venv/bin/activate
```

On Windows:

```bash
.venv\Scripts\activate
```

### 3. Install the Test Requirements

Go to the test directory:

```bash
cd tests
```

Install the required packages from `requirements.txt`:

```bash
pip install -r requirements.txt
```

### 4. Running All Web Tests

cd into the directory that contains the kind of tests you would like to run. For example:
```bash
cd unit/web_test
```
Then:
```bash
pytest
```

### 5. Running a Specific Test File

cd into the directory that contains the test you would like to run. For example:
```bash
cd unit/web_test
```
Then run the file you would like: 

```bash
pytest test_main.py
```

For more detailed output:

```bash
pytest test_main.py -v
```

### 6. Run a Single Test

```bash
pytest test_main.py::test_index -v
```

For example, to run the health endpoint test:

```bash
pytest test_main.py::test_health -v
```

### Expected Result

A successful test run should look similar to:

```text
============================= test session starts =============================
collected 3 items

test_main.py::test_index PASSED
test_main.py::test_health PASSED
test_main.py::test_static_css PASSED

============================== 3 passed =======================================
```

### Test Configuration

Tests contain their own `pytest.ini` and `conftest.py` configuration. This allows the tests to be run directly from their specific directory without manually setting Python paths or changing into the `web` application directory.

### Note

The virtual environment must be activated before running the tests.

Docker/PostgreSQL is not required for the current unit tests because the database connection in the `/health` test is mocked.

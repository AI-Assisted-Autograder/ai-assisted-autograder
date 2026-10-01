# Web app (Student B)

FastAPI backend + Jinja2/HTMX instructor UI, backed by PostgreSQL. See
`docs/architecture/DECISIONS.md` (AD-13) for why this stack.

## Run with Docker (recommended)

```bash
cp .env.example .env
docker compose up --build
```

Then visit http://localhost:8000 and http://localhost:8000/health (the
latter round-trips a query to Postgres to confirm the DB connection).

## Run without Docker

Requires a local PostgreSQL instance; point `DATABASE_URL` at it via `.env`.

```bash
python -m venv .venv
source .venv/bin/activate  # .venv\Scripts\activate on Windows
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

## Database schema

Tables are defined in `app/models.py`, which mirrors
`docs/architecture/02-data-model.puml`. Migrations live in
`migrations/versions/` and are applied automatically when the Docker
container starts.

To change the schema, edit `app/models.py`, then generate and review a
migration against a running database:

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

Autogenerate misses some things, notably CHECK constraint changes, so
read the generated file before committing it.

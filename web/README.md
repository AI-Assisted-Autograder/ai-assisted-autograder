# Web app (Student B)

FastAPI backend + Jinja2/HTMX instructor UI, backed by PostgreSQL. See
`docs/architecture/DECISIONS.md` (AD-2) for why this stack.

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
uvicorn app.main:app --reload
```

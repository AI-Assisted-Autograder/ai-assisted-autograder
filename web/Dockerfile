FROM python:3.12-slim

WORKDIR /app

COPY web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web/app/*.py ./app/
COPY web/app/repository/*.py ./app/repository/
COPY web/app/templates/*.html ./app/templates/
COPY web/app/static/*.css ./app/static/
COPY web/app/static/*.js ./app/static/
COPY web/alembic.ini .
COPY web/migrations/*.py web/migrations/script.py.mako ./migrations/
COPY web/migrations/versions/*.py ./migrations/versions/
COPY ai_grading/*.py ./ai_grading/
COPY ai_grading/ILLMProvider/*.py ./ai_grading/ILLMProvider/

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log"]

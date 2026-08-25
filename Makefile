.PHONY: up down build migrate seed run test lint fmt logs reset-db

# --- Docker Compose lifecycle ---
up:
	docker compose up --build

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f api

# --- Local (non-Docker) developer loop, matches README Option B ---
migrate:
	cd apps/api && alembic upgrade head

seed:
	cd apps/api && python -m app.db.seed

run:
	cd apps/api && uvicorn app.main:app --reload

test:
	cd apps/api && python -m pytest -v

lint:
	cd apps/api && python -m py_compile $$(find app -name '*.py')

# Drop and recreate the local dev database, then re-migrate and re-seed.
# Useful for proving migrations are reproducible, not just "worked once."
reset-db:
	cd apps/api && \
	python -c "from app.core.config import get_settings; import psycopg2; s=get_settings(); \
	print('Resetting DB defined by DATABASE_URL — see .env')" && \
	alembic downgrade base && \
	alembic upgrade head && \
	python -m app.db.seed

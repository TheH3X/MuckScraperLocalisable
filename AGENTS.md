# AGENTS.md

## Cursor Cloud specific instructions

MuckScraper is a single Python/Flask product with two runnable parts plus two backing services:

- **Web app** (`aggregator/`) — Flask UI + admin tooling. Primary product; runs standalone.
- **Scheduler** (`news_fetcher/scheduler.py`) — background ingestion/scrape/group/summarize pipeline.
- **PostgreSQL + pgvector** — required datastore.
- **Meilisearch** — full-text search (admin falls back to SQL `ILIKE` when it is down).

The dev environment is set up natively (no Docker in the cloud VM), even though `README.md`/`docker-compose.yml` document the Docker path. The Python venv, Postgres (apt `postgresql-16` + `postgresql-16-pgvector`), the Meilisearch binary, and the Playwright Chromium browser are all baked into the VM snapshot; the update script only refreshes Python deps.

### Starting services (not started automatically)

Neither Postgres nor Meilisearch auto-start on boot. Start them before running the app:

```bash
sudo pg_ctlcluster 16 main start                 # PostgreSQL (DB: aggregator, user muck)
meilisearch --master-key muckscraper-dev-key \
  --db-path /workspace/meili_data --http-addr 127.0.0.1:7700 &
```

### Running the app (dev)

The app does **not** load `.env` itself — env vars must be exported into the shell first. Also, the repo root must be on `PYTHONPATH` (running `python aggregator/app.py` directly fails with `ModuleNotFoundError: aggregator`). Use:

```bash
set -a; . ./.env; set +a
PYTHONPATH=/workspace .venv/bin/python -m aggregator.app   # dev server, debug reload, :5000
```

`/workspace/.env` (gitignored) holds local dev config: `DATABASE_URL=postgresql://muck:muckpass@127.0.0.1:5432/aggregator`, a generated `SECRET_KEY`, admin creds (`admin` / `admin12345`), and `MEILI_*`. If it is ever missing, recreate it from `.env.sample` with those local values. `NEWS_API_KEY`, `GNEWS_API_KEY`, and `OLLAMA_HOST` are intentionally blank — ingestion/LLM features degrade gracefully without them; set them only for full end-to-end ingestion tests.

First-time DB init / admin user (idempotent): `set -a; . ./.env; set +a; .venv/bin/python bootstrap_admin.py`.

### Tests

```bash
set -a; . ./.env; set +a; .venv/bin/python -m pytest tests/
```

### Dependency caveat

`requirements.txt` pins `pgvector==0.2.5`; the unpinned default (0.5.0) is incompatible with the pinned `sqlalchemy==1.4.35` (`ImportError: cannot import name 'Operators'`). Keep this pin.

### Known pre-existing code issues (not environment problems)

These exist on `main` and are unrelated to setup; do not treat them as env breakage:

- `news_fetcher/scheduler.py` has a syntax error (duplicated `get_last_static_sync` with an empty `except`), so the scheduler module currently cannot be imported/run.
- `aggregator/blueprints/public.py` imports `news_fetcher.summarizer` and `aggregator/blueprints/admin.py` uses `flash` without importing it (topic create still commits, then errors on the redirect). Two `tests/test_oss_boundary.py` tests fail as a result.

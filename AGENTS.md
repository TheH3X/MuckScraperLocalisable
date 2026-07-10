# AGENTS.md

## Cursor Cloud specific instructions

MuckScraper is a single Python/Flask product with two runnable parts plus two backing services:

- **Web app** (`aggregator/`) — Flask UI + admin tooling. Primary product; runs standalone.
- **Scheduler** (`news_fetcher/scheduler.py`) — background ingestion/scrape/group/summarize pipeline.
- **PostgreSQL + pgvector** — required datastore.
- **Meilisearch** — full-text search (admin falls back to SQL `ILIKE` when it is down).
- **Ollama** — local LLM runtime for summaries, headlines, bias scoring, topic classification, and story-grouping embeddings.

The dev environment is set up natively (no Docker in the cloud VM), even though `README.md`/`docker-compose.yml` document the Docker path. The Python venv, Postgres (apt `postgresql-16` + `postgresql-16-pgvector`), the Meilisearch binary, Playwright Chromium, and Ollama (with the `qwen3:4b` and `nomic-embed-text` models under `~/.ollama`) are all baked into the VM snapshot; the update script only refreshes Python deps.

### Starting services (not started automatically)

None of these auto-start on boot. Start them before running the app:

```bash
sudo pg_ctlcluster 16 main start                 # PostgreSQL (DB: aggregator, user muck)
meilisearch --master-key muckscraper-dev-key \
  --db-path /workspace/meili_data --http-addr 127.0.0.1:7700 &
OLLAMA_HOST=127.0.0.1:11434 ollama serve &       # local LLM, reachable at http://127.0.0.1:11434
```

### Ollama / LLM notes

- Models installed: `qwen3:4b` (chat/analysis, `OLLAMA_MODEL`) and `nomic-embed-text` (768-dim embeddings, `EMBEDDING_MODEL`; matches the `Vector(768)` column). `.env` sets `OLLAMA_HOST=http://127.0.0.1:11434`.
- **AVX-512 crash workaround:** the VM's virtualized Xeon advertises AVX-512 but Ollama's auto-selected `sapphirerapids`/AVX-512 CPU backends segfault (general protection fault). The AVX-512 `libggml-cpu-*.so` variants were moved to `/usr/local/lib/ollama/disabled_avx512/` so Ollama falls back to the AVX2 (`alderlake`/`haswell`) backend, which runs fine on CPU. Keep them moved; if Ollama is ever reinstalled, re-apply this. Inference is CPU-only and slow (tens of seconds for long generations).
- `qwen3` is a reasoning model. The `llm_client.py` task presets include `stop` sequences (e.g. `"\n\nI "`) that can fire during the model's thinking phase and yield an empty `response`; this is pre-existing app/model tuning, not an environment fault. Ollama itself generates correctly (verify with a raw `/api/generate` call using `"think": false`).

### Running the app (dev)

The app does **not** load `.env` itself — env vars must be exported into the shell first. Also, the repo root must be on `PYTHONPATH` (running `python aggregator/app.py` directly fails with `ModuleNotFoundError: aggregator`). Use:

```bash
set -a; . ./.env; set +a
PYTHONPATH=/workspace .venv/bin/python -m aggregator.app   # dev server, debug reload, :5000
```

`/workspace/.env` (gitignored) holds local dev config: `DATABASE_URL=postgresql://muck:muckpass@127.0.0.1:5432/aggregator`, a generated `SECRET_KEY`, admin creds (`admin` / `admin12345`), `MEILI_*`, and `OLLAMA_HOST=http://127.0.0.1:11434`. If it is ever missing, recreate it from `.env.sample` with those local values. `NEWS_API_KEY` and `GNEWS_API_KEY` are intentionally blank — API-based ingestion degrades gracefully (RSS still works); set them only for full end-to-end NewsAPI/GNews ingestion tests.

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

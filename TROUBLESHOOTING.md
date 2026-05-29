# Troubleshooting Reference

This file is a quick reference for diagnosing fetch, ingestion, scraping, bias, database, and scheduler issues in MuckScraper.

Assumptions:
- You are in the repo root: `/home/regis/Docker/muckscraper`
- The stack is running with `docker compose`
- Service names are `app`, `scheduler`, and `postgres`

Use the read-only commands first. The last section includes a few commands that actively rerun parts of the pipeline.

## Container and service checks

### Show running containers
What it does: confirms whether `app`, `scheduler`, and `postgres` are up.

```bash
docker compose ps
```

### Follow scheduler logs
What it does: shows fetch, scrape, bias, and n8n activity.

```bash
docker compose logs -f scheduler
```

### Show recent scheduler errors only
What it does: filters the scheduler log for failures and warnings.

```bash
docker compose logs --tail=300 scheduler | rg "ERROR|WARNING|Traceback|429|Webhook failed"
```

### Follow app logs
What it does: useful when the web UI is broken but the scheduler looks normal.

```bash
docker compose logs -f app
```

### Follow Postgres logs
What it does: checks whether the database is restarting, rejecting connections, or reporting corruption.

```bash
docker compose logs -f postgres
```

## Metrics and app state

### Show the saved run metrics
What it does: prints the last scheduler run metrics from `app_settings`.

```bash
docker compose exec postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select key, value from app_settings where key = '\''last_run_metrics'\'';"'
```

### Show fetch and AllSides timestamps
What it does: confirms when the scheduler last completed and when AllSides was last synced.

```bash
docker compose exec postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select key, value from app_settings where key in ('\''last_fetch'\'', '\''last_allsides_sync'\'') order by key;"'
```

## Fetch and ingestion queries

### Show the most recently fetched articles
What it does: confirms that ingestion is still writing new rows.

```sql
select id, fetched_at, date, source, title
from articles
order by fetched_at desc
limit 25;
```

### Show recent article volume by hour
What it does: helps confirm whether scheduled fetches actually produced rows.

```sql
select date_trunc('hour', fetched_at) as hour,
       count(*) as articles
from articles
where fetched_at >= now() - interval '48 hours'
group by 1
order by 1 desc;
```

### Show scrape outcomes for the last 24 hours
What it does: highlights scraper failures, fallback-heavy runs, or blocked domains.

```sql
select scrape_status,
       count(*) as articles
from articles
where fetched_at >= now() - interval '24 hours'
group by scrape_status
order by articles desc;
```

### Compare low-value skip reasons before and after a change date
What it does: summarizes run-level `roundup` and `low_value_url` skips from `scrape_outcome_history_v1`.
Use this after deploying filtering changes to confirm those skip reasons rise without reducing stored coverage.

```sql
with h as (
  select jsonb_array_elements(value::jsonb) elem
  from app_settings
  where key = 'scrape_outcome_history_v1'
),
hist as (
  select (elem->>'recorded_at')::timestamp as recorded_at,
         (elem->>'input_articles')::int as input_articles,
         (elem->>'stored_articles')::int as stored_articles,
         coalesce((elem->'run_skipped'->>'roundup')::int, 0) as roundup,
         coalesce((elem->'run_skipped'->>'low_value_url')::int, 0) as low_value_url
  from h
)
select case
         when recorded_at < timestamp '2026-05-16 00:00:00' then 'before'
         else 'after'
       end as period,
       count(*) as runs,
       sum(input_articles) as total_input,
       sum(stored_articles) as total_stored,
       round((sum(stored_articles)::numeric / nullif(sum(input_articles), 0)) * 100, 2) as stored_pct_input,
       sum(roundup) as total_roundup,
       sum(low_value_url) as total_low_value_url
from hist
where recorded_at >= timestamp '2026-05-13 00:00:00'
  and recorded_at < timestamp '2026-05-21 00:00:00'
group by 1
order by 1;
```

### Show recent scrape failures by outlet
What it does: identifies publishers that are currently breaking scraping.

```sql
select o.name as outlet,
       a.scrape_status,
       a.scrape_http_status,
       count(*) as articles
from articles a
left join outlets o on o.id = a.outlet_id
where a.fetched_at >= now() - interval '48 hours'
  and a.scrape_status in ('failed', 'blocked', 'fallback')
group by o.name, a.scrape_status, a.scrape_http_status
order by articles desc, outlet;
```

### Show raw payload volume by source and topic
What it does: confirms the upstream APIs are still returning payloads.

```sql
select source,
       topic_name,
       count(*) as payloads,
       max(fetched_at) as last_seen
from raw_article_payloads
where fetched_at >= now() - interval '48 hours'
group by source, topic_name
order by last_seen desc, source, topic_name;
```

### Show outlets still missing bias ratings
What it does: identifies whether bias coverage issues come from unrated outlets.

```sql
select id, name, bias_score, bias_source, bias_retry_count
from outlets
where bias_score is null
order by bias_retry_count desc, name
limit 100;
```

### Show overall article bias distribution
What it does: breaks down all stored articles by bias bucket, using article bias when present and falling back to outlet bias.

```sql
select case
         when coalesce(a.bias_score, o.bias_score) is null then 'unrated'
         when coalesce(a.bias_score, o.bias_score) <= 1.5 then 'left'
         when coalesce(a.bias_score, o.bias_score) <= 2.5 then 'lean_left'
         when coalesce(a.bias_score, o.bias_score) <= 3.5 then 'center'
         when coalesce(a.bias_score, o.bias_score) <= 4.5 then 'lean_right'
         else 'right'
       end as bias_bucket,
       count(*) as articles
from articles a
left join outlets o on o.id = a.outlet_id
group by 1
order by 1;
```

### Show article bias distribution for a certain time frame
What it does: lets you compare article bias for a recent window such as `24 hours`, `7 days`, or `30 days`.

```sql
select case
         when coalesce(a.bias_score, o.bias_score) is null then 'unrated'
         when coalesce(a.bias_score, o.bias_score) <= 1.5 then 'left'
         when coalesce(a.bias_score, o.bias_score) <= 2.5 then 'lean_left'
         when coalesce(a.bias_score, o.bias_score) <= 3.5 then 'center'
         when coalesce(a.bias_score, o.bias_score) <= 4.5 then 'lean_right'
         else 'right'
       end as bias_bucket,
       count(*) as articles
from articles a
left join outlets o on o.id = a.outlet_id
where a.fetched_at >= now() - interval '7 days'
group by 1
order by 1;
```

### Show article bias distribution for the latest scheduled fetch
What it does: uses the saved `last_run_metrics` start and finish timestamps to isolate the most recent scheduler fetch run.

```sql
with last_run as (
  select (value::jsonb ->> 'started_at')::timestamp as started_at,
         (value::jsonb ->> 'finished_at')::timestamp as finished_at
  from app_settings
  where key = 'last_run_metrics'
)
select case
         when coalesce(a.bias_score, o.bias_score) is null then 'unrated'
         when coalesce(a.bias_score, o.bias_score) <= 1.5 then 'left'
         when coalesce(a.bias_score, o.bias_score) <= 2.5 then 'lean_left'
         when coalesce(a.bias_score, o.bias_score) <= 3.5 then 'center'
         when coalesce(a.bias_score, o.bias_score) <= 4.5 then 'lean_right'
         else 'right'
       end as bias_bucket,
       count(*) as articles
from articles a
left join outlets o on o.id = a.outlet_id
cross join last_run r
where a.fetched_at >= r.started_at
  and a.fetched_at <= r.finished_at
group by 1
order by 1;
```

## Safe repair commands

These commands change state. Use them after you have confirmed the failure mode.

### Run one full fetch cycle manually
What it does: runs the scheduler pipeline immediately, including fetch, processing, metrics, and n8n notify.

```bash
docker exec muckscraper-scheduler-1 python -c "from news_fetcher.scheduler import run_all_fetches; run_all_fetches()"
```

### Retry unrated outlets
What it does: asks AllSides and then Ollama to fill in missing outlet bias ratings.

```bash
docker exec muckscraper-app-1 python -c "from aggregator import create_app; from news_fetcher.fetch_and_store_articles import retry_unrated_outlets; app=create_app(); ctx=app.app_context(); ctx.push(); retry_unrated_outlets()"
```

## Running the SQL queries

If you want to run any SQL above directly, use this pattern:

```bash
docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Then paste the query at the `psql` prompt.

For one-off execution from the shell:

```bash
docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select now();"'
```

# Project Map

This file is a working map of the current repository layout, what each major file or folder does, and a summary of the recent work sessions.

## Directory Tree

```text
.
├── .env                         - Local environment variables and secrets for Docker/app runtime.
├── .env.sample                  - Example environment variable template.
├── .gitignore                   - Local Git ignore rules for this repo.
├── CHANGELOG.md                 - Release-facing change history.
├── Dockerfile                   - Main app image build for the Flask app and shared runtime.
├── GEMINI.md                    - Project notes/instructions.
├── LICENSE                      - Repository license.
├── README.md                    - Main project overview and setup instructions.
├── TODO.md                      - Active roadmap and backlog notes.
├── boot.sh                      - Container entrypoint for app startup.
├── catcode.sh                   - Local helper script.
├── create_admin.py              - Utility script to create an admin user.
├── docker-compose.yml           - Primary local Docker Compose stack.
├── regroup_script.py            - Utility script for story regrouping tasks.
├── requirements.txt             - Python dependency pins for the project.
├── restart.sh                   - Local helper to restart services.
├── aggregator/                  - Flask web app package for admin/public site behavior.
│   ├── __init__.py              - App package marker.
│   ├── app.py                   - Main Flask app entrypoint.
│   ├── public_app.py            - Public-facing Flask app entrypoint.
│   ├── constants.py             - Shared app constants.
│   ├── filters.py               - Template/helper filters for summaries and display text.
│   ├── models.py                - SQLAlchemy models for articles, stories, editions, outlets, and related state.
│   ├── blueprints/              - Route modules for admin, auth, personal, and public views.
│   │   ├── admin.py             - Admin tools for articles, stories, audits, rescrapes, and moderation.
│   │   ├── auth.py              - Login/logout and auth routes.
│   │   ├── personal.py          - Personal/headlines site routes and edition rendering.
│   │   └── public.py            - Public site routes.
│   ├── static/                  - Shared CSS, images, and favicon assets.
│   └── templates/               - Jinja templates for admin/public pages.
│       ├── article.html         - Admin article detail page.
│       ├── articles.html        - Admin article/story listing and filters.
│       ├── headlines.html       - Dynamic headlines edition page.
│       ├── public_article.html  - Public/static article detail page shell.
│       ├── public_story.html    - Public/static story detail page shell.
│       ├── scrape_blocklist.html- Admin scrape blocklist page.
│       └── story.html           - Admin story detail page.
├── migrations/                  - Flask-Migrate/Alembic database migration setup.
│   ├── alembic.ini              - Alembic configuration used by Flask-Migrate.
│   ├── env.py                   - Migration environment/bootstrap code.
│   ├── script.py.mako           - Migration file template.
│   └── versions/                - Individual schema migrations.
│       ├── f12a3456bcde_add_scrape_result_fields_to_articles.py
│       │                          - Adds scrape telemetry fields to articles.
│       ├── c4f8b7a1d9e2_add_has_updates_to_edition_stories.py
│       │                          - Adds edition repeat/update flagging.
│       ├── d7a1f5c2e4b9_add_deep_analysis_to_articles.py
│       │                          - Adds article deep analysis storage.
│       └── e91f4c2b7a6d_merge_heads_after_scrape_and_analysis_changes.py
│                                  - Merges recent migration branches into one head.
├── news_fetcher/                - Fetching, scraping, ranking, grouping, and summarization pipeline.
│   ├── Dockerfile               - Secondary image definition for fetcher workflows.
│   ├── outlet_bias_lookup.py    - Outlet bias lookup helpers.
│   ├── backfill_images.py       - Backfill script for missing article images.
│   ├── cleanup_duplicates.py    - Utility for duplicate article cleanup.
│   ├── editorial_ranker.py      - Story ranking/editorial scoring helpers.
│   ├── fetch_and_store_articles.py
│   │                          - Main ingestion, persistence, grouping, and edition publishing pipeline.
│   ├── headline_generator.py    - Headline generation helpers.
│   ├── headline_ranker.py       - Ranking helpers for headline selection.
│   ├── merge_outlets.py         - Outlet merge tooling.
│   ├── outlet_bias_llm.py       - LLM-assisted outlet bias classification.
│   ├── rss_fetcher.py           - RSS feed fetch logic.
│   ├── scheduler.py             - Scheduled fetch/run entrypoints.
│   ├── scraper.py               - Article scrape pipeline and fallback extraction logic.
│   ├── story_grouper.py         - Story clustering/grouping logic.
│   ├── summarizer.py            - Story/article summary and deep analysis generation.
│   └── topic_classifier.py      - Topic classification helpers.
├── printouts/                   - Generated text snapshots and session notes.
│   ├── session_summary.md       - Prior generated summary notes.
│   └── tree.txt                 - Generated repository tree snapshot.
├── screenshots/                 - README/UI screenshots.
├── tests/                       - Automated tests.
│   ├── test_oss_boundary.py     - Tests that private/bypass behavior stays out of scope.
│   └── test_scraper_pipeline.py - Tests for scrape fallback and telemetry behavior.
└── postgres_data/               - Local Postgres data volume directory used by Docker.
```

## Notes On The Tree

- `postgres_data/` is a local database volume directory, not application source.
- Some ignored or deployment-specific directories are intentionally omitted from this public map.

## Changes From Last Night's Chat

- Cleaned `README.md`, `CHANGELOG.md`, and `.gitignore` for the public repo and removed deployment-specific references from the public docs.
- Added scrape reliability telemetry:
  - article scrape status
  - scrape method
  - failure reason
  - HTTP status
- Reworked `news_fetcher/scraper.py` into a ranked fallback pipeline:
  - normal extraction
  - readability fallback
  - canonical/print/mobile/AMP variant attempts
  - metadata extraction
  - RSS/API description fallback
- Wired scrape telemetry into ingestion and admin rescrape flows.
- Exposed scrape telemetry in the admin article list and article detail page.
- Added scrape-status filtering in the admin UI.
- Updated `TODO.md` to split scraping reliability into completed work and remaining work.
- Confirmed the static archive/export behavior uses HTML output and noted that archive story pages are not historical snapshots yet.
- Added a `Maybe Later` TODO item for archive snapshotting.
- Changed repeated-edition behavior:
  - repeated stories with no new articles stay at the end as carry-over filler
  - repeated stories with new articles stay in the normal ranking pool
- Added a reader-facing `New Updates` badge for repeated stories that gained new coverage.
- Refined summary/analysis format:
  - story executive summaries are now short paragraphs
  - deep story analysis remains the heavier multi-source report
  - articles can now get deep analysis only for topics that justify it, like politics and technical/business coverage
- Added and extended database migrations for scrape telemetry, edition update flags, article deep analysis, archived edition images, and grouping review fields, with a single current Alembic head.

## Changes From Tonight's Chat

- Improved scheduler startup behavior so it only catches up after a missed scheduled slot instead of running again after any one-hour gap.
- Tightened scrape retry behavior:
  - `fallback` clears URL retry state without clearing domain cooldowns
  - `403`/`404`-style failures skip low-value variant fan-out
- Added scrape outcome history reporting and DB-backed tests for persisted scrape states.
- Tightened `process_current_edition()` so unchanged stable stories are skipped earlier.
- Added publish-time edition dedupe for same-event headline candidates and refined the matcher to reduce generic-token false positives.
- Added archived edition image support on `edition_stories` and related schema changes.

## Current Follow-Up Items

- Review which modified and untracked files belong in the next public push so the commit is scoped cleanly.
- Restart the scheduler after code changes that affect future fetch/publish behavior.
- Validate the next live runs for scrape cooldown behavior, edition dedupe, and headline quality.

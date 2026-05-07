# muckscraperHeadlinesGoogleNEW/news_fetcher/scheduler.py
# news_fetcher/scheduler.py

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from aggregator import create_app, db
from aggregator.models import AppSetting
from news_fetcher.fetch_and_store_articles import fetch_and_store_articles, process_current_edition, sync_allsides_ratings, publish_edition, retry_unrated_outlets
from news_fetcher.rss_fetcher import fetch_and_store_rss
from datetime import datetime, timedelta
import logging
import sys
import os
import requests

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# New schedule times in Eastern Time: 7am, 12pm, 5pm, 10pm
SCHEDULE_HOURS = "7,12,17,22"
TIMEZONE = "America/New_York"

SCHEDULED_FETCHES = [
    # === NATIONAL / POLITICS ===
    {
        "label":          "US Politics",
        "mode":           "query",
        "country":        None,
        "category":       None,
        "query":          "US politics congress white house senate supreme court",
        "gnews_query":    "US politics congress white house",
        "gnews_category": None,
    },
    # === BUSINESS / ECONOMY ===
    {
        "label":          "Business & Economy",
        "mode":           "top",
        "country":        "us",
        "category":       "business",
        "query":          None,
        "gnews_query":    None,
        "gnews_category": "business",
    },
    # === SCIENCE / HEALTH ===
    {
        "label":          "Science & Health",
        "mode":           "query",
        "country":        None,
        "category":       None,
        "query":          "scientific breakthroughs medical research healthcare tech",
        "gnews_query":    "science health research",
        "gnews_category": "science",
    },
    # === SPORTS ===
    {
        "label":          "Sports",
        "mode":           "top",
        "country":        "us",
        "category":       "sports",
        "query":          None,
        "gnews_query":    None,
        "gnews_category": "sports",
    },
    # === WORLD NEWS ===
    {
        "label":          "World News",
        "mode":           "query",
        "country":        None,
        "category":       None,
        "query":          "international world global news conflicts diplomacy",
        "gnews_query":    "world global news",
        "gnews_category": "world",
    },
]

app = create_app()


def run_optional_headline_ranking():
    """
    Run the private ranking plugin when it exists locally.
    The open-source scheduler must not require ignored/private modules.
    """
    try:
        from news_fetcher.headline_ranker import run_headline_ranking
    except ImportError as e:
        logging.info(f"--- Headline ranking skipped ({e}) ---")
        return

    run_headline_ranking()


def run_optional_static_export():
    """
    Export the private static site when the ignored private exporter exists.
    This lets muckscraper.news publish after edition processing without making
    the open-source stack depend on private templates or routes.
    """
    try:
        from private_site.export_static import export_static_site
    except ImportError as e:
        logging.warning(
            "--- Static site export skipped (%s). If the public site is enabled, "
            "make sure docker-compose.private.yml is loaded and mounts "
            "./private_site and ./site_output into the scheduler container. ---",
            e,
        )
        return

    export_static_site()


def get_last_fetch_time():
    """Get the last fetch timestamp from the database."""
    setting = AppSetting.query.filter_by(key="last_fetch").first()
    if setting and setting.value:
        try:
            return datetime.fromisoformat(setting.value)
        except Exception:
            return None
    return None


def set_last_fetch_time():
    """Store the current time as the last fetch timestamp."""
    setting = AppSetting.query.filter_by(key="last_fetch").first()
    if setting:
        setting.value = datetime.utcnow().isoformat()
    else:
        setting = AppSetting(key="last_fetch", value=datetime.utcnow().isoformat())
        db.session.add(setting)
    db.session.commit()


def get_last_allsides_sync():
    """Get the last AllSides sync timestamp from the database."""
    setting = AppSetting.query.filter_by(key="last_allsides_sync").first()
    if setting and setting.value:
        try:
            return datetime.fromisoformat(setting.value)
        except Exception:
            return None
    return None


def set_last_allsides_sync():
    """Store the current time as the last AllSides sync timestamp."""
    setting = AppSetting.query.filter_by(key="last_allsides_sync").first()
    if setting:
        setting.value = datetime.utcnow().isoformat()
    else:
        setting = AppSetting(key="last_allsides_sync", value=datetime.utcnow().isoformat())
        db.session.add(setting)
    db.session.commit()


def should_fetch_now():
    """
    Returns True if it's been more than 1 hour since the last fetch,
    or if no fetch has ever been recorded. This allows the scheduler 
    to fetch on startup if it was offline during a scheduled window.
    """
    last_fetch = get_last_fetch_time()
    if not last_fetch:
        logging.info("No record of previous fetch. Initializing...")
        return True

    elapsed = datetime.utcnow() - last_fetch
    threshold = timedelta(hours=1)

    if elapsed >= threshold:
        logging.info(f"Last fetch was {elapsed} ago, fetching now.")
        return True
    else:
        logging.info(
            f"Last fetch was {int(elapsed.total_seconds() / 60)} minutes ago. "
            f"Skipping startup fetch."
        )
        return False


def _notify_n8n():
    webhook = os.getenv("N8N_WEBHOOK_URL")
    if not webhook:
        return
    try:
        requests.post(webhook, timeout=5)
        logging.info("  [n8n] Webhook fired — Ollama suspend sequence triggered")
    except Exception as e:
        logging.warning(f"  [n8n] Webhook failed ({e}) — continuing normally")


def run_all_fetches():
    logging.info("=== Starting scheduled fetch run ===")
    with app.app_context():
        # Fetch all categories
        for fetch in SCHEDULED_FETCHES:
            logging.info(f"--- Fetching: {fetch['label']} ---")
            try:
                fetch_and_store_articles(
                    fetch["label"],
                    mode=fetch["mode"],
                    query=fetch["query"],
                    country=fetch["country"],
                    category=fetch["category"],
                    gnews_query=fetch["gnews_query"],
                    gnews_category=fetch["gnews_category"]
                )
            except Exception as e:
                logging.error(f"Error fetching {fetch['label']}: {e}")

        # Run RSS fetch for major wire services and networks
        logging.info("--- Fetching RSS feeds ---")
        try:
            fetch_and_store_rss()
        except Exception as e:
            logging.error(f"Error fetching RSS feeds: {e}")

        # Run Bias Checker ONCE after all fetches
        logging.info("--- Retrying unrated outlets (Bias Checker) ---")
        try:
            retry_unrated_outlets()
        except Exception as e:
            logging.error(f"Error checking outlet bias: {e}")

        # Run AllSides sync once a month
        last_sync = get_last_allsides_sync()
        if last_sync is None or (datetime.utcnow() - last_sync).days >= 30:
            logging.info("--- Syncing AllSides bias ratings ---")
            try:
                sync_allsides_ratings()
                set_last_allsides_sync()
            except Exception as e:
                logging.error(f"Error syncing AllSides ratings: {e}")
        else:
            days_since = (datetime.utcnow() - last_sync).days
            logging.info(f"--- AllSides sync skipped ({days_since}/30 days) ---")

        logging.info("--- Running headline ranking ---")
        try:
            run_optional_headline_ranking()
        except Exception as e:
            logging.error(f"Error in headline ranking: {e}")

        logging.info("--- Publishing edition ---")
        try:
            publish_edition()
        except Exception as e:
            logging.error(f"Error publishing edition: {e}")

        logging.info("--- Processing current edition content ---")
        try:
            process_current_edition()
        except Exception as e:
            logging.error(f"Error processing edition content: {e}")

        logging.info("--- Exporting static site ---")
        try:
            run_optional_static_export()
        except Exception as e:
            logging.error(f"Error exporting static site: {e}")

        set_last_fetch_time()

        # Notify n8n pipeline is done — triggers Ollama machine suspend
        _notify_n8n()

    logging.info("=== Scheduled fetch run complete ===")


if __name__ == "__main__":
    logging.info("Scheduler starting up...")

    with app.app_context():
        db.create_all()
        # Only fetch on startup if enough time has passed
        if should_fetch_now():
            run_all_fetches()
        else:
            logging.info("Skipping startup fetch.")

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_all_fetches,
        trigger=CronTrigger(hour=SCHEDULE_HOURS, minute=0, timezone=TIMEZONE),
        id="fetch_job",
        name="Scheduled news fetch (America/New_York)",
        replace_existing=True
    )

    logging.info(f"Scheduler running. Fetching at {SCHEDULE_HOURS} in {TIMEZONE}.")
    scheduler.start()

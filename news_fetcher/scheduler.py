# muckscraperHeadlinesGoogleNEW/news_fetcher/scheduler.py
# news_fetcher/scheduler.py

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from aggregator import create_app, db
from aggregator.models import AppSetting
from news_fetcher.fetch_and_store_articles import fetch_and_store_articles, process_current_edition, review_ambiguous_grouping_matches, publish_edition, clear_stale_single_article_headlines, merge_count_maps
from news_fetcher.rss_fetcher import fetch_and_store_rss
from news_fetcher.headline_generator import generate_missing_headlines
from datetime import datetime, timedelta, timezone
import logging
import sys
import os
import requests
import json
from zoneinfo import ZoneInfo

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Fetch runs can be more frequent than full edition publishing.
FETCH_SCHEDULE_HOURS = os.environ.get("FETCH_SCHEDULE_HOURS") or "7,12,17,22"
FULL_PIPELINE_HOURS = os.environ.get("FULL_PIPELINE_HOURS") or "7,17"
from aggregator.country_config import get_config

_cfg = get_config()
TIMEZONE = _cfg["timezone"]

app = create_app()
SCRAPE_OUTCOME_HISTORY_KEY = "scrape_outcome_history_v1"
SCRAPE_OUTCOME_HISTORY_MAX_RUNS = 40


def get_scheduled_fetches():
    """Query active topics with fetch config from the DB.
    Must be called inside an active app context.
    """
    from aggregator.models import Topic
    return (
        Topic.query
        .filter(Topic.is_active == True, Topic.fetch_mode.isnot(None))
        .order_by(Topic.display_order)
        .all()
    )


def run_optional_headline_ranking():
    """
    Run the private ranking plugin when it exists locally.
    The open-source scheduler must not require ignored/private modules.
    """
    try:
        from news_fetcher.headline_ranker import run_headline_ranking
    except ImportError:
        logging.info("--- Headline ranking skipped (private plugin not installed) ---")
        return {
            "status": "skipped",
            "reason": "private plugin not installed",
        }

    run_headline_ranking()
    return {"status": "ok"}


def run_optional_static_export():
    """
    Export optional static output when an additional exporter is available.
    This keeps the main open-source stack working even when deployment-specific
    export code is not present.
    """
    try:
        from private_site.export_static import export_static_site
    except ImportError as e:
        logging.warning(
            "--- Optional static export skipped (%s). If static publishing is "
            "enabled in this deployment, make sure the extra exporter module "
            "and output mounts are available to the scheduler container. ---",
            e,
        )
        return {
            "status": "skipped",
            "reason": str(e),
        }

    export_static_site()
    return {"status": "ok"}


def _load_json_setting(key):
    setting = AppSetting.query.filter_by(key=key).first()
    if not setting or not setting.value:
        return None
    try:
        return json.loads(setting.value)
    except Exception:
        logging.warning("Could not parse JSON AppSetting for key=%s", key)
        return None


def _save_json_setting(key, value):
    payload = json.dumps(value, sort_keys=True)
    setting = AppSetting.query.filter_by(key=key).first()
    if setting:
        setting.value = payload
    else:
        db.session.add(AppSetting(key=key, value=payload))
    db.session.commit()


def _build_scrape_outcome_history_entry(run_metrics, headline_site_metrics):
    started_at = run_metrics.get("started_at")
    finished_at = run_metrics.get("finished_at")
    duration_seconds = None
    if started_at and finished_at:
        try:
            duration_seconds = int(
                (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
            )
        except Exception:
            duration_seconds = None

    return {
        "recorded_at": finished_at or datetime.utcnow().isoformat(),
        "status": run_metrics.get("status"),
        "duration_seconds": duration_seconds,
        "edition": headline_site_metrics.get("edition"),
        "run_scrape_statuses": dict(run_metrics.get("totals", {}).get("scrape_statuses", {})),
        "run_skipped": dict(run_metrics.get("totals", {}).get("skipped", {})),
        "headline_scrape": dict(headline_site_metrics.get("scrape", {}).get("articles_by_status", {})),
        "headline_readable_articles": headline_site_metrics.get("scrape", {}).get("readable_articles"),
        "headline_fully_read_articles": headline_site_metrics.get("scrape", {}).get("fully_read_articles"),
        "headline_blocked_articles": headline_site_metrics.get("scrape", {}).get("blocked_articles"),
        "stored_articles": run_metrics.get("totals", {}).get("stored"),
        "input_articles": run_metrics.get("totals", {}).get("input_articles"),
    }


def append_scrape_outcome_history(run_metrics, headline_site_metrics, max_runs=SCRAPE_OUTCOME_HISTORY_MAX_RUNS):
    history = _load_json_setting(SCRAPE_OUTCOME_HISTORY_KEY)
    if not isinstance(history, list):
        history = []

    history.append(_build_scrape_outcome_history_entry(run_metrics, headline_site_metrics))
    history = history[-max_runs:]
    _save_json_setting(SCRAPE_OUTCOME_HISTORY_KEY, history)
    return history


def build_headline_site_metrics():
    from aggregator.models import Edition, EditionStory

    latest_edition = Edition.query.filter_by(published=True).order_by(
        Edition.created_at.desc()
    ).first()
    if not latest_edition:
        return {
            "status": "no_published_edition",
            "recorded_at": datetime.utcnow().isoformat(),
        }

    edition_stories = latest_edition.edition_stories.order_by(EditionStory.rank).all()
    stories = [edition_story.story for edition_story in edition_stories]
    article_ids = set()
    outlet_ids = set()
    scrape_status_counts = {
        "success": 0,
        "fallback": 0,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
        "pending": 0,
    }
    multi_source_story_count = 0

    for story in stories:
        story_outlet_ids = set()

        for article in story.articles:
            article_ids.add(article.id)
            if article.outlet_id:
                story_outlet_ids.add(article.outlet_id)
                outlet_ids.add(article.outlet_id)

            scrape_status = (article.scrape_status or "pending").lower()
            scrape_status_counts[scrape_status] = scrape_status_counts.get(scrape_status, 0) + 1

        if len(story_outlet_ids) > 1:
            multi_source_story_count += 1

    return {
        "status": "ok",
        "recorded_at": datetime.utcnow().isoformat(),
        "edition": {
            "id": latest_edition.id,
            "date": latest_edition.date.isoformat(),
            "edition_type": latest_edition.edition_type,
            "created_at": latest_edition.created_at.isoformat() if latest_edition.created_at else None,
        },
        "story_count": len(stories),
        "article_count": len(article_ids),
        "outlet_count": len(outlet_ids),
        "multi_source_story_count": multi_source_story_count,
        "scrape": {
            "articles_by_status": scrape_status_counts,
            "readable_articles": scrape_status_counts.get("success", 0) + scrape_status_counts.get("fallback", 0),
            "fully_read_articles": scrape_status_counts.get("success", 0),
            "blocked_articles": scrape_status_counts.get("blocked", 0),
        },
    }


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


def _parse_schedule_hours(raw_hours):
    return sorted(int(hour.strip()) for hour in raw_hours.split(",") if hour.strip())


def _scheduled_hours():
    return _parse_schedule_hours(FETCH_SCHEDULE_HOURS)


def _full_pipeline_hours():
    return _parse_schedule_hours(FULL_PIPELINE_HOURS)


def _latest_scheduled_run_before(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    local_tz = ZoneInfo(TIMEZONE)
    local_now = now.astimezone(local_tz)

    candidates = []
    for day_offset in (0, -1):
        candidate_date = (local_now + timedelta(days=day_offset)).date()
        for hour in _scheduled_hours():
            candidates.append(
                datetime(
                    candidate_date.year,
                    candidate_date.month,
                    candidate_date.day,
                    hour,
                    0,
                    tzinfo=local_tz,
                )
            )

    eligible = [candidate for candidate in candidates if candidate <= local_now]
    if not eligible:
        return None
    return max(eligible).astimezone(timezone.utc)


def _latest_full_pipeline_run_before(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    local_tz = ZoneInfo(TIMEZONE)
    local_now = now.astimezone(local_tz)

    candidates = []
    for day_offset in (0, -1):
        candidate_date = (local_now + timedelta(days=day_offset)).date()
        for hour in _full_pipeline_hours():
            candidates.append(
                datetime(
                    candidate_date.year,
                    candidate_date.month,
                    candidate_date.day,
                    hour,
                    0,
                    tzinfo=local_tz,
                )
            )

    eligible = [candidate for candidate in candidates if candidate <= local_now]
    if not eligible:
        return None
    return max(eligible).astimezone(timezone.utc)


def should_fetch_now(now=None, last_fetch=None):
    """
    Returns True on startup only when the app missed a scheduled fetch slot.
    This avoids ad-hoc catch-up runs based on elapsed time alone.
    """
    if last_fetch is None:
        last_fetch = get_last_fetch_time()

    if not last_fetch:
        logging.info("No record of previous fetch. Initializing with a startup fetch.")
        return True

    if last_fetch.tzinfo is None:
        last_fetch = last_fetch.replace(tzinfo=timezone.utc)

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    latest_scheduled_run = _latest_scheduled_run_before(now=now)
    if latest_scheduled_run and last_fetch < latest_scheduled_run:
        logging.info(
            "Last fetch at %s missed scheduled run at %s, fetching on startup.",
            last_fetch.isoformat(),
            latest_scheduled_run.isoformat(),
        )
        return True

    logging.info(
        "Last fetch at %s is up to date with scheduled runs. Skipping startup fetch.",
        last_fetch.isoformat(),
    )
    return False


def should_run_full_pipeline(now=None, last_fetch=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if last_fetch is not None:
        if last_fetch.tzinfo is None:
            last_fetch = last_fetch.replace(tzinfo=timezone.utc)
        latest_full_slot = _latest_full_pipeline_run_before(now=now)
        return bool(latest_full_slot and last_fetch < latest_full_slot)

    local_now = now.astimezone(ZoneInfo(TIMEZONE))
    return local_now.hour in _full_pipeline_hours()


def _notify_n8n():
    webhook = os.getenv("N8N_WEBHOOK_URL")
    if not webhook:
        return
    try:
        from news_fetcher.summarizer import check_ollama_status

        if not check_ollama_status():
            logging.info("  [n8n] Ollama already unreachable, skipping suspend webhook")
            return

        response = requests.post(webhook, timeout=5)
        response.raise_for_status()
        logging.info(
            "  [n8n] Webhook fired — Ollama suspend sequence triggered (status %s)",
            response.status_code,
        )
    except Exception as e:
        logging.warning(f"  [n8n] Webhook failed ({e}) — continuing normally")


def _check_ollama_status_for_report(ollama_state, label):
    from news_fetcher.summarizer import check_ollama_status

    try:
        is_up = check_ollama_status()
    except Exception as e:
        logging.warning("  [Ollama] Health check failed during %s (%s)", label, e)
        is_up = False

    checked_at = datetime.utcnow().isoformat()
    ollama_state["checks"].append({
        "at": checked_at,
        "label": label,
        "up": is_up,
    })
    if not is_up:
        ollama_state["went_down_during_run"] = True
    return is_up


def _build_fetch_report(run_metrics, headline_site_metrics, ollama_state):
    started_at = datetime.fromisoformat(run_metrics["started_at"])
    finished_at = datetime.fromisoformat(run_metrics["finished_at"])
    duration_seconds = int((finished_at - started_at).total_seconds())
    run_totals = run_metrics.get("totals", {})
    headline_scrape = dict(headline_site_metrics.get("scrape", {}).get("articles_by_status", {}))
    edition = headline_site_metrics.get("edition", {}) or {}
    headline_metrics_label = "Latest published edition stats"

    return {
        "status": run_metrics.get("status", "unknown"),
        "started_at": run_metrics["started_at"],
        "finished_at": run_metrics["finished_at"],
        "duration_seconds": duration_seconds,
        # Backward-compatible top-level summary fields for n8n formatters.
        "input_articles": run_totals.get("input_articles", 0),
        "stored_articles": run_totals.get("stored", 0),
        "new_outlets": run_totals.get("new_outlets", 0),
        "stories_touched": run_totals.get("stories_touched", 0),
        "run_scrape_statuses": dict(run_totals.get("scrape_statuses", {})),
        "headline_metrics_label": headline_metrics_label,
        "headline_scrape": headline_scrape,
        "headline_readable_articles": headline_site_metrics.get("scrape", {}).get("readable_articles", 0),
        "headline_fully_read_articles": headline_site_metrics.get("scrape", {}).get("fully_read_articles", 0),
        "headline_blocked_articles": headline_site_metrics.get("scrape", {}).get("blocked_articles", 0),
        "edition": {
            "id": edition.get("id"),
            "date": edition.get("date"),
            "edition_type": edition.get("edition_type"),
        },
        "ollama": ollama_state,
        "run_metrics": run_metrics,
        "headline_metrics": headline_site_metrics,
        "latest_published_edition_metrics": headline_site_metrics,
    }


def _notify_fetch_report(report_payload):
    webhook = os.getenv("N8N_FETCH_REPORT_WEBHOOK_URL")
    if not webhook:
        return

    try:
        response = requests.post(webhook, json=report_payload, timeout=10)
        response.raise_for_status()
        logging.info(
            "  [n8n] Fetch report webhook fired (status %s)",
            response.status_code,
        )
    except Exception as e:
        logging.warning(f"  [n8n] Fetch report webhook failed ({e}) — continuing normally")


def _broadcast_step(status, current_step, started_at, steps_completed, steps_remaining, ollama_up, article_progress=None):
    payload = {
        "status": status,
        "current_step": current_step,
        "started_at": started_at.isoformat() if started_at else None,
        "steps_completed": steps_completed,
        "steps_remaining": steps_remaining,
        "ollama_up": ollama_up,
        "article_progress": article_progress,
    }
    _save_json_setting("pipeline_live_status", payload)


def _clear_pipeline_status(status="idle", finished_at=None):
    payload = {
        "status": status,
        "current_step": None,
        "started_at": None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "steps_completed": [],
        "steps_remaining": [],
        "ollama_up": None,
        "article_progress": None,
    }
    _save_json_setting("pipeline_live_status", payload)


def run_all_fetches(run_full_pipeline=True):
    logging.info("=== Starting scheduled fetch run ===")
    run_started_at = datetime.utcnow()
    steps_completed = []
    
    SCHEDULED_FETCHES = get_scheduled_fetches()
    if SCHEDULED_FETCHES:
        summary = ", ".join(f"{f.name} ({f.fetch_mode})" for f in SCHEDULED_FETCHES)
        logging.info("Scheduled fetches (%d): %s", len(SCHEDULED_FETCHES), summary)
    else:
        logging.warning(
            "No topics with fetch_mode configured — scheduled topic fetches will be skipped. "
            "Run seed_topics.py or restart the app container to seed topics."
        )
    steps_remaining = [f"Fetching: {f.display_label}" for f in SCHEDULED_FETCHES]
    steps_remaining.append("Fetching RSS feeds")
    steps_remaining.append("Generating missing headlines")

    if run_full_pipeline:
        steps_remaining.extend([
            "Headline ranking",
            "Publishing edition",
            "Processing current edition",
            "Exporting static site"
        ])
    
    _broadcast_step("running", "Initializing...", run_started_at, steps_completed, steps_remaining, None)

    with app.app_context():
        ollama_state = {
            "up_at_start": False,
            "up_at_end": False,
            "went_down_during_run": False,
            "checks": [],
        }
        run_metrics = {
            "status": "ok",
            "started_at": datetime.utcnow().isoformat(),
            "topics": {},
            "rss": None,
            "totals": {
                "input_articles": 0,
                "stored": 0,
                "new_outlets": 0,
                "stories_touched": 0,
                "skipped": {},
                "scrape_statuses": {},
            },
            "steps": {},
        }
        ollama_state["up_at_start"] = _check_ollama_status_for_report(ollama_state, "run_start")
        touched_story_ids = set()

        # Fetch all categories
        for fetch in SCHEDULED_FETCHES:
            step_name = f"Fetching: {fetch.display_label}"
            if step_name in steps_remaining:
                steps_remaining.remove(step_name)

            _broadcast_step("running", step_name, run_started_at, steps_completed, steps_remaining, ollama_state.get("up_at_start"))
            def create_progress_cb(s_name, s_comp, s_rem):
                def cb(current, total):
                    _broadcast_step("running", s_name, run_started_at, s_comp, s_rem, ollama_state.get("up_at_start"), article_progress={"current": current, "total": total})
                return cb

            progress_cb = create_progress_cb(step_name, list(steps_completed), list(steps_remaining))
            logging.info(f"--- {step_name} ---")
            try:
                topic_metrics = fetch_and_store_articles(
                    fetch.name,
                    mode=fetch.fetch_mode,
                    query=fetch.fetch_query,
                    country=fetch.fetch_country,
                    category=fetch.fetch_category,
                    gnews_query=fetch.gnews_query,
                    gnews_category=fetch.gnews_category,
                    progress_cb=progress_cb
                )
                run_metrics["topics"][fetch.display_label] = topic_metrics
                for provider_metrics in topic_metrics.get("providers", {}).values():
                    run_metrics["totals"]["input_articles"] += provider_metrics.get("input_articles", 0)
                    run_metrics["totals"]["stored"] += provider_metrics.get("stored", 0)
                    run_metrics["totals"]["new_outlets"] += provider_metrics.get("new_outlets", 0)
                    run_metrics["totals"]["stories_touched"] += provider_metrics.get("stories_touched", 0)
                    touched_story_ids.update(provider_metrics.get("story_ids", []))
                    merge_count_maps(run_metrics["totals"]["skipped"], provider_metrics.get("skipped"))
                    merge_count_maps(run_metrics["totals"]["scrape_statuses"], provider_metrics.get("scrape_statuses"))
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error fetching {fetch.display_label}: {e}")
                run_metrics["status"] = "partial_error"
                run_metrics["topics"][fetch.display_label] = {
                    "status": "error",
                    "reason": str(e),
                }
            steps_completed.append(step_name)

        # Run RSS fetch for major wire services and networks
        step_name = "Fetching RSS feeds"
        if step_name in steps_remaining:
            steps_remaining.remove(step_name)
        _broadcast_step("running", step_name, run_started_at, steps_completed, steps_remaining, ollama_state.get("up_at_start"))
        def rss_progress_cb(current, total):
            _broadcast_step("running", step_name, run_started_at, steps_completed, steps_remaining, ollama_state.get("up_at_start"), article_progress={"current": current, "total": total})

        logging.info("--- Fetching RSS feeds ---")
        try:
            rss_metrics = fetch_and_store_rss(progress_cb=rss_progress_cb)
            run_metrics["rss"] = rss_metrics
            run_metrics["totals"]["input_articles"] += rss_metrics.get("input_articles", 0)
            run_metrics["totals"]["stored"] += rss_metrics.get("stored", 0)
            run_metrics["totals"]["new_outlets"] += rss_metrics.get("new_outlets", 0)
            run_metrics["totals"]["stories_touched"] += rss_metrics.get("stories_touched", 0)
            touched_story_ids.update(rss_metrics.get("story_ids", []))
            merge_count_maps(run_metrics["totals"]["skipped"], rss_metrics.get("skipped"))
            merge_count_maps(run_metrics["totals"]["scrape_statuses"], rss_metrics.get("scrape_statuses"))
        except Exception as e:
            db.session.rollback()
            logging.error(f"Error fetching RSS feeds: {e}")
            run_metrics["status"] = "partial_error"
            run_metrics["rss"] = {
                "status": "error",
                "reason": str(e),
            }
        steps_completed.append(step_name)

        # NEW: Generate missing headlines for all multi-article stories created during this run
        step_name = "Generating missing headlines"
        if step_name in steps_remaining:
            steps_remaining.remove(step_name)
        _broadcast_step("running", step_name, run_started_at, steps_completed, steps_remaining, ollama_state.get("up_at_start"))
        logging.info("--- Generating missing headlines (Batch Pass) ---")
        try:
            generate_missing_headlines(story_ids=touched_story_ids)
            run_metrics["steps"]["generate_missing_headlines"] = {"status": "ok"}
        except Exception as e:
            db.session.rollback()
            logging.error(f"Error generating missing headlines: {e}")
            run_metrics["status"] = "partial_error"
            run_metrics["steps"]["generate_missing_headlines"] = {"status": "error", "reason": str(e)}
        steps_completed.append(step_name)

        if run_full_pipeline:
            step_name = "Headline ranking"
            if step_name in steps_remaining:
                steps_remaining.remove(step_name)
            _broadcast_step("running", step_name, run_started_at, steps_completed, steps_remaining, ollama_state.get("up_at_start"))

            logging.info("--- Running headline ranking ---")
            _check_ollama_status_for_report(ollama_state, "before_headline_ranking")
            try:
                cleared = clear_stale_single_article_headlines()
                run_metrics["steps"]["clear_stale_single_article_headlines"] = {
                    "status": "ok",
                    "cleared": cleared,
                }
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error clearing stale single-article headlines: {e}")
                run_metrics["status"] = "partial_error"
                run_metrics["steps"]["clear_stale_single_article_headlines"] = {"status": "error", "reason": str(e)}
            try:
                run_metrics["steps"]["review_ambiguous_grouping_matches"] = review_ambiguous_grouping_matches()
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error reviewing ambiguous grouping matches: {e}")
                run_metrics["status"] = "partial_error"
                run_metrics["steps"]["review_ambiguous_grouping_matches"] = {"status": "error", "reason": str(e)}
            try:
                run_metrics["steps"]["headline_ranking"] = run_optional_headline_ranking()
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error in headline ranking: {e}")
                run_metrics["status"] = "partial_error"
                run_metrics["steps"]["headline_ranking"] = {"status": "error", "reason": str(e)}
            steps_completed.append(step_name)

            step_name = "Publishing edition"
            if step_name in steps_remaining:
                steps_remaining.remove(step_name)
            _broadcast_step("running", step_name, run_started_at, steps_completed, steps_remaining, ollama_state.get("up_at_start"))

            logging.info("--- Publishing edition ---")
            try:
                run_metrics["steps"]["publish_edition"] = publish_edition()
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error publishing edition: {e}")
                run_metrics["status"] = "partial_error"
                run_metrics["steps"]["publish_edition"] = {"status": "error", "reason": str(e)}
            steps_completed.append(step_name)

            step_name = "Processing current edition"
            if step_name in steps_remaining:
                steps_remaining.remove(step_name)
            _broadcast_step("running", step_name, run_started_at, steps_completed, steps_remaining, ollama_state.get("up_at_start"))

            logging.info("--- Processing current edition content ---")
            _check_ollama_status_for_report(ollama_state, "before_process_current_edition")
            try:
                run_metrics["steps"]["process_current_edition"] = process_current_edition()
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error processing edition content: {e}")
                run_metrics["status"] = "partial_error"
                run_metrics["steps"]["process_current_edition"] = {"status": "error", "reason": str(e)}
            steps_completed.append(step_name)

            step_name = "Exporting static site"
            if step_name in steps_remaining:
                steps_remaining.remove(step_name)
            _broadcast_step("running", step_name, run_started_at, steps_completed, steps_remaining, ollama_state.get("up_at_start"))

            logging.info("--- Exporting static site ---")
            try:
                run_metrics["steps"]["static_export"] = run_optional_static_export()
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error exporting static site: {e}")
                run_metrics["status"] = "partial_error"
                run_metrics["steps"]["static_export"] = {"status": "error", "reason": str(e)}
            steps_completed.append(step_name)
        else:
            run_metrics["steps"]["clear_stale_single_article_headlines"] = {"status": "skipped", "reason": "fetch_only_run"}
            run_metrics["steps"]["review_ambiguous_grouping_matches"] = {"status": "skipped", "reason": "fetch_only_run"}
            run_metrics["steps"]["headline_ranking"] = {"status": "skipped", "reason": "fetch_only_run"}
            run_metrics["steps"]["publish_edition"] = {"status": "skipped", "reason": "fetch_only_run"}
            run_metrics["steps"]["process_current_edition"] = {"status": "skipped", "reason": "fetch_only_run"}
            run_metrics["steps"]["static_export"] = {"status": "skipped", "reason": "fetch_only_run"}

        set_last_fetch_time()
        run_metrics["finished_at"] = datetime.utcnow().isoformat()
        ollama_state["up_at_end"] = _check_ollama_status_for_report(ollama_state, "run_end")
        _save_json_setting("last_run_metrics", run_metrics)
        headline_site_metrics = build_headline_site_metrics()
        _save_json_setting("last_headline_site_metrics", headline_site_metrics)
        append_scrape_outcome_history(run_metrics, headline_site_metrics)
        fetch_report = _build_fetch_report(run_metrics, headline_site_metrics, ollama_state)
        _save_json_setting("last_fetch_report", fetch_report)
        logging.info(
            "[Metrics] Run stored=%s input=%s latest_edition=%s %s",
            run_metrics["totals"]["stored"],
            run_metrics["totals"]["input_articles"],
            headline_site_metrics.get("edition", {}).get("date"),
            headline_site_metrics.get("edition", {}).get("edition_type"),
        )
        _notify_fetch_report(fetch_report)

        # Notify n8n pipeline is done — triggers Ollama machine suspend
        _notify_n8n()
        
        _clear_pipeline_status(status=run_metrics["status"], finished_at=datetime.fromisoformat(run_metrics["finished_at"]))

    logging.info("=== Scheduled fetch run complete ===")


if __name__ == "__main__":
    logging.info("Scheduler starting up...")

    with app.app_context():
        db.create_all()
        # Only fetch on startup if enough time has passed
        if should_fetch_now():
            run_all_fetches(
                run_full_pipeline=should_run_full_pipeline(
                    last_fetch=get_last_fetch_time(),
                )
            )
        else:
            logging.info("Skipping startup fetch.")

    scheduler = BlockingScheduler()
    scheduler.add_job(
        lambda: run_all_fetches(run_full_pipeline=should_run_full_pipeline()),
        trigger=CronTrigger(hour=FETCH_SCHEDULE_HOURS, minute=0, timezone=TIMEZONE),
        id="fetch_job",
        name=f"Scheduled news fetch ({TIMEZONE})",
        replace_existing=True
    )

    logging.info(
        "Scheduler running. Fetching at %s and running full pipeline at %s in %s.",
        FETCH_SCHEDULE_HOURS,
        FULL_PIPELINE_HOURS,
        TIMEZONE,
    )
    scheduler.start()

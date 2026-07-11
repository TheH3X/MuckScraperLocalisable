import logging
import json
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Blueprint, current_app, render_template, request, redirect, url_for, jsonify, flash
from flask_login import login_required
from sqlalchemy import case, func, or_
from aggregator import db
from aggregator.models import AppSetting, Article, Outlet, Story, Topic, RawArticlePayload
from aggregator.search import SearchUnavailableError, reindex_all, search_story_ids
from aggregator.story_view import apply_aggregator_filter

logger = logging.getLogger(__name__)

admin = Blueprint("admin", __name__)
SEARCH_REINDEX_STATUS_KEY = "search_reindex_status_v1"
search_reindex_lock = threading.Lock()
ai_task_lock = threading.Lock()
SCRAPE_STATUS_FILTERS = ("success", "fallback", "blocked", "failed", "skipped", "pending")


def _get_active_topics():
    """Return active Topic rows ordered for sidebar display."""
    return Topic.query.filter_by(is_active=True).order_by(Topic.display_order).all()


def _get_fetch_topics():
    """Return active Topic rows that have a scheduled fetch configured."""
    return (
        Topic.query
        .filter(Topic.is_active == True, Topic.fetch_mode.isnot(None))
        .order_by(Topic.display_order)
        .all()
    )


def _load_json_setting(key):
    setting = AppSetting.query.filter_by(key=key).first()
    if not setting or not setting.value:
        return None
    try:
        return json.loads(setting.value)
    except Exception:
        logger.warning("Failed to parse AppSetting JSON for key=%s", key)
        return {
            "status": "parse_error",
            "raw_value": setting.value,
        }


def _save_json_setting(key, payload):
    setting = AppSetting.query.filter_by(key=key).first()
    if setting:
        setting.value = json.dumps(payload)
    else:
        db.session.add(AppSetting(key=key, value=json.dumps(payload)))
    db.session.commit()


def _run_background_tool(app, name, steps, func, *args, **kwargs):
    with app.app_context():
        from news_fetcher.scheduler import _broadcast_step, _clear_pipeline_status
        from datetime import datetime
        started_at = datetime.utcnow()
        try:
            _broadcast_step("running", f"Running: {name}", started_at, [], steps, None)
            func(*args, **kwargs)
            _clear_pipeline_status("success", datetime.utcnow())
        except Exception as e:
            logger.exception(f"Background tool error ({name}): {e}")
            _clear_pipeline_status("error", datetime.utcnow())


def dispatch_background_tool(name, steps, func, *args, **kwargs):
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_run_background_tool,
        args=(app, name, steps, func) + args,
        kwargs=kwargs,
        daemon=True
    )
    thread.start()


def _search_reindex_status_payload():
    payload = _load_json_setting(SEARCH_REINDEX_STATUS_KEY)
    if payload:
        return payload
    return {
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "message": "Search index has not been rebuilt in this app session yet.",
        "story_documents": None,
        "article_documents": None,
    }


def _ai_task_status_key(task_type, resource_id):
    return f"ai_task_status_v1:{task_type}:{resource_id}"


def _ai_task_default_message(task_type):
    messages = {
        "story_summary": "Story summary has not been started in this app session yet.",
        "story_deep_report": "Story analysis has not been started in this app session yet.",
        "article_summary": "Article summary has not been started in this app session yet.",
    }
    return messages.get(task_type, "AI task has not been started in this app session yet.")


def _ai_task_status_payload(task_type, resource_id):
    payload = _load_json_setting(_ai_task_status_key(task_type, resource_id))
    if payload:
        return payload
    return {
        "status": "idle",
        "task_type": task_type,
        "resource_id": resource_id,
        "started_at": None,
        "finished_at": None,
        "message": _ai_task_default_message(task_type),
    }


def _save_ai_task_status(task_type, resource_id, payload):
    base_payload = {
        "task_type": task_type,
        "resource_id": resource_id,
    }
    base_payload.update(payload)
    _save_json_setting(_ai_task_status_key(task_type, resource_id), base_payload)


def _run_ai_task(app, task_type, resource_id):
    with app.app_context():
        started_at = _ai_task_status_payload(task_type, resource_id).get("started_at")

        try:
            from news_fetcher.summarizer import (
                summarize_story,
                summarize_article,
                generate_deep_report,
                check_ollama_status,
            )

            if not check_ollama_status():
                raise RuntimeError("Ollama is offline.")

            if task_type == "story_summary":
                story = Story.query.get_or_404(resource_id)
                summary = summarize_story(story)
                if not summary:
                    raise RuntimeError("No story summary was generated.")
                story.summary = summary
                db.session.commit()
                message = "Story summary completed successfully."
            elif task_type == "story_deep_report":
                story = Story.query.get_or_404(resource_id)
                if len(story.articles) < 2:
                    raise RuntimeError("Story analysis requires at least two articles.")
                report = generate_deep_report(story)
                if not report:
                    raise RuntimeError("No story analysis was generated.")
                story.deep_report = report
                db.session.commit()
                message = "Story analysis completed successfully."
            elif task_type == "article_summary":
                article = Article.query.get_or_404(resource_id)
                summary = summarize_article(article)
                if not summary:
                    raise RuntimeError("No article summary was generated.")
                article.summary = summary
                db.session.commit()
                message = "Article summary completed successfully."
            else:
                raise RuntimeError(f"Unknown AI task type: {task_type}")

            _save_ai_task_status(
                task_type,
                resource_id,
                {
                    "status": "success",
                    "started_at": started_at,
                    "finished_at": datetime.utcnow().isoformat(),
                    "message": message,
                },
            )
        except Exception as e:
            db.session.rollback()
            logger.exception("Async AI task error type=%s resource_id=%s: %s", task_type, resource_id, e)
            _save_ai_task_status(
                task_type,
                resource_id,
                {
                    "status": "error",
                    "started_at": started_at,
                    "finished_at": datetime.utcnow().isoformat(),
                    "message": str(e),
                },
            )


def _run_search_reindex(app):
    with app.app_context():
        try:
            started_at = _search_reindex_status_payload().get("started_at")
            counts = reindex_all()
            _save_json_setting(
                SEARCH_REINDEX_STATUS_KEY,
                {
                    "status": "success",
                    "started_at": started_at,
                    "finished_at": datetime.utcnow().isoformat(),
                    "message": "Meilisearch reindex completed successfully.",
                    "story_documents": counts["story_documents"],
                    "article_documents": counts["article_documents"],
                },
            )
            logger.info(
                "[Search] Async reindex complete stories=%s articles=%s",
                counts["story_documents"],
                counts["article_documents"],
            )
        except Exception as e:
            logger.exception(f"Async search reindex error: {e}")
            started_at = _search_reindex_status_payload().get("started_at")
            _save_json_setting(
                SEARCH_REINDEX_STATUS_KEY,
                {
                    "status": "error",
                    "started_at": started_at,
                    "finished_at": datetime.utcnow().isoformat(),
                    "message": str(e),
                    "story_documents": None,
                    "article_documents": None,
                },
            )


def article_domain(url):
    if not url:
        return None
    domain = urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


def story_bias_totals(story):
    """Compute left/center/right article counts for a story.

    Uses article.bias_score exclusively. A None value means the article belongs
    to a non-political topic and bias was intentionally suppressed — do NOT fall
    back to outlet.bias_score, as that would re-introduce bias for tech/sports/etc.
    """
    counts = {
        "left": 0,
        "center": 0,
        "right": 0,
    }
    for article in story.articles:
        score = article.bias_score
        if score is None:
            continue  # Intentionally suppressed — do not fall back to outlet score
        if score <= 2.5:
            counts["left"] += 1
        elif score <= 3.5:
            counts["center"] += 1
        else:
            counts["right"] += 1

    story.left_bias_count = counts["left"]
    story.center_bias_count = counts["center"]
    story.right_bias_count = counts["right"]
    story.bias_gap = abs(counts["left"] - counts["right"])
    if counts["left"] > counts["right"]:
        story.enrichment_direction = "right"
    elif counts["right"] > counts["left"]:
        story.enrichment_direction = "left"
    else:
        story.enrichment_direction = None


def redirect_to_articles(label=None, scrape_status=None):
    next_url = request.form.get("next", "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    params = {}
    if label:
        params["topic"] = label
    if scrape_status:
        params["scrape_status"] = scrape_status
    return redirect(url_for("admin.list_articles", **params))


def apply_scrape_result(article, result):
    article.scrape_status = result.status
    article.scrape_method = result.method
    article.scrape_failure_reason = result.failure_reason
    article.scrape_http_status = result.http_status
    article.scrape_audited = False
    if result.content:
        article.content = result.content


@admin.route("/fetch-page")
@login_required
def fetch_page():
    fetch_topics = _get_fetch_topics()
    return render_template("fetch.html", fetch_presets=fetch_topics)


@admin.route("/tools")
@login_required
def tools_page():
    return render_template("admin_tools.html")


@admin.route("/run-full-pipeline", methods=["POST"])
@login_required
def run_full_pipeline():
    def _run_full(app):
        with app.app_context():
            try:
                from news_fetcher.scheduler import run_all_fetches
                run_all_fetches(run_full_pipeline=True)
            except Exception as e:
                logger.exception(f"Run full pipeline error: {e}")
                from news_fetcher.scheduler import _clear_pipeline_status
                from datetime import datetime
                _clear_pipeline_status("error", datetime.utcnow())

    app = current_app._get_current_object()
    thread = threading.Thread(target=_run_full, args=(app,), daemon=True)
    thread.start()
    return redirect(url_for("admin.tools_page"))


@admin.route("/articles")
@login_required
def list_articles(per_page=25, force_multi=False):
    active_label = request.args.get("topic", None)
    active_scrape_status = request.args.get("scrape_status", "").strip().lower() or None
    active_search_query = request.args.get("q", "").strip() or None
    page = request.args.get("page", 1, type=int)
    show_single = request.args.get("show_single", "false") == "true"
    story_id = request.args.get("story_id", type=int)

    if active_scrape_status not in SCRAPE_STATUS_FILTERS:
        active_scrape_status = None

    if story_id:
        return redirect(url_for("public.view_story", story_id=story_id))

    if force_multi:
        show_single = False

    query = Story.query.join(Article).group_by(Story.id)
    meili_story_ids = None

    if not show_single:
        query = query.having(func.count(Article.id) > 1)

    if active_label:
        topic = Topic.query.filter_by(name=active_label).first()
        if not topic:
            topic = Topic.query.filter_by(label=active_label).first()
        if topic:
            query = query.filter(Story.topics.contains(topic))
        else:
            query = query.filter(False)

    if active_scrape_status:
        query = query.filter(Story.articles.any(Article.scrape_status == active_scrape_status))

    if active_search_query:
        try:
            meili_story_ids = search_story_ids(active_search_query)
        except SearchUnavailableError as exc:
            logger.warning("Meilisearch unavailable, falling back to SQL search: %s", exc)

        if meili_story_ids is not None:
            if meili_story_ids:
                query = query.filter(Story.id.in_(meili_story_ids))
            else:
                query = query.filter(False)
        else:
            # Keep admin search fast by limiting it to shorter text fields.
            # The previous full-text search scanned large summary/content columns
            # and could time out on short terms like "ICE".
            search_terms = [term for term in active_search_query.split() if term]
            if not search_terms:
                search_terms = [active_search_query]
            for term in search_terms:
                like_term = f"%{term}%"
                query = query.filter(
                    or_(
                        Story.title.ilike(like_term),
                        Story.headline.ilike(like_term),
                        Story.topics.any(Topic.name.ilike(like_term)),
                        Story.articles.any(Article.title.ilike(like_term)),
                        Story.articles.any(Article.source.ilike(like_term)),
                        Story.articles.any(Article.outlet.has(Outlet.name.ilike(like_term))),
                    )
                )

    if meili_story_ids:
        order_by = [
            case({story_id: index for index, story_id in enumerate(meili_story_ids)}, value=Story.id),
            func.max(Article.date).desc(),
        ]
    else:
        order_by = [func.max(Article.date).desc()]

    pagination = query.order_by(*order_by).paginate(
        page=page, per_page=per_page, error_out=False
    )

    stories = pagination.items if pagination else []
    total_pages = pagination.pages if pagination else 0

    for story in stories:
        apply_aggregator_filter(story)
        story_bias_totals(story)

    return render_template(
        "articles.html",
        stories=stories,
        topics=_get_active_topics(),
        active_label=active_label,
        active_scrape_status=active_scrape_status,
        active_search_query=active_search_query,
        scrape_status_filters=SCRAPE_STATUS_FILTERS,
        page=page,
        total_pages=total_pages,
        show_single=show_single,
        is_multi_view=force_multi
    )


@admin.route("/multi-stories")
@login_required
def multi_article_stories():
    return list_articles(per_page=50, force_multi=True)


@admin.route("/fetch", methods=["POST"])
@login_required
def fetch_articles():
    mode = request.form.get("mode", "top").strip() or "top"
    query = request.form.get("query", "").strip() or None
    country = request.form.get("country", "").strip() or None
    category = request.form.get("category", "").strip() or None
    label = request.form.get("label", "").strip() or None
    scrape_status = request.form.get("scrape_status", "").strip() or None
    gnews_query = request.form.get("gnews_query", "").strip() or None
    gnews_category = request.form.get("gnews_category", "").strip() or None

    kwargs = {
        "topic_name": label or "Custom",
        "mode": mode,
        "query": query,
        "country": country,
        "category": category,
        "gnews_query": gnews_query,
        "gnews_category": gnews_category,
    }

    from news_fetcher.fetch_and_store_articles import fetch_and_store_articles
    dispatch_background_tool(
        f"Fetch {label or 'Custom'}", 
        [f"Fetching and parsing {label or 'Custom'} articles", "Generating embeddings and summaries", "Ranking headlines"], 
        fetch_and_store_articles, 
        **kwargs
    )

    return redirect_to_articles(label, scrape_status)


@admin.route("/enrich-story-balance/<int:story_id>", methods=["POST"])
@login_required
def enrich_story_balance(story_id):
    story = Story.query.get_or_404(story_id)
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.rss_fetcher import enrich_story_with_opposite_feeds
        from news_fetcher.fetch_and_store_articles import retry_unrated_outlets

        metrics = enrich_story_with_opposite_feeds(story, max_articles_per_story=3)
        if metrics.get("stored", 0) > 0:
            retry_unrated_outlets()
        logger.info(
            "[Admin] Story balance enrichment story_id=%s direction=%s stored=%s matched=%s status=%s",
            story_id,
            metrics.get("direction"),
            metrics.get("stored", 0),
            metrics.get("matched_articles", 0),
            metrics.get("status"),
        )
    except Exception as e:
        logger.exception(f"Story balance enrichment error for story {story_id}: {e}")
    return redirect_to_articles(label, scrape_status)


@admin.route("/summarize/<int:story_id>", methods=["POST"])
@login_required
def summarize_story_route(story_id):
    story = Story.query.get_or_404(story_id)
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.summarizer import summarize_story, check_ollama_status
        if check_ollama_status():
            summary = summarize_story(story)
            if summary:
                story.summary = summary
                db.session.commit()
    except Exception as e:
        logger.error(f"Summarization error: {e}")
    return redirect_to_articles(label, scrape_status)


@admin.route("/summarize-article/<int:article_id>", methods=["POST"])
@login_required
def summarize_article_route(article_id):
    article = Article.query.get_or_404(article_id)
    try:
        from news_fetcher.summarizer import summarize_article, check_ollama_status
        if check_ollama_status():
            summary = summarize_article(article)
            if summary:
                article.summary = summary
            db.session.commit()
    except Exception as e:
        logger.error(f"Article summarization error: {e}")
    return redirect(url_for("public.view_article", article_id=article_id))


@admin.route("/rerank-outlet/<int:outlet_id>", methods=["POST"])
@login_required
def rerank_outlet(outlet_id):
    from aggregator.models import Outlet
    outlet = Outlet.query.get_or_404(outlet_id)
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.outlet_bias_llm import get_outlet_bias_from_llm
        bias_score = get_outlet_bias_from_llm(outlet.name)
        if bias_score is not None:
            outlet.bias_score = bias_score
            for article in outlet.articles:
                # Only update articles that already have a bias_score.
                # A None score means bias was intentionally suppressed for that
                # article's topic (e.g. tech, sports) — don't re-introduce it.
                if article.bias_score is not None:
                    article.bias_score = bias_score
            db.session.commit()
    except Exception as e:
        logger.error(f"Re-rank error: {e}")
    return redirect_to_articles(label, scrape_status)


@admin.route("/rate-article/<int:article_id>", methods=["POST"])
@login_required
def rate_article(article_id):
    article = Article.query.get_or_404(article_id)
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.outlet_bias_llm import get_article_bias_from_llm
        bias_score = get_article_bias_from_llm(article.title, article.content)
        if bias_score is not None:
            article.bias_score = bias_score
            db.session.commit()
    except Exception as e:
        logger.error(f"Article rating error: {e}")
    return redirect_to_articles(label, scrape_status)


@admin.route("/ollama-catchup", methods=["POST"])
@login_required
def ollama_catchup_route():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    from news_fetcher.fetch_and_store_articles import ollama_catchup
    dispatch_background_tool("AI Catch-up", ["Auditing scrapes", "Generating embeddings", "Generating headlines", "Regrouping stories", "Retrying unrated outlets"], ollama_catchup)
    return redirect_to_articles(label, scrape_status)


@admin.route("/scrape-article/<int:article_id>", methods=["POST"])
@login_required
def scrape_article_route(article_id):
    article = Article.query.get_or_404(article_id)
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.scraper import scrape_article
        result = scrape_article(article.url, fallback_content=article.content, force=True)
        apply_scrape_result(article, result)
        db.session.commit()
    except Exception as e:
        logger.error(f"Scrape error: {e}")
    return redirect_to_articles(label, scrape_status)


@admin.route("/scrape-all-missing", methods=["POST"])
@login_required
def scrape_all_missing():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    
    def _do_scrape():
        from news_fetcher.scraper import scrape_article, should_auto_rescrape_article
        candidates = Article.query.filter(
            (Article.content == None) |
            (Article.content == "") |
            (db.func.length(Article.content) < 500) |
            (Article.scrape_status.in_(["pending", "failed"]))
        ).order_by(Article.fetched_at.desc()).limit(100).all()
        eligible = [article for article in candidates if should_auto_rescrape_article(article)][:20]
        if eligible:
            for article in eligible:
                result = scrape_article(article.url, fallback_content=article.content)
                apply_scrape_result(article, result)
            db.session.commit()

    dispatch_background_tool("Scrape Missing", ["Scraping missing article contents"], _do_scrape)
    return redirect_to_articles(label, scrape_status)


@admin.route("/rescrape-article/<int:article_id>", methods=["POST"])
@login_required
def rescrape_article_route(article_id):
    article = Article.query.get_or_404(article_id)
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.scraper import scrape_article
        result = scrape_article(article.url, fallback_content=article.content, force=True)
        apply_scrape_result(article, result)
        db.session.commit()
    except Exception as e:
        logger.error(f"Rescrape error: {e}")
    return redirect_to_articles(label, scrape_status)
@admin.route("/force-regroup", methods=["POST"])
@login_required
def force_regroup():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    from news_fetcher.fetch_and_store_articles import force_regroup_all
    dispatch_background_tool("Rebuild Story Grouping", ["Re-evaluating embeddings", "Regrouping stories"], force_regroup_all)
    return redirect_to_articles(label, scrape_status)


@admin.route("/force-resummarize", methods=["POST"])
@login_required
def force_resummarize():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    from news_fetcher.fetch_and_store_articles import force_resummarize_all
    dispatch_background_tool("Rebuild Story Summaries", ["Regenerating summaries for stories"], force_resummarize_all)
    return redirect_to_articles(label, scrape_status)


@admin.route("/wake-ollama", methods=["POST"])
@login_required
def wake_ollama():
    import os
    import wakeonlan
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        mac = os.environ.get("OLLAMA_MAC", "")
        if mac:
            wakeonlan.send_magic_packet(mac)
    except Exception as e:
        logger.error(f"WoL error: {e}")
    return redirect_to_articles(label, scrape_status)


@admin.route("/reindex-search", methods=["POST"])
@login_required
def reindex_search():
    with search_reindex_lock:
        current_status = _search_reindex_status_payload()
        if current_status.get("status") == "running":
            return jsonify({
                "started": False,
                "status": current_status,
            }), 409

        started_at = datetime.utcnow().isoformat()
        _save_json_setting(
            SEARCH_REINDEX_STATUS_KEY,
            {
                "status": "running",
                "started_at": started_at,
                "finished_at": None,
                "message": "Reindexing stories and articles into Meilisearch.",
                "story_documents": None,
                "article_documents": None,
            },
        )
        app = current_app._get_current_object()
        thread = threading.Thread(target=_run_search_reindex, args=(app,), daemon=True)
        thread.start()

    return jsonify({
        "started": True,
        "status": _search_reindex_status_payload(),
    }), 202


@admin.route("/reindex-search-status")
@login_required
def reindex_search_status():
    return jsonify(_search_reindex_status_payload())


@admin.route("/ai-task/start", methods=["POST"])
@login_required
def start_ai_task():
    payload = request.get_json(silent=True) or request.form
    task_type = (payload.get("task_type") or "").strip()
    resource_id = payload.get("resource_id")

    try:
        resource_id = int(resource_id)
    except (TypeError, ValueError):
        return jsonify({
            "started": False,
            "message": "Invalid resource id.",
        }), 400

    if task_type not in {"story_summary", "story_deep_report", "article_summary"}:
        return jsonify({
            "started": False,
            "message": "Invalid AI task type.",
        }), 400

    with ai_task_lock:
        current_status = _ai_task_status_payload(task_type, resource_id)
        if current_status.get("status") == "running":
            return jsonify({
                "started": False,
                "status": current_status,
            }), 409

        started_at = datetime.utcnow().isoformat()
        _save_ai_task_status(
            task_type,
            resource_id,
            {
                "status": "running",
                "started_at": started_at,
                "finished_at": None,
                "message": "AI task is running.",
            },
        )
        app = current_app._get_current_object()
        thread = threading.Thread(
            target=_run_ai_task,
            args=(app, task_type, resource_id),
            daemon=True,
        )
        thread.start()

    return jsonify({
        "started": True,
        "status": _ai_task_status_payload(task_type, resource_id),
    }), 202


@admin.route("/ai-task-status/<task_type>/<int:resource_id>")
@login_required
def ai_task_status(task_type, resource_id):
    if task_type not in {"story_summary", "story_deep_report", "article_summary"}:
        return jsonify({
            "status": "error",
            "message": "Invalid AI task type.",
        }), 400
    return jsonify(_ai_task_status_payload(task_type, resource_id))


@admin.route("/reclassify-articles", methods=["POST"])
@login_required
def reclassify_articles():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    from news_fetcher.fetch_and_store_articles import reclassify_all_articles
    dispatch_background_tool("Reclassify Topics", ["Re-running topic classification"], reclassify_all_articles)
    return redirect_to_articles(label, scrape_status)


@admin.route("/deep-report/<int:story_id>", methods=["POST"])
@login_required
def deep_report_route(story_id):
    story = Story.query.get_or_404(story_id)
    label = request.form.get("label", "")
    try:
        if len(story.articles) >= 2:
            from news_fetcher.summarizer import generate_deep_report, check_ollama_status
            if check_ollama_status():
                report = generate_deep_report(story)
                if report:
                    story.deep_report = report
                    db.session.commit()
    except Exception as e:
        logger.error(f"Deep report error: {e}")
    if label:
        return redirect(url_for("admin.list_articles", topic=label))
    return redirect(url_for("admin.list_articles"))


@admin.route("/scrape-blocklist")
@login_required
def scrape_blocklist():
    from aggregator.models import ScrapeBlocklist
    cutoff = datetime.utcnow() - timedelta(hours=24)
    entries = ScrapeBlocklist.query.order_by(
        ScrapeBlocklist.is_permanent.desc(),
        ScrapeBlocklist.added_at.desc()
    ).all()
    recent_articles = Article.query.filter(Article.fetched_at >= cutoff).all()

    status_counts = {}
    domain_status_counts = {}
    domain_last_seen = {}

    for article in recent_articles:
        status = (article.scrape_status or "pending").lower()
        status_counts[status] = status_counts.get(status, 0) + 1

        domain = article_domain(article.url)
        if not domain:
            continue

        domain_counts = domain_status_counts.setdefault(domain, {})
        domain_counts[status] = domain_counts.get(status, 0) + 1
        fetched_at = article.fetched_at or article.date
        if fetched_at and fetched_at > domain_last_seen.get(domain, datetime.min):
            domain_last_seen[domain] = fetched_at

    recent_problem_domains = []
    for domain, counts in domain_status_counts.items():
        issue_total = counts.get("blocked", 0) + counts.get("failed", 0) + counts.get("fallback", 0)
        if not issue_total:
            continue
        recent_problem_domains.append({
            "domain": domain,
            "issue_total": issue_total,
            "success": counts.get("success", 0),
            "fallback": counts.get("fallback", 0),
            "blocked": counts.get("blocked", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "last_seen": domain_last_seen.get(domain),
        })

    recent_problem_domains.sort(
        key=lambda row: (row["issue_total"], row["blocked"], row["failed"], row["fallback"]),
        reverse=True,
    )
    recent_problem_domains = recent_problem_domains[:15]

    retry_cache = _load_json_setting("scrape_retry_cache_v1") or {"domains": {}, "urls": {}}
    retry_cache_domains = []
    retry_cache_urls = []
    for domain, payload in (retry_cache.get("domains") or {}).items():
        if isinstance(payload, dict):
            retry_cache_domains.append({
                "domain": domain,
                "status": payload.get("status"),
                "failure_reason": payload.get("failure_reason"),
                "failure_count": payload.get("failure_count", 0),
                "defer_until": payload.get("defer_until"),
            })
    for url, payload in (retry_cache.get("urls") or {}).items():
        if isinstance(payload, dict):
            retry_cache_urls.append({
                "url": url,
                "status": payload.get("status"),
                "failure_reason": payload.get("failure_reason"),
                "failure_count": payload.get("failure_count", 0),
                "defer_until": payload.get("defer_until"),
            })

    retry_cache_domains.sort(key=lambda row: (row["defer_until"] or "", row["failure_count"]), reverse=True)
    retry_cache_urls.sort(key=lambda row: (row["defer_until"] or "", row["failure_count"]), reverse=True)

    blocklist_rows = []
    for entry in entries:
        counts = domain_status_counts.get(entry.domain, {})
        blocklist_rows.append({
            "entry": entry,
            "success": counts.get("success", 0),
            "fallback": counts.get("fallback", 0),
            "blocked": counts.get("blocked", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "last_seen": domain_last_seen.get(entry.domain),
        })

    return render_template(
        "scrape_blocklist.html",
        entries=blocklist_rows,
        status_counts=status_counts,
        recent_problem_domains=recent_problem_domains,
        retry_cache_domain_count=len(retry_cache_domains),
        retry_cache_url_count=len(retry_cache_urls),
        retry_cache_domains=retry_cache_domains[:10],
        retry_cache_urls=retry_cache_urls[:10],
        telemetry_window_hours=24,
    )


@admin.route("/audit-scrapes", methods=["POST"])
@login_required
def audit_scrapes():
    label = request.form.get("label", "")
    from news_fetcher.fetch_and_store_articles import audit_existing_scrapes
    dispatch_background_tool("Audit Stored Scrapes", ["Auditing existing article content"], audit_existing_scrapes)
    return redirect(url_for("admin.scrape_blocklist"))


@admin.route("/unblock-domain", methods=["POST"])
@login_required
def unblock_domain():
    from aggregator.models import ScrapeBlocklist
    domain = request.form.get("domain", "").strip()
    if domain:
        entry = ScrapeBlocklist.query.filter_by(domain=domain, is_permanent=False).first()
        if entry:
            db.session.delete(entry)
            db.session.commit()
            logger.info(f"[Blocklist] Removed {domain}")
    return redirect(url_for("admin.scrape_blocklist"))


@admin.route("/sync-static", methods=["POST"])
@login_required
def sync_static():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    from news_fetcher.fetch_and_store_articles import sync_static_ratings
    dispatch_background_tool("Sync Curated Ratings", ["Applying curated bias to outlets and articles"], sync_static_ratings)
    return redirect_to_articles(label, scrape_status)


@admin.route("/merge-outlets", methods=["POST"])
@login_required
def merge_outlets():
    from news_fetcher.fetch_and_store_articles import merge_duplicate_outlets
    try:
        summary = merge_duplicate_outlets()
        return jsonify({
            'status': 'ok',
            'renamed': summary['renamed'],
            'outlets_deleted': summary['outlets_deleted'],
            'articles_reassigned': summary['articles_reassigned'],
        })
    except Exception as e:
        logger.error(f"Outlet merge failed: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@admin.route("/metrics")
@login_required
def metrics():
    return jsonify({
        "last_run_metrics": _load_json_setting("last_run_metrics"),
        "last_headline_site_metrics": _load_json_setting("last_headline_site_metrics"),
        "scrape_outcome_history": _load_json_setting("scrape_outcome_history_v1"),
    })


@admin.route("/pipeline-status")
@login_required
def pipeline_status():
    status = _load_json_setting("pipeline_live_status") or {
        "status": "idle",
        "current_step": None,
        "started_at": None,
        "finished_at": None,
        "steps_completed": [],
        "steps_remaining": [],
        "ollama_up": None,
    }
    
    last_run_metrics = _load_json_setting("last_run_metrics") or {}
    last_headline_metrics = _load_json_setting("last_headline_site_metrics") or {}
    
    return jsonify({
        "live_status": status,
        "last_run_summary": {
            "input_articles": last_run_metrics.get("totals", {}).get("input_articles", 0),
            "stored_articles": last_run_metrics.get("totals", {}).get("stored", 0),
            "stories_touched": last_run_metrics.get("totals", {}).get("stories_touched", 0),
            "scrape_statuses": last_run_metrics.get("totals", {}).get("scrape_statuses", {}),
            "headline_readable": last_headline_metrics.get("scrape", {}).get("readable_articles", 0)
        }
    })


@admin.route("/db-stats")
@login_required
def db_stats():
    from aggregator.models import Article, Story, Outlet, Edition
    from aggregator.search import get_index_stats
    
    total_articles = db.session.query(func.count(Article.id)).scalar() or 0
    articles_with_embeddings = db.session.query(func.count(Article.id)).filter(Article.embedding != None).scalar() or 0
    articles_without_embeddings = total_articles - articles_with_embeddings
    
    total_stories = db.session.query(func.count(Story.id)).scalar() or 0
    total_outlets = db.session.query(func.count(Outlet.id)).scalar() or 0
    total_editions = db.session.query(func.count(Edition.id)).scalar() or 0
    
    # Try to get postgres relation size for articles table
    try:
        articles_size_bytes = db.session.execute(db.text("SELECT pg_total_relation_size('articles')")).scalar()
    except Exception:
        articles_size_bytes = 0

    meili_stats = get_index_stats()
    
    return jsonify({
        "articles": {
            "total": total_articles,
            "with_embeddings": articles_with_embeddings,
            "without_embeddings": articles_without_embeddings,
            "size_bytes": articles_size_bytes,
        },
        "stories": {"total": total_stories},
        "outlets": {"total": total_outlets},
        "editions": {"total": total_editions},
        "meilisearch": meili_stats,
    })


@admin.route("/purge-embeddings", methods=["POST"])
@login_required
def purge_embeddings():
    from aggregator.models import Article
    try:
        updated = db.session.execute(db.update(Article).values(embedding=None)).rowcount
        db.session.commit()
        logger.info(f"Purged vector embeddings from {updated} articles.")
        return jsonify({"status": "ok", "cleared": updated})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error purging embeddings: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@admin.route("/purge-articles", methods=["POST"])
@login_required
def purge_articles():
    from aggregator.models import Article, Story
    try:
        deleted_articles = db.session.query(Article).count()
        deleted_stories = db.session.query(Story).count()
        
        db.session.execute(db.text("TRUNCATE stories, articles CASCADE"))
        db.session.commit()
        
        logger.info(f"Purged {deleted_articles} articles and {deleted_stories} stories via TRUNCATE.")
        return jsonify({
            "status": "ok", 
            "articles_deleted": deleted_articles,
            "stories_deleted": deleted_stories
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error purging articles: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@admin.route("/purge-raw-payloads", methods=["POST"])
@login_required
def purge_raw_payloads():
    from aggregator.models import RawArticlePayload
    try:
        deleted = db.session.query(RawArticlePayload).delete()
        db.session.commit()
        logger.info(f"Purged {deleted} raw payloads.")
        return jsonify({"status": "ok", "deleted": deleted})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error purging raw payloads: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@admin.route("/vacuum-db", methods=["POST"])
@login_required
def vacuum_db():
    try:
        connection = db.engine.raw_connection()
        connection.set_isolation_level(0) # AUTOCOMMIT
        cursor = connection.cursor()
        cursor.execute("VACUUM ANALYZE articles;")
        cursor.execute("VACUUM ANALYZE stories;")
        cursor.execute("VACUUM ANALYZE edition_stories;")
        cursor.execute("VACUUM ANALYZE article_topics;")
        cursor.execute("VACUUM ANALYZE story_topics;")
        cursor.close()
        connection.close()
        logger.info("Vacuum analyze completed successfully.")
        return jsonify({"status": "ok", "message": "Vacuum completed."})
    except Exception as e:
        logger.error(f"Error running vacuum: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Topic management CRUD
# ---------------------------------------------------------------------------

@admin.route("/topics")
@login_required
def topics_list():
    topics = Topic.query.order_by(Topic.display_order, Topic.id).all()
    return render_template("admin_topics.html", topics=topics)


@admin.route("/topics/new", methods=["GET"])
@login_required
def topic_new():
    return render_template("admin_topic_form.html", topic=None)


def _read_topic_form_data(fallback_name):
    try:
        display_order = int(request.form.get("display_order", 99))
    except (ValueError, TypeError):
        display_order = 99

    return {
        "label": request.form.get("label", "").strip() or fallback_name,
        "description": request.form.get("description", "").strip() or None,
        "icon": request.form.get("icon", "").strip() or None,
        "display_order": display_order,
        "is_active": request.form.get("is_active") == "on",
        "fetch_mode": request.form.get("fetch_mode", "").strip() or None,
        "fetch_country": request.form.get("fetch_country", "").strip() or None,
        "fetch_category": request.form.get("fetch_category", "").strip() or None,
        "fetch_query": request.form.get("fetch_query", "").strip() or None,
        "gnews_query": request.form.get("gnews_query", "").strip() or None,
        "gnews_category": request.form.get("gnews_category", "").strip() or None,
        "analysis_persona": request.form.get("analysis_persona", "").strip() or None,
        "analysis_keywords_json": request.form.get("analysis_keywords_json", "").strip() or None,
        "classifier_hint": request.form.get("classifier_hint", "").strip() or None,
        "summary_prompt": request.form.get("summary_prompt", "").strip() or None,
        "deep_report_prompt": request.form.get("deep_report_prompt", "").strip() or None,
    }


@admin.route("/topics", methods=["POST"])
@login_required
def topic_create():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Topic name is required.", "error")
        return redirect(url_for("admin.topic_new"))

    existing = Topic.query.filter_by(name=name).first()
    if existing:
        flash(f"Topic '{name}' already exists. Redirected to edit.", "warning")
        return redirect(url_for("admin.topic_edit", topic_id=existing.id))

    topic_data = _read_topic_form_data(name)
    topic = Topic(name=name, **topic_data)
    
    db.session.add(topic)
    db.session.commit()
    logger.info("Created topic id=%s name=%r", topic.id, topic.name)
    flash(f"Topic '{topic.name}' created.", "success")
    return redirect(url_for("admin.topics_list"))


@admin.route("/topics/<int:topic_id>/edit", methods=["GET"])
@login_required
def topic_edit(topic_id):
    topic = Topic.query.get_or_404(topic_id)
    return render_template("admin_topic_form.html", topic=topic)


@admin.route("/topics/<int:topic_id>", methods=["POST"])
@login_required
def topic_update(topic_id):
    topic = Topic.query.get_or_404(topic_id)

    new_name = request.form.get("name", "").strip()
    if new_name and new_name != topic.name:
        conflict = Topic.query.filter(Topic.name == new_name, Topic.id != topic.id).first()
        if conflict:
            logger.warning("Cannot rename topic %s to %r — name already taken by topic %s", topic.id, new_name, conflict.id)
            flash(f"Cannot rename to '{new_name}' — name already taken.", "error")
        else:
            topic.name = new_name

    topic_data = _read_topic_form_data(topic.name)
    
    # Restore the original display_order if the form was invalid (instead of defaulting to 99)
    if "display_order" not in request.form:
        topic_data["display_order"] = topic.display_order
        try:
            topic_data["display_order"] = int(request.form.get("display_order", topic.display_order))
        except (ValueError, TypeError):
            pass

    for key, value in topic_data.items():
        setattr(topic, key, value)

    db.session.commit()
    logger.info("Updated topic id=%s name=%r", topic.id, topic.name)
    flash(f"Topic '{topic.name}' updated.", "success")
    return redirect(url_for("admin.topics_list"))


@admin.route("/topics/<int:topic_id>/toggle-active", methods=["POST"])
@login_required
def topic_toggle_active(topic_id):
    topic = Topic.query.get_or_404(topic_id)
    topic.is_active = not topic.is_active
    db.session.commit()
    return redirect(url_for("admin.topics_list"))


@admin.route("/topics/<int:topic_id>/delete", methods=["POST"])
@login_required
def topic_delete(topic_id):
    topic = Topic.query.get_or_404(topic_id)
    topic.is_active = False  # Soft-delete: keep history intact
    db.session.commit()
    logger.info("Soft-deleted topic id=%s name=%r", topic.id, topic.name)
    return redirect(url_for("admin.topics_list"))


@admin.route("/topics/reseed", methods=["POST"])
@login_required
def reseed_topics():
    import subprocess
    import sys
    from flask import flash
    try:
        subprocess.run([sys.executable, "seed_topics.py"], check=True, capture_output=True, text=True)
        flash("Topics successfully reseeded from country configuration.", "success")
    except subprocess.CalledProcessError as e:
        logger.error("Failed to reseed topics: %s", e.stderr)
        flash(f"Error reseeding topics: {e.stderr}", "danger")
    except Exception as e:
        logger.error("Failed to reseed topics: %s", e)
        flash(f"Error reseeding topics: {e}", "danger")
    return redirect(url_for("admin.topics_list"))

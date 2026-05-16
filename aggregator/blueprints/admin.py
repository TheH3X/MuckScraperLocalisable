import logging
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from flask_login import login_required
from sqlalchemy import func, or_
from aggregator import db
from aggregator.models import AppSetting, Article, Story, Topic, RawArticlePayload
from aggregator.constants import TOPICS, AGGREGATORS

logger = logging.getLogger(__name__)

admin = Blueprint("admin", __name__)
SCRAPE_STATUS_FILTERS = ("success", "fallback", "blocked", "failed", "skipped", "pending")
FETCH_PRESETS = [
    {
        "label": "US Politics",
        "description": "Congress, White House, courts, elections",
        "mode": "query",
        "country": "",
        "category": "",
        "query": "US politics congress white house senate supreme court",
        "gnews_query": "US politics congress white house",
        "gnews_category": "",
    },
    {
        "label": "Business & Economy",
        "description": "Top business headlines",
        "mode": "top",
        "country": "us",
        "category": "business",
        "query": "",
        "gnews_query": "",
        "gnews_category": "business",
    },
    {
        "label": "Science & Health",
        "description": "Research, medicine, technology",
        "mode": "query",
        "country": "",
        "category": "",
        "query": "scientific breakthroughs medical research healthcare tech",
        "gnews_query": "science health research",
        "gnews_category": "science",
    },
    {
        "label": "Sports",
        "description": "Top sports headlines",
        "mode": "top",
        "country": "us",
        "category": "sports",
        "query": "",
        "gnews_query": "",
        "gnews_category": "sports",
    },
    {
        "label": "World News",
        "description": "International news, conflict, diplomacy",
        "mode": "query",
        "country": "",
        "category": "",
        "query": "international world global news conflicts diplomacy",
        "gnews_query": "world global news",
        "gnews_category": "world",
    },
]


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


def article_domain(url):
    if not url:
        return None
    domain = urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


def apply_aggregator_filter(story):
    from datetime import datetime as dt
    originals = []
    aggregators_list = []
    has_good_original = False
    seen_articles = set()
    sorted_articles = sorted(story.articles, key=lambda x: x.date or dt.min, reverse=True)
    for art in sorted_articles:
        key = (art.title, art.outlet_id)
        if key in seen_articles:
            continue
        seen_articles.add(key)
        outlet_name = art.outlet.name if art.outlet else ""
        if any(agg in outlet_name for agg in AGGREGATORS):
            aggregators_list.append(art)
        else:
            originals.append(art)
            if art.content and len(art.content) > 500:
                has_good_original = True
    story.display_articles = originals if has_good_original else (originals + aggregators_list)
    if not has_good_original:
        story.display_articles.sort(key=lambda x: x.date or dt.min, reverse=True)


def redirect_to_articles(label=None, scrape_status=None):
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
    return render_template("fetch.html", fetch_presets=FETCH_PRESETS)


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

    if not show_single:
        query = query.having(func.count(Article.id) > 1)

    if active_label:
        topic = Topic.query.filter_by(name=active_label).first()
        if topic:
            query = query.filter(Story.topics.contains(topic))
        else:
            query = query.filter(False)

    if active_scrape_status:
        query = query.filter(Story.articles.any(Article.scrape_status == active_scrape_status))

    search_rank = None
    if active_search_query:
        tsquery = func.websearch_to_tsquery("english", active_search_query)
        story_document = func.to_tsvector(
            "english",
            func.concat_ws(
                " ",
                func.coalesce(Story.title, ""),
                func.coalesce(Story.headline, ""),
                func.coalesce(Story.summary, ""),
                func.coalesce(Story.deep_report, ""),
            ),
        )
        article_document = func.to_tsvector(
            "english",
            func.concat_ws(
                " ",
                func.coalesce(Article.title, ""),
                func.coalesce(Article.content, ""),
                func.coalesce(Article.source, ""),
            ),
        )
        query = query.filter(
            or_(
                story_document.op("@@")(tsquery),
                article_document.op("@@")(tsquery),
                Story.topics.any(Topic.name.ilike(f"%{active_search_query}%")),
            )
        )
        search_rank = func.greatest(
            func.coalesce(func.max(func.ts_rank(story_document, tsquery)), 0.0),
            func.coalesce(func.max(func.ts_rank(article_document, tsquery)), 0.0),
        )

    order_by = [func.max(Article.date).desc()]
    if search_rank is not None:
        order_by.insert(0, search_rank.desc())

    pagination = query.order_by(*order_by).paginate(
        page=page, per_page=per_page, error_out=False
    )

    stories = pagination.items if pagination else []
    total_pages = pagination.pages if pagination else 0

    for story in stories:
        apply_aggregator_filter(story)

    return render_template(
        "articles.html",
        stories=stories,
        topics=TOPICS,
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
    mode = request.form.get("mode", "top").strip()
    query = request.form.get("query", "").strip() or None
    country = request.form.get("country", "").strip() or None
    category = request.form.get("category", "").strip() or None
    label = request.form.get("label", "").strip() or None
    scrape_status = request.form.get("scrape_status", "").strip() or None
    gnews_query = request.form.get("gnews_query", "").strip() or None
    gnews_category = request.form.get("gnews_category", "").strip() or None

    try:
        from news_fetcher.fetch_and_store_articles import fetch_and_store_articles
        fetch_and_store_articles(
            topic_name=label or "Custom",
            mode=mode,
            query=query,
            country=country,
            category=category,
            gnews_query=gnews_query,
            gnews_category=gnews_category,
        )
    except Exception as e:
        logger.error(f"Fetch error: {e}")

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
    try:
        from news_fetcher.fetch_and_store_articles import ollama_catchup
        ollama_catchup()
    except Exception as e:
        logger.error(f"Catchup error: {e}")
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
    try:
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
    except Exception as e:
        logger.error(f"Scrape all error: {e}")
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
    try:
        from news_fetcher.fetch_and_store_articles import force_regroup_all
        force_regroup_all()
    except Exception as e:
        logger.exception(f"Force regroup error: {e}")
    return redirect_to_articles(label, scrape_status)


@admin.route("/force-resummarize", methods=["POST"])
@login_required
def force_resummarize():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.fetch_and_store_articles import force_resummarize_all
        force_resummarize_all()
    except Exception as e:
        logger.exception(f"Force resummarize error: {e}")
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


@admin.route("/reclassify-articles", methods=["POST"])
@login_required
def reclassify_articles():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.fetch_and_store_articles import reclassify_all_articles
        reclassify_all_articles()
    except Exception as e:
        logger.exception(f"Reclassify error: {e}")
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
    try:
        from news_fetcher.fetch_and_store_articles import audit_existing_scrapes
        audit_existing_scrapes()
    except Exception as e:
        logger.exception(f"Audit error: {e}")
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


@admin.route("/sync-allsides", methods=["POST"])
@login_required
def sync_allsides():
    label = request.form.get("label", "")
    scrape_status = request.form.get("scrape_status", "").strip() or None
    try:
        from news_fetcher.fetch_and_store_articles import sync_allsides_ratings
        sync_allsides_ratings()
    except Exception as e:
        logger.exception(f"AllSides sync error: {e}")
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

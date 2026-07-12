import os
import requests
import logging
from datetime import datetime, timedelta, date as date_cls
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, abort
from aggregator.search import healthcheck as meili_healthcheck
from aggregator.models import Article, Story, Topic, RawArticlePayload, Edition, EditionStory
from aggregator.story_view import apply_aggregator_filter, compute_bias_breakdown

logger = logging.getLogger(__name__)

public = Blueprint("public", __name__)


from news_fetcher.llm_client import check_ollama_status


@public.route("/")
def index():
    return redirect(url_for("public.latest_edition"))


@public.route("/feed-headlines")
def aggregator_headlines():
    from aggregator.models import Story, Article
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=1)
    stories = Story.query.join(Article).group_by(Story.id).filter(
        Story.created_at >= cutoff,
        Story.headline_score > 0
    ).order_by(Story.headline_score.desc()).limit(20).all()
    
    for story in stories:
        apply_aggregator_filter(story)
        
    return render_template(
        'articles.html',
        stories=stories,
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.display_order).all(),
        active_label=None,
        page=1,
        total_pages=1,
        show_single=True,
        is_multi_view=False
    )


EDITION_TYPE_ORDER = {"morning": 0, "afternoon": 1, "evening": 2, "night": 3}
EDITION_PAGE_SIZE = 5


def _published_editions_query():
    return Edition.query.filter_by(published=True)


def _edition_sort_key(edition):
    return (edition.date, EDITION_TYPE_ORDER.get(edition.edition_type, 99))


def _recent_editions(limit=10):
    return sorted(
        _published_editions_query().all(),
        key=_edition_sort_key,
        reverse=True,
    )[:limit]


def _story_kind(rank, story, edition_story):
    """Classify a ranked story the way a print edition would label it."""
    if rank == 1:
        return "top-story", "Top Story"
    if edition_story.has_updates or len(story.articles) >= 3:
        return "developing", "Developing"
    return "in-brief", "In Brief"


@public.route("/editions/latest")
def latest_edition():
    edition = _published_editions_query().order_by(Edition.created_at.desc()).first()
    if not edition:
        abort(404, description="No editions have been published yet.")
    return redirect(url_for(
        "public.view_edition",
        edition_date=edition.date.isoformat(),
        edition_type=edition.edition_type,
    ))


@public.route("/editions")
def edition_archive():
    editions = _published_editions_query().order_by(
        Edition.date.desc(), Edition.created_at.desc()
    ).all()

    grouped = {}
    for edition in editions:
        grouped.setdefault(edition.date, []).append(edition)
    for day_editions in grouped.values():
        day_editions.sort(key=lambda e: EDITION_TYPE_ORDER.get(e.edition_type, 99))

    return render_template(
        "edition_archive.html",
        grouped_editions=sorted(grouped.items(), key=lambda item: item[0], reverse=True),
        recent_editions=_recent_editions(),
    )


@public.route("/editions/<edition_date>/<edition_type>")
def view_edition(edition_date, edition_type):
    try:
        parsed_date = date_cls.fromisoformat(edition_date)
    except ValueError:
        abort(404)

    edition = Edition.query.filter_by(date=parsed_date, edition_type=edition_type).first()
    if not edition:
        abort(404)

    edition_stories = edition.edition_stories.order_by(EditionStory.rank).all()
    entries = []
    for rank, edition_story in enumerate(edition_stories, start=1):
        story = edition_story.story
        if not story:
            continue
        apply_aggregator_filter(story)
        kind_class, kind_label = _story_kind(rank, story, edition_story)
        entries.append({
            "rank": rank,
            "edition_story": edition_story,
            "story": story,
            "bias": compute_bias_breakdown(story),
            "kind_class": kind_class,
            "kind_label": kind_label,
        })

    total_pages = max(1, (len(entries) + EDITION_PAGE_SIZE - 1) // EDITION_PAGE_SIZE)
    page = request.args.get("page", 1, type=int) or 1
    page = min(max(page, 1), total_pages)
    page_entries = entries[(page - 1) * EDITION_PAGE_SIZE : page * EDITION_PAGE_SIZE]

    all_published = sorted(_published_editions_query().all(), key=_edition_sort_key)
    current_index = next(
        (i for i, e in enumerate(all_published) if e.id == edition.id), None
    )
    prev_edition = all_published[current_index - 1] if current_index and current_index > 0 else None
    next_edition = (
        all_published[current_index + 1]
        if current_index is not None and current_index + 1 < len(all_published)
        else None
    )

    recent_editions = sorted(all_published, key=_edition_sort_key, reverse=True)[:10]

    outlet_ids = set()
    total_articles = 0
    for item in entries:
        total_articles += len(item["story"].articles)
        outlet_ids.update(o.id for o in item["story"].unique_outlets)

    edition_stats = {
        "stories": len(entries),
        "articles": total_articles,
        "outlets": len(outlet_ids),
    }

    ollama_online = check_ollama_status()

    return render_template(
        "edition.html",
        edition=edition,
        entries=page_entries,
        total_story_count=len(entries),
        edition_stats=edition_stats,
        page=page,
        total_pages=total_pages,
        recent_editions=recent_editions,
        prev_edition=prev_edition,
        next_edition=next_edition,
        ollama_online=ollama_online,
    )


@public.route("/story/<int:story_id>")
def view_story(story_id):
    from sqlalchemy.orm import joinedload
    story = Story.query.options(
        joinedload(Story.articles).joinedload(Article.outlet)
    ).get_or_404(story_id)

    ollama_online = check_ollama_status()

    apply_aggregator_filter(story)

    return render_template(
        "story.html",
        story=story,
        ollama_online=ollama_online,
        recent_editions=_recent_editions(),
    )


@public.route("/article/<int:article_id>")
def view_article(article_id):
    article = Article.query.get_or_404(article_id)
    ollama_online = check_ollama_status()

    return render_template(
        "article.html",
        article=article,
        ollama_online=ollama_online,
        recent_editions=_recent_editions(),
    )


@public.route("/ollama-status")
def ollama_status():
    return jsonify({"online": check_ollama_status()})


@public.route("/meili-status")
def meili_status():
    return jsonify({"online": meili_healthcheck()})

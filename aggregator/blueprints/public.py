import os
import requests
import logging
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from aggregator.models import Article, Story, Topic, RawArticlePayload
from aggregator.constants import TOPICS, AGGREGATORS

logger = logging.getLogger(__name__)

public = Blueprint("public", __name__)


def apply_aggregator_filter(story):
    from datetime import datetime as dt
    originals = []
    aggregators = []
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
            aggregators.append(art)
        else:
            originals.append(art)
            if art.content and len(art.content) > 500:
                has_good_original = True
    story.display_articles = originals if has_good_original else (originals + aggregators)
    if not has_good_original:
        story.display_articles.sort(key=lambda x: x.date or dt.min, reverse=True)


def check_ollama_status():
    ollama_host = os.environ.get("OLLAMA_HOST", "")
    if not ollama_host:
        return False
    try:
        response = requests.get(f"{ollama_host}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


@public.route("/")
def index():
    return redirect(url_for("admin.list_articles"))


@public.route("/feed-headlines")
def aggregator_headlines():
    from aggregator.models import Story, Article
    from datetime import datetime, timedelta
    from aggregator.constants import TOPICS
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
        topics=TOPICS,
        active_label=None,
        page=1,
        total_pages=1,
        show_single=True,
        is_multi_view=False
    )


@public.route("/story/<int:story_id>")
def view_story(story_id):
    from sqlalchemy.orm import joinedload
    story = Story.query.options(
        joinedload(Story.articles).joinedload(Article.outlet)
    ).get_or_404(story_id)

    ollama_online = check_ollama_status()

    apply_aggregator_filter(story)

    return render_template("story.html", story=story, ollama_online=ollama_online)


@public.route("/article/<int:article_id>")
def view_article(article_id):
    article = Article.query.get_or_404(article_id)
    ollama_online = check_ollama_status()

    return render_template("article.html", article=article, ollama_online=ollama_online)


@public.route("/ollama-status")
def ollama_status():
    return jsonify({"online": check_ollama_status()})

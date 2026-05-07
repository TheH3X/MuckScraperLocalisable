# muckscraperHeadlinesGoogleNEW/news_fetcher/fetch_and_store_articles.py
# news_fetcher/fetch_and_store_articles.py

from aggregator import create_app, db
from aggregator.models import Article, Outlet, Story, Topic
from newsapi import NewsApiClient
from news_fetcher.outlet_bias_llm import get_outlet_bias_from_llm
from news_fetcher.allsides_lookup import get_allsides_score
from news_fetcher.summarizer import summarize_story, check_ollama_status, generate_deep_report, summarize_article
from news_fetcher.scraper import scrape_article
from datetime import datetime
import requests
import os
import json
from news_fetcher.story_grouper import find_or_create_story, get_embedding
from datetime import datetime, timedelta
from news_fetcher.topic_classifier import classify_article
from news_fetcher.headline_generator import generate_story_headline, generate_missing_headlines
import logging

logger = logging.getLogger(__name__)

app = create_app()

BLOCKED_SOURCES = [
    "github.com",
    "github.blog",
    "dev.to",
    "stackoverflow.com",
    "reddit.com",
    "npmjs.com",
    "pypi.org",
]

BLOCKED_TITLE_KEYWORDS = [
    "starred",
    "forked",
    "pull request",
    "merged",
    "repository",
    "npm package",
    "pypi",
    "added to pypi",
    "released on pypi",
    "week in review",
    "patch tuesday",
    "added to npm",
    "new release:",
    "changelog:",
    "box office",
    "box score",
    "game recap",
    "highlights:",
    "traded to",
    "signs with",
    "scores in",
    "Nintendo",
    "PlayStation",
    "Xbox",
    "Game review",
    "Gameplay",
    "eSports",
    "patch notes",
    "Twitch",
    "Fortnite",
    "Minecraft",
    "Pokemon",
]


def guess_story_title(title):
    if ":" in title:
        return title.split(":")[0]
    if "-" in title:
        return title.split("-")[0]
    return " ".join(title.split()[:6])


def retry_unrated_outlets():
    """Find outlets with no bias score and retry.
    Checks AllSides lookup table first, then falls back to Ollama.
    Outlets that have failed Ollama 15 or more times are permanently skipped.
    """
    unrated = Outlet.query.filter(
        Outlet.bias_score == None,
        Outlet.bias_retry_count < 15
    ).all()

    if not unrated:
        logger.info("No unrated outlets to retry.")
        return

    skipped = Outlet.query.filter(
        Outlet.bias_score == None,
        Outlet.bias_retry_count >= 15
    ).count()

    if skipped:
        logger.info(f"Permanently skipping {skipped} outlets that have failed 15+ times.")

    logger.info(f"Found {len(unrated)} unrated outlets, checking AllSides then Ollama...")

    for outlet in unrated:
        # Check AllSides lookup table first
        as_score = get_allsides_score(outlet.name)
        if as_score is not None:
            logger.info(f"  AllSides rating found for {outlet.name}: {as_score}")
            outlet.bias_score = as_score
            outlet.allsides_bias_score = as_score
            outlet.bias_source = "allsides"
            outlet.bias_retry_count = 0
            for article in outlet.articles:
                article.bias_score = as_score
            continue

        # Fall back to Ollama
        logger.info(f"  No AllSides rating for {outlet.name}, trying Ollama...")
        bias_score = get_outlet_bias_from_llm(outlet.name)

        if bias_score is not None:
            logger.info(f"  Ollama score {bias_score} for {outlet.name}")
            outlet.bias_score = bias_score
            outlet.bias_source = "ai"
            outlet.bias_retry_count = 0
            for article in outlet.articles:
                article.bias_score = bias_score
        else:
            outlet.bias_retry_count = (outlet.bias_retry_count or 0) + 1
            logger.warning(
                f"  Still couldn't rate {outlet.name} "
                f"(attempt {outlet.bias_retry_count}/15)."
            )

    db.session.commit()
    logger.info("Finished retrying unrated outlets.")


def get_or_create_topic(topic_name):
    """Get existing topic or create a new one, handling race conditions."""
    topic = Topic.query.filter_by(name=topic_name).first()
    if not topic:
        try:
            topic = Topic(name=topic_name)
            db.session.add(topic)
            db.session.flush()
        except Exception:
            # Another process created it at the same time, roll back and fetch it
            db.session.rollback()
            topic = Topic.query.filter_by(name=topic_name).first()
    return topic


def normalize_url(url):
    """Strip query parameters from URL to detect duplicates."""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        # Keep only scheme, netloc, and path
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return url


def detect_duplicate_outlet_content(content, outlet_id, exclude_article_id=None):
    """
    Check if scraped content is near-identical to other articles from the same outlet.
    This catches login/error pages that return the same HTML for every blocked request.
    Returns (is_duplicate: bool, reason: str or None).
    """
    if not content or not outlet_id:
        return False, None

    from news_fetcher.scraper import sanitize_html
    import re

    def strip_to_text(html, max_chars=2000):
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]

    clean_new = strip_to_text(content)
    if len(clean_new) < 100:
        return False, None

    from difflib import SequenceMatcher

    recent = Article.query.filter(
        Article.outlet_id == outlet_id,
        Article.content != None,
        Article.content != "",
    )
    if exclude_article_id:
        recent = recent.filter(Article.id != exclude_article_id)
    recent = recent.order_by(Article.id.desc()).limit(10).all()

    match_count = 0
    for article in recent:
        if not article.content:
            continue
        clean_existing = strip_to_text(article.content)
        if len(clean_existing) < 100:
            continue
        ratio = SequenceMatcher(None, clean_new, clean_existing).ratio()
        if ratio > 0.85:
            match_count += 1

    if match_count >= 2:
        reason = f"Bad scrape: content near-identical to {match_count} other articles from same outlet (login/error page)"
        return True, reason

    return False, None


def normalize_source_name(name):
    """Clean up and standardize outlet names."""
    if not name:
        return "Unknown"

    name_lower = name.lower().strip()

    # Define normalization map
    mapping = {
        "npr topics": "NPR",
        "home - cbsnews.com": "CBS News",
        "pbs newshour": "PBS News",
        "the associated press": "Associated Press",
        "fox news": "Fox News",
        "abc news": "ABC News",
        "nbc news": "NBC News",
        "the wall street journal": "WSJ",
        "the new york times": "New York Times",
        "the washington post": "Washington Post",
        
    }

    # Direct match in mapping
    if name_lower in mapping:
        return mapping[name_lower]

    # Partial matches/cleaning

    # Al Jazeera — strip long feed title
    if "al jazeera" in name_lower:
        return "Al Jazeera"

    # The Hill — strip " news" suffix
    if "the hill" in name_lower:
        return "The Hill"

    # New York Times variants
    if "nyt" in name_lower or "new york times" in name_lower:
        return "New York Times"

    # The Guardian variants
    if "guardian" in name_lower:
        return "The Guardian"

    # AP / Associated Press
    if "associated press" in name_lower or name_lower == "ap news":
        return "Associated Press"

    # Google News — flag as aggregator
    if name_lower == "google news":
        return "Google News"

    # Reuters variants
    if "reuters" in name_lower:
        return "Reuters"

    # Washington Post variants
    if "washington post" in name_lower:
        return "Washington Post"

    # Wall Street Journal variants  
    if "wall street journal" in name_lower or name_lower == "wsj":
        return "WSJ"

    # NBC variants — keep NBCSports separate
    if "nbc news" in name_lower:
        return "NBC News"
    if "nbcsports" in name_lower or "nbc sports" in name_lower:
        return "NBC Sports"

    # CBS variants
    if "cbs news" in name_lower:
        return "CBS News"

    # PBS variants
    if "pbs" in name_lower and "news" in name_lower:
        return "PBS News"

    # ABC News
    if "abc news" in name_lower:
        return "ABC News"

    # NPR variants
    if "npr" in name_lower:
        return "NPR"

    # BBC variants
    if "bbc" in name_lower:
        return "BBC News"

    # Fox News — keep Fox Business separate
    if "fox news" in name_lower:
        return "Fox News"
    if "fox business" in name_lower:
        return "Fox Business"

    # Bloomberg
    if "bloomberg" in name_lower:
        return "Bloomberg"

    # Axios
    if "axios" in name_lower:
        return "Axios"

    # CNN
    if name_lower == "cnn" or name_lower.startswith("cnn "):
        return "CNN"

    # CNBC
    if "cnbc" in name_lower and "tv18" not in name_lower:
        return "CNBC"

    return name


def merge_duplicate_outlets():
    """
    One-time (and periodic) cleanup:
    1. Re-normalizes all outlet names using normalize_source_name().
    2. Finds outlets whose normalized name matches another outlet.
    3. Merges duplicates — reassigns all articles to the canonical outlet,
       then deletes the duplicate.
    Returns a summary dict with counts for logging/display.
    """
    from aggregator.models import Outlet, Article

    outlets = Outlet.query.all()
    renamed = 0
    merged = 0
    deleted = 0

    # Step 1: Normalize all names in-place
    for outlet in outlets:
        clean = normalize_source_name(outlet.name)
        if clean != outlet.name:
            logger.info(f"  [Merge] Renaming '{outlet.name}' → '{clean}'")
            outlet.name = clean
            renamed += 1

    db.session.flush()

    # Step 2: Find duplicates by name (case-insensitive)
    # For each group of outlets with the same normalized name,
    # keep the one with the most articles (canonical), merge the rest into it.
    outlets = Outlet.query.all()
    name_map = {}
    for outlet in outlets:
        key = outlet.name.lower().strip()
        if key not in name_map:
            name_map[key] = []
        name_map[key].append(outlet)

    for name_key, group in name_map.items():
        if len(group) <= 1:
            continue

        # Canonical = outlet with the most articles
        canonical = max(group, key=lambda o: len(o.articles))
        duplicates = [o for o in group if o.id != canonical.id]

        for dup in duplicates:
            article_count = Article.query.filter_by(outlet_id=dup.id).count()
            logger.info(
                f"  [Merge] Merging '{dup.name}' (id={dup.id}, "
                f"{article_count} articles) → '{canonical.name}' (id={canonical.id})"
            )

            # CRITICAL: Reassign articles BEFORE deleting the outlet.
            # Use direct SQL update to avoid SQLAlchemy session conflicts
            # that can cause articles to be orphaned.
            db.session.execute(
                db.text(
                    "UPDATE articles SET outlet_id = :canonical_id "
                    "WHERE outlet_id = :dup_id"
                ),
                {"canonical_id": canonical.id, "dup_id": dup.id}
            )
            db.session.flush()

            # Verify reassignment before deleting
            remaining = Article.query.filter_by(outlet_id=dup.id).count()
            if remaining > 0:
                logger.error(
                    f"  [Merge] ABORT: {remaining} articles still attached to "
                    f"'{dup.name}' after reassignment — skipping delete"
                )
                continue

            # Copy bias data if canonical is missing it
            if canonical.bias_score is None and dup.bias_score is not None:
                canonical.bias_score = dup.bias_score
                canonical.bias_source = dup.bias_source
                canonical.allsides_bias_score = getattr(dup, 'allsides_bias_score', None)

            db.session.delete(dup)
            merged += article_count
            deleted += 1

    db.session.commit()

    summary = {
        'renamed': renamed,
        'outlets_deleted': deleted,
        'articles_reassigned': merged,
    }
    logger.info(f"  [Merge] Complete: {summary}")
    return summary


def store_articles(articles_data, topic_name):
    """
    Store a list of normalized article dicts into the database,
    tagging them with the given topic.
    articles_data: list of dicts with keys:
        title, content, url, source_name, published_at, image_url
    """
    stored = 0

    # Pre-fetch recent stories once for the whole batch
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent_stories = Story.query.filter(Story.created_at >= cutoff).all()
    logger.info(f"  [Grouper] Loaded {len(recent_stories)} recent stories for matching")

    for article in articles_data:
        title        = article.get("title")
        content      = article.get("content") or ""
        raw_url      = article.get("url")
        source_name  = normalize_source_name(article.get("source_name", "Unknown"))
        published_at = article.get("published_at", datetime.utcnow())
        image_url    = article.get("image_url")

        if not title or not raw_url:
            continue
            
        url = normalize_url(raw_url)

        if any(blocked in url.lower() for blocked in BLOCKED_SOURCES):
            logger.debug(f"Skipping blocked source: {url}")
            continue

        if any(kw in title.lower() for kw in BLOCKED_TITLE_KEYWORDS):
            logger.debug(f"Skipping blocked title: {title}")
            continue

        # Check for URL duplicate (normalized)
        existing = Article.query.filter_by(url=url).first()
        if existing:
            logger.debug(f"Skipping duplicate URL: {title}")
            continue

        # Check for Title + Source duplicate (catch same article, different URL)
        # First get/create outlet to have the ID
        outlet = Outlet.query.filter_by(name=source_name).first()
        if outlet:
            existing_title = Article.query.filter_by(title=title, outlet_id=outlet.id).first()
            if existing_title:
                logger.debug(f"Skipping duplicate Title+Outlet: {title}")
                continue
        
        logger.info(f"Processing: {title}")

        if not outlet:
            as_score = get_allsides_score(source_name)
            if as_score is not None:
                logger.info(f"  New outlet {source_name}: AllSides rating {as_score}")
                bias_score = as_score
                bias_source = "allsides"
                allsides_bias_score = as_score
            else:
                logger.info(f"  New outlet {source_name}: no AllSides rating, asking Ollama...")
                bias_score = get_outlet_bias_from_llm(source_name)
                bias_source = "ai" if bias_score is not None else None
                allsides_bias_score = None

            outlet = Outlet(
                name=source_name,
                url=url,
                description="N/A",
                bias_score=bias_score,
                allsides_bias_score=allsides_bias_score,
                bias_source=bias_source
            )
            db.session.add(outlet)
            db.session.flush()

        # Generate embedding for this article
        # Use title + snippet for better semantic matching
        from news_fetcher.story_grouper import strip_video_prefix
        clean_title = strip_video_prefix(title)
        embed_text = clean_title
        if content:
            from news_fetcher.summarizer import strip_html
            snippet = strip_html(content)[:200].strip()
            embed_text = f"{clean_title}. {snippet}"
        article_embedding = get_embedding(embed_text)
        
        story = find_or_create_story(title, db, Story, recent_stories,
                                     article_embedding=article_embedding,
                                     article_content=content)

        # Add new story to recent_stories so subsequent articles
        # in this same batch can match against it
        if story not in recent_stories:
            recent_stories.append(story)

        # Classify article into topics via Ollama
        from aggregator.models import Topic as TopicModel
        classified_topic_names = classify_article(title, content)
        for classified_name in classified_topic_names:
            classified_topic = TopicModel.query.filter_by(name=classified_name).first()
            if not classified_topic:
                classified_topic = TopicModel(name=classified_name)
                db.session.add(classified_topic)
                db.session.flush()
            if classified_topic not in story.topics:
                story.topics.append(classified_topic)

        scraped_content = scrape_article(url)
        if scraped_content:
            # Check if this looks like a duplicate login/error page across the outlet
            is_dup, dup_reason = detect_duplicate_outlet_content(scraped_content, outlet.id)
            if is_dup:
                logger.warning(f"  [Scraper] {dup_reason} — clearing content and blocking domain for {url[:60]}")
                from news_fetcher.scraper import add_to_blocklist
                add_to_blocklist(url, dup_reason)
                scraped_content = None

        if scraped_content:
            final_content = scraped_content
        else:
            from news_fetcher.scraper import sanitize_html
            final_content = sanitize_html(f"<div>{content}</div>") if content else ""

        # Ensure embedding is a list, not a string
        if isinstance(article_embedding, str):
            import json
            article_embedding = json.loads(article_embedding)

        new_article = Article(
            title=title,
            content=final_content,
            source=source_name,
            outlet_id=outlet.id,
            story_id=story.id,
            url=url,
            date=published_at,
            fetched_at=datetime.utcnow(),
            bias_score=outlet.bias_score,
            image_url=image_url,
            embedding=article_embedding
        )

        db.session.add(new_article)
        # IMPORTANT: Append to story.articles so it's visible to find_matching_story
        # for subsequent articles in this SAME loop iteration.
        story.articles.append(new_article)

        # Tag article with same topics as story
        for t in story.topics:
            if t not in new_article.topics:
                new_article.topics.append(t)

        # Generate headline if this is a multi-article story (2+ articles)
        if len(story.articles) >= 2:
            db.session.flush() # Ensure article is associated for headline generator
            headline = generate_story_headline(story)
            if headline:
                story.headline = headline
        else:
            # For single-article stories, ensure story headline is cleared
            # so the UI falls back to story.title (original article title)
            story.headline = None
                
        stored += 1

    db.session.commit()
    logger.info(f"Stored {stored} new articles for topic: {topic_name}")


def fetch_newsapi(topic_name, mode="top", query=None, country="us", category=None):
    """Fetch articles from NewsAPI and store them."""
    api_key = os.environ.get("NEWS_API_KEY", "")
    if not api_key:
        logger.warning("NEWS_API_KEY not set, skipping NewsAPI fetch.")
        return

    newsapi = NewsApiClient(api_key=api_key)

    try:
        if mode == "query" and query:
            logger.info(f"[NewsAPI] Fetching query: {query}")
            results = newsapi.get_everything(
                q=query,
                language="en",
                sort_by="publishedAt",
                page_size=100,
            )
        else:
            label = f"country={country}" if country else ""
            label += f" category={category}" if category else ""
            logger.info(f"[NewsAPI] Fetching top headlines ({label.strip()})")
            kwargs = {"page_size": 100}
            if country:
                kwargs["country"] = country
            if category:
                kwargs["category"] = category
            results = newsapi.get_top_headlines(**kwargs)

        raw_articles = results.get("articles", [])
        logger.info(f"[NewsAPI] Fetched {len(raw_articles)} articles")

        normalized = []
        for a in raw_articles:
            published_at_str = a.get("publishedAt")
            try:
                published_at = datetime.fromisoformat(
                    published_at_str.replace("Z", "+00:00")
                ) if published_at_str else datetime.utcnow()
            except Exception:
                published_at = datetime.utcnow()

            normalized.append({
                "title":        a.get("title"),
                "content":      a.get("content") or "",
                "url":          a.get("url"),
                "source_name":  (a.get("source") or {}).get("name", "Unknown"),
                "published_at": published_at,
                "image_url":    a.get("urlToImage"),
            })

        store_articles(normalized, topic_name)

        # Store raw payload
        from aggregator.models import RawArticlePayload
        raw = RawArticlePayload(
            source="newsapi",
            topic_name=topic_name,
            payload=json.dumps(results),
        )
        db.session.add(raw)
        db.session.commit()

    except Exception as e:
        logger.error(f"[NewsAPI] Error fetching {topic_name}: {e}")


def fetch_gnews(topic_name, query=None, category=None):
    """Fetch articles from GNews API and store them."""
    api_key = os.environ.get("GNEWS_API_KEY", "")
    if not api_key:
        logger.warning("GNEWS_API_KEY not set, skipping GNews fetch.")
        return

    try:
        if query:
            logger.info(f"[GNews] Fetching query: {query}")
            url = "https://gnews.io/api/v4/search"
            params = {
                "q":      query,
                "lang":   "en",
                "max":    20,
                "apikey": api_key,
            }
        elif category:
            logger.info(f"[GNews] Fetching category: {category}")
            url = "https://gnews.io/api/v4/top-headlines"
            params = {
                "category": category,
                "lang":     "en",
                "country":  "us",
                "max":      20,
                "apikey":   api_key,
            }
        else:
            logger.info(f"[GNews] Fetching top headlines")
            url = "https://gnews.io/api/v4/top-headlines"
            params = {
                "lang":    "en",
                "country": "us",
                "max":     20,
                "apikey":  api_key,
            }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        raw_articles = data.get("articles", [])
        logger.info(f"[GNews] Fetched {len(raw_articles)} articles")

        normalized = []
        for a in raw_articles:
            published_at_str = a.get("publishedAt")
            try:
                published_at = datetime.fromisoformat(
                    published_at_str.replace("Z", "+00:00")
                ) if published_at_str else datetime.utcnow()
            except Exception:
                published_at = datetime.utcnow()

            source = a.get("source") or {}
            normalized.append({
                "title":        a.get("title"),
                "content":      a.get("content") or a.get("description") or "",
                "url":          a.get("url"),
                "source_name":  source.get("name", "Unknown"),
                "published_at": published_at,
                "image_url":    a.get("image"),
            })

        store_articles(normalized, topic_name)

        # Store raw payload
        from aggregator.models import RawArticlePayload
        raw = RawArticlePayload(
            source="gnews",
            topic_name=topic_name,
            payload=json.dumps(data),
        )
        db.session.add(raw)
        db.session.commit()

    except Exception as e:
        logger.error(f"[GNews] Error fetching {topic_name}: {e}")
    

def regroup_ungrouped_stories():
    """
    Find single-article stories from the last 7 days and attempt
    to re-group them using the vector similarity matcher.
    """
    from news_fetcher.story_grouper import find_matching_story

    cutoff = datetime.utcnow() - timedelta(days=7)

    # Find stories that only have one article
    all_recent = Story.query.filter(Story.created_at >= cutoff).all()
    ungrouped_stories = [s for s in all_recent if len(s.articles) == 1]

    if not ungrouped_stories:
        logger.info("No single-article stories to re-group.")
        return

    logger.info(f"Checking {len(ungrouped_stories)} single-article stories for potential matches...")

    # Potential targets for merging (stories with > 1 article)
    multi_article_stories = [s for s in all_recent if len(s.articles) > 1]

    merged = 0
    for story in ungrouped_stories:
        if not story.articles:
            continue

        article = story.articles[0]
        if article.embedding is None:
            continue

        # Try to match to an existing multi-article story
        matched = find_matching_story(article.title, article.embedding, multi_article_stories, article_content=article.content)

        if matched and matched.id != story.id:
            logger.info(f"  [Re-group] Merging '{story.title}' into '{matched.title}'")

            # Move article to matched story
            article.story_id = matched.id
            db.session.flush()

            # Merge topic tags
            for topic in story.topics:
                if topic not in matched.topics:
                    matched.topics.append(topic)

            # Generate/Update headline for the matched story now that it has a new article
            from news_fetcher.headline_generator import generate_story_headline
            headline = generate_story_headline(matched)
            if headline:
                matched.headline = headline

            # Delete the now-empty story
            db.session.delete(story)
            merged += 1

    db.session.commit()
    logger.info(f"Re-grouping complete. Merged {merged} stories.")


def generate_missing_deep_reports(batch_size=5):
    """Find multi-article stories picked for headlines that don't have deep reports."""
    if not check_ollama_status():
        logger.info("Ollama offline, skipping deep report generation.")
        return

    from sqlalchemy import func
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=2)

    # Only target stories with a headline_score > 0 (meaning they were picked by the ranker)
    undissected = Story.query.join(Article).group_by(Story.id).having(
        func.count(Article.id) >= 2
    ).filter(
        Story.headline_score > 0,
        Story.created_at >= cutoff,
        (Story.deep_report == None) | (Story.deep_report == "")
    ).order_by(Story.headline_score.desc()).limit(batch_size).all()

    if not undissected:
        logger.info("No headline stories need deep reports.")
        return

    logger.info(f"Generating deep reports for {len(undissected)} headline stories...")
    from news_fetcher.summarizer import generate_deep_report
    for story in undissected:
        report = generate_deep_report(story)
        if report:
            story.deep_report = report
            logger.info(f"  Generated deep report for: {story.title[:60]}")
    
    db.session.commit()
    logger.info("Finished deep report batch.")


def generate_missing_embeddings(batch_size=50):
    """Generate embeddings for articles that don't have one yet."""
    from news_fetcher.story_grouper import get_embedding

    missing = Article.query.filter(Article.embedding == None).limit(batch_size).all()

    if not missing:
        logger.info("All articles have embeddings.")
        return

    logger.info(f"Generating embeddings for {len(missing)} articles...")
    count = 0
    for article in missing:
        # Align with store_articles and force_regroup_all: use title + snippet
        from news_fetcher.story_grouper import strip_video_prefix
        clean_title = strip_video_prefix(article.title)
        embed_text = clean_title
        if article.content:
            from news_fetcher.summarizer import strip_html
            snippet = strip_html(article.content)[:200].strip()
            embed_text = f"{clean_title}. {snippet}"
        embedding = get_embedding(embed_text)
        if embedding is not None:
            article.embedding = embedding
            count += 1

    db.session.commit()
    logger.info(f"Generated {count} embeddings.")


def audit_existing_scrapes(batch_size=200):
    """
    Scan non-audited article content for bad scrapes — login walls, captchas,
    bot detection pages, and outlet-level duplicate content.
    Clears bad content and adds offending domains to the blocklist.
    """
    from news_fetcher.scraper import detect_bad_scrape, get_domain, add_to_blocklist
    import re

    articles = Article.query.filter(
        Article.scrape_audited == False,
        Article.content != None,
        Article.content != ""
    ).order_by(Article.outlet_id, Article.id).all()

    if not articles:
        logger.info("[Audit] No new articles to audit.")
        return

    logger.info(f"[Audit] Scanning {len(articles)} new articles for bad scrapes...")

    cleared = 0
    auto_blocked = set()

    for i, article in enumerate(articles):
        # Mark as audited immediately
        article.scrape_audited = True

        if not article.content:
            continue

        domain = get_domain(article.url)

        # If domain was already flagged this run, just clear the content
        if domain and domain in auto_blocked:
            article.content = None
            cleared += 1
            continue

        # Strong/weak indicator check
        is_bad, reason = detect_bad_scrape(article.content)
        if is_bad:
            logger.info(f"  [Audit] Bad scrape detected: {article.title[:60]} — {reason}")
            article.content = None
            cleared += 1
            if domain:
                add_to_blocklist(article.url, reason)
                auto_blocked.add(domain)
            continue

        # Duplicate content check
        is_dup, dup_reason = detect_duplicate_outlet_content(
            article.content, article.outlet_id, exclude_article_id=article.id
        )
        if is_dup:
            logger.info(f"  [Audit] Duplicate scrape detected: {article.title[:60]} — {dup_reason}")
            article.content = None
            cleared += 1
            if domain:
                add_to_blocklist(article.url, dup_reason)
                auto_blocked.add(domain)
            continue

        # Commit in batches
        if (i + 1) % batch_size == 0:
            db.session.commit()
            logger.info(f"  [Audit] Progress: {i + 1}/{len(articles)}, cleared {cleared} so far")

    db.session.commit()
    logger.info(f"[Audit] Complete. Cleared {cleared} articles, auto-blocked {len(auto_blocked)} domains.")


def force_resummarize_all(batch_size=20):
    """
    Force re-generate summaries and deep reports for all stories and articles
    using the updated specialized journalist personas.
    """
    if not check_ollama_status():
        logger.info("Ollama offline, skipping force re-summarization.")
        return

    logger.info("=== Force re-summarization starting ===")
    
    # 1. Update Story Summaries
    stories = Story.query.all()
    logger.info(f"Re-summarizing {len(stories)} stories...")
    for i, story in enumerate(stories):
        if not story.articles:
            continue
        summary = summarize_story(story)
        if summary:
            story.summary = summary
        
        if (i + 1) % batch_size == 0:
            db.session.commit()
            logger.info(f"  Progress (Stories): {i+1}/{len(stories)}")
    
    db.session.commit()

    # 2. Update Deep Reports
    from sqlalchemy import func
    multi_article_stories = Story.query.join(Article).group_by(Story.id).having(
        func.count(Article.id) >= 2
    ).all()
    logger.info(f"Re-analyzing {len(multi_article_stories)} multi-article stories (Deep Reports)...")
    for i, story in enumerate(multi_article_stories):
        report = generate_deep_report(story)
        if report:
            story.deep_report = report
        
        if (i + 1) % 5 == 0: # Deep reports are slower
            db.session.commit()
            logger.info(f"  Progress (Deep Reports): {i+1}/{len(multi_article_stories)}")
            
    db.session.commit()

    # 3. Update Article Summaries
    articles = Article.query.filter(Article.content != None).all()
    logger.info(f"Re-summarizing {len(articles)} articles...")
    for i, article in enumerate(articles):
        summary = summarize_article(article)
        if summary:
            article.summary = summary
        
        if (i + 1) % batch_size == 0:
            db.session.commit()
            logger.info(f"  Progress (Articles): {i+1}/{len(articles)}")

    db.session.commit()
    logger.info("=== Force re-summarization complete ===")


def force_regroup_all():
    """
    Force re-group ALL articles using vector similarity embeddings.
    Regenerates ALL embeddings first (to include content), then re-assigns every article
    to the best matching story.
    """
    from news_fetcher.story_grouper import get_embedding, find_matching_story

    if not check_ollama_status():
        logger.info("Ollama offline, skipping force re-group.")
        return

    logger.info("=== Force re-group starting ===")
    logger.info("  [Force Regroup] Step 1: Regenerating embeddings...")

    # Step 1: Regenerate embeddings for ALL articles to ensure content is included
    all_articles = Article.query.all()
    logger.info(f"Regenerating embeddings for {len(all_articles)} articles (this may take a while)...")
    
    for i, article in enumerate(all_articles):
        # Use title + snippet for better semantic matching
        from news_fetcher.story_grouper import strip_video_prefix
        clean_title = strip_video_prefix(article.title)
        embed_text = clean_title
        if article.content:
            from news_fetcher.summarizer import strip_html
            snippet = strip_html(article.content)[:200].strip()
            embed_text = f"{clean_title}. {snippet}"
        embedding = get_embedding(embed_text)
        if embedding is not None:
            article.embedding = embedding
        
        if (i + 1) % 50 == 0:
            db.session.commit()
            logger.info(f"  [Force Regroup] Embeddings progress: {i + 1}/{len(all_articles)}")

    db.session.commit()
    logger.info("Embeddings regenerated.")
    logger.info("  [Force Regroup] Step 2: Starting re-grouping loop...")

    # Step 2: Get all articles with embeddings (should be all of them now)
    # Re-query to be safe
    all_articles = Article.query.filter(Article.embedding != None).all()
    logger.info(f"Re-grouping {len(all_articles)} articles...")

    # Step 3: Delete all existing stories and re-create from scratch
    # First detach all articles from stories and clear topics
    for article in all_articles:
        article.story_id = None
        article.topics = [] # Clear in-memory topics to avoid IntegrityError on flush/commit
    db.session.flush()

    # Clear junction tables first to avoid foreign key violations
    db.session.execute(db.text("DELETE FROM story_topics"))
    db.session.execute(db.text("DELETE FROM article_topics"))
    db.session.flush()

    # Delete all stories
    Story.query.delete()
    db.session.flush()
    
    # CRITICAL: Expire all objects after bulk deletes so the identity map 
    # doesn't contain references to the deleted Story objects.
    db.session.expire_all()

    # Step 4: Re-group articles one by one and re-attach topics
    from news_fetcher.story_grouper import clean_story_title
    from news_fetcher.topic_classifier import classify_article
    from aggregator.models import Topic as TopicModel

    new_stories = []
    try:
        for i, article in enumerate(all_articles):
            matched = find_matching_story(
                article.title, article.embedding, new_stories, article_content=article.content
            )

            if matched:
                story = matched
            else:
                new_title = clean_story_title(article.title)
                story = Story(title=new_title, summary=None)
                db.session.add(story)
                db.session.flush()
                new_stories.append(story)
            
            # Re-attach article to story
            article.story = story
            # Maintain in-memory list so find_matching_story can see it
            if article not in story.articles:
                story.articles.append(article)

            # Re-attach topic tags
            topic_names = classify_article(article.title, article.content or "")
            for topic_name in topic_names:
                topic = TopicModel.query.filter_by(name=topic_name).first()
                if not topic:
                    topic = TopicModel(name=topic_name)
                    db.session.add(topic)
                    db.session.flush()
                
                # Since we cleared article.topics = [] above, this is safe
                if topic not in article.topics:
                    article.topics.append(topic)
                if topic not in story.topics:
                    story.topics.append(topic)

            # Commit in batches of 50
            if (i + 1) % 50 == 0:
                db.session.commit()
                logger.info(f"  [Force Regroup] Grouping progress: {i + 1}/{len(all_articles)}")

    except Exception as e:
        logger.error(f"  [Force Regroup] CRITICAL ERROR: {e}")
        import traceback
        logger.error(traceback.format_exc())
        db.session.rollback()
        raise

    db.session.commit()

    # Step 5: Generate headlines for all multi-article stories
    logger.info("Generating AI headlines for regrouped stories...")
    logger.info("  [Force Regroup] Step 3: Generating AI headlines...")
    generate_missing_headlines()

    logger.info(f"=== Force re-group complete. Created {len(new_stories)} stories. ===")


def reclassify_all_articles(batch_size=50):
    """
    Reclassify all existing articles into the new topic system using Ollama.
    Clears existing topic tags and reassigns based on content.
    """
    from news_fetcher.topic_classifier import classify_article
    from aggregator.models import Topic as TopicModel

    if not check_ollama_status():
        logger.info("Ollama offline, skipping reclassification.")
        return

    # Clear all existing topic assignments
    db.session.execute(db.text("DELETE FROM article_topics"))
    db.session.execute(db.text("DELETE FROM story_topics"))
    db.session.flush()
    db.session.expire_all() # Ensure stale collections are cleared
    logger.info("Cleared existing topic assignments.")

    all_articles = Article.query.all()
    total = len(all_articles)
    logger.info(f"Reclassifying {total} articles...")

    for i, article in enumerate(all_articles):
        # Clear in-memory topics for this article to be safe
        article.topics = []
        
        topic_names = classify_article(article.title, article.content or "")

        for topic_name in topic_names:
            topic = TopicModel.query.filter_by(name=topic_name).first()
            if not topic:
                topic = TopicModel(name=topic_name)
                db.session.add(topic)
                db.session.flush()
            
            if topic not in article.topics:
                article.topics.append(topic)
            
            if article.story:
                if topic not in article.story.topics:
                    article.story.topics.append(topic)

        # Commit in batches
        if (i + 1) % batch_size == 0:
            db.session.commit()
            logger.info(f"  Progress: {i + 1}/{total}")

    db.session.commit()
    logger.info(f"Reclassification complete. Processed {total} articles.")


def ollama_catchup():
    """
    Run all Ollama-dependent tasks that may have been skipped
    while Ollama was offline.
    """
    logger.info("=== Ollama catchup starting ===")
    audit_existing_scrapes()
    generate_missing_embeddings(batch_size=50)
    generate_missing_headlines()
    regroup_ungrouped_stories()
    retry_unrated_outlets()
    logger.info("=== Ollama catchup complete ===")


def cleanup_old_payloads():
    """Delete raw API payloads older than 30 days."""
    from aggregator.models import RawArticlePayload
    cutoff = datetime.utcnow() - timedelta(days=30)
    old = RawArticlePayload.query.filter(RawArticlePayload.fetched_at < cutoff).all()
    if old:
        logger.info(f"Deleting {len(old)} raw payloads older than 30 days...")
        for payload in old:
            db.session.delete(payload)
        db.session.commit()
        logger.info("Cleanup complete.")
    else:
        logger.info("No old payloads to clean up.")


def fetch_and_store_articles(topic_name, mode="top", query=None,
                              country="us", category=None,
                              gnews_query=None, gnews_category=None):
    """
    Main entry point. Fetches from both NewsAPI and GNews for a given topic.
    """
    fetch_newsapi(topic_name, mode=mode, query=query,
                  country=country, category=category)
    fetch_gnews(topic_name, query=gnews_query, category=gnews_category)
    cleanup_old_payloads()


def process_current_edition():
    """
    Exhaustively summarize and analyze ONLY the stories selected for the latest edition.
    1. Finds the most recent Edition.
    2. For every story in that edition:
       - Generates the Story Summary (if missing).
       - Generates the Deep Report (if multi-source and missing).
       - Generates a summary for EVERY article associated with that story.
    This ensures the static headlines site is fully populated.
    """
    from news_fetcher.summarizer import (
        summarize_story, generate_deep_report, summarize_article, check_ollama_status
    )
    from aggregator.models import Edition, EditionStory

    if not check_ollama_status():
        logger.info("[Processor] Ollama offline, skipping edition processing.")
        return

    latest_edition = Edition.query.order_by(Edition.created_at.desc()).first()
    if not latest_edition:
        logger.info("[Processor] No edition found to process.")
        return

    stories = [es.story for es in latest_edition.edition_stories.order_by(EditionStory.rank).all()]
    
    logger.info(f"[Processor] Processing {len(stories)} stories from {latest_edition.edition_type} edition...")

    STALE_ARTICLE_THRESHOLD = 3  # new articles needed to trigger reanalysis

    for story in stories:
        article_count = len(story.articles)
        if article_count == 0:
            continue

        try:
            # Check if analysis is stale — enough new articles arrived
            # since the last time this story was summarized
            is_stale = False
            if story.summary_generated_at and article_count >= 2:
                new_article_count = sum(
                    1 for a in story.articles
                    if a.fetched_at and a.fetched_at > story.summary_generated_at
                )
                if new_article_count >= STALE_ARTICLE_THRESHOLD:
                    logger.info(
                        f"  [Processor] Story stale ({new_article_count} new articles "
                        f"since last analysis): {story.title[:60]}"
                    )
                    story.summary = None
                    story.deep_report = None
                    is_stale = True

            # 1. Process Story-level Summaries
            if article_count >= 2:
                if not story.summary:
                    summary = summarize_story(story)
                    if summary:
                        story.summary = summary
                        story.summary_generated_at = datetime.utcnow()
                        logger.info(f"  [Processor] Story summary: {story.title[:60]}")

                if not story.deep_report:
                    report = generate_deep_report(story)
                    if report:
                        story.deep_report = report
                        logger.info(f"  [Processor] Deep report: {story.title[:60]}")
            else:
                # Single-article story: Ensure story summary exists
                if not story.summary:
                    art = story.articles[0]
                    summary = art.summary or summarize_article(art)
                    if summary:
                        art.summary = summary
                        story.summary = summary
                        story.summary_generated_at = datetime.utcnow()
                        logger.info(f"  [Processor] Single-source summary: {story.title[:60]}")

                # Ensure old stories that once had multiple articles (and thus a deep_report)
                # are cleaned up when they later appear as single-article stories.
                story.deep_report = None

            db.session.commit()
            # This is critical for the static site links
            for article in story.articles:
                if not article.summary and article.content:
                    summary = summarize_article(article)
                    if summary:
                        article.summary = summary
                        logger.info(f"    [Processor] Child article summary: {article.title[:60]}")

            db.session.commit()

        except Exception as e:
            logger.error(f"  [Processor] Error processing story {story.id}: {e}")
            db.session.rollback()

    logger.info("[Processor] Current edition processing complete.")


def sync_allsides_ratings():
    """
    Sync all outlets against the AllSides lookup table.
    - Upgrades Ollama-rated outlets to AllSides ratings where a match exists
    - Updates outlets whose AllSides score has changed since last sync
    - Propagates any score changes to all articles for that outlet
    Run monthly via scheduler, or manually via admin menu.
    """
    from news_fetcher.allsides_lookup import get_allsides_score
    from aggregator.models import Outlet

    logger.info("=== AllSides sync starting ===")

    outlets = Outlet.query.all()
    updated = 0
    skipped = 0

    for outlet in outlets:
        as_score = get_allsides_score(outlet.name)

        if as_score is None:
            skipped += 1
            continue

        score_changed = outlet.allsides_bias_score != as_score
        not_yet_allsides = outlet.bias_source != "allsides"

        if score_changed or not_yet_allsides:
            old_score = outlet.bias_score
            outlet.bias_score = as_score
            outlet.allsides_bias_score = as_score
            outlet.bias_source = "allsides"
            outlet.bias_retry_count = 0

            for article in outlet.articles:
                article.bias_score = as_score

            logger.info(
                f"  [AllSides Sync] {outlet.name}: "
                f"{old_score} -> {as_score} "
                f"({'upgraded from AI' if not_yet_allsides else 'score updated'})"
            )
            updated += 1

    db.session.commit()
    logger.info(f"=== AllSides sync complete. Updated {updated}, no match for {skipped} outlets. ===")


def publish_edition():
    """
    Create an Edition record for the current fetch cycle.
    Determines edition type (night/morning/afternoon/evening) from Eastern time.
    Only includes stories that are new since the last edition, or have received
    new articles since then. Falls back to best available if fewer than 20 eligible.
    Skips if this edition slot already exists.
    """
    from zoneinfo import ZoneInfo
    from aggregator.models import Edition, Story, EditionStory

    eastern = ZoneInfo('America/New_York')
    now_eastern = datetime.now(eastern)
    hour = now_eastern.hour
    today = now_eastern.date()

    if 7 <= hour < 12:
        edition_type = 'morning'
    elif 12 <= hour < 17:
        edition_type = 'afternoon'
    elif 17 <= hour < 22:
        edition_type = 'evening'
    else:
        edition_type = 'night'

    # Skip if this edition slot already published
    existing = Edition.query.filter_by(date=today, edition_type=edition_type).first()
    if existing:
        logger.info(f"[Edition] {edition_type} edition for {today} already published, skipping.")
        return

    # Look back across all editions published in the last 24 hours
    recent_cutoff = datetime.utcnow() - timedelta(hours=24)
    recent_editions = Edition.query.filter(
        Edition.created_at >= recent_cutoff,
        Edition.published == True
    ).order_by(Edition.created_at.desc()).all()

    prev_story_ids = set()
    for ed in recent_editions:
        for es in ed.edition_stories.all():
            prev_story_ids.add(es.story_id)

    # Most recent edition's timestamp is used for the new-articles check
    prev_edition = Edition.query.filter(
        Edition.published == True
    ).order_by(Edition.created_at.desc()).first()
    prev_published_at = prev_edition.created_at if prev_edition else None

    # Get top 50 scored stories as candidates
    story_cutoff = datetime.utcnow() - timedelta(days=3)
    candidates = Story.query.filter(
        Story.headline_score > 0,
        Story.created_at >= story_cutoff
    ).order_by(Story.headline_score.desc()).limit(50).all()

    # Exclude single-article stories with no scraped content
    candidates = [
        s for s in candidates
        if not (
            len(s.articles) == 1 and
            not (s.articles[0].content or '').strip()
        )
    ]

    eligible = []
    carried = []
    seen_story_ids = set()

    for story in candidates:
        if story.id in seen_story_ids:
            continue
        seen_story_ids.add(story.id)

        if story.id not in prev_story_ids:
            # New story not in previous edition
            eligible.append(story)
        elif prev_published_at:
            # Story was in previous edition — only include if new articles arrived
            new_articles = [
                a for a in story.articles
                if a.fetched_at and a.fetched_at > prev_published_at
            ]
            if new_articles:
                eligible.append(story)
            else:
                carried.append(story)

    # Fill to 20 with carried-over stories if needed.
    # Only carry stories that are less than 48 hours old to prevent
    # ancient high-scored stories from recycling indefinitely.
    carry_cutoff = datetime.utcnow() - timedelta(hours=48)
    fresh_carried = [s for s in carried if s.created_at >= carry_cutoff]
    if len(eligible) < 20:
        slots_left = 20 - len(eligible)
        eligible.extend(fresh_carried[:slots_left])

    # Final dedup safety net — ensures no story_id appears twice
    # regardless of how eligible was built
    seen = set()
    deduped = []
    for s in eligible:
        if s.id not in seen:
            seen.add(s.id)
            deduped.append(s)
    top_20 = deduped[:20]

    if not top_20:
        logger.warning(f"[Edition] No stories available for {edition_type} edition on {today}.")
        return

    edition = Edition(date=today, edition_type=edition_type)
    db.session.add(edition)
    db.session.flush()

    for rank, story in enumerate(top_20, 1):
        es = EditionStory(
            edition_id=edition.id,
            story_id=story.id,
            rank=rank,
            headline_score_at_publish=story.headline_score
        )
        db.session.add(es)

    db.session.commit()
    logger.info(
        f"[Edition] Published {edition_type} edition for {today} "
        f"with {len(top_20)} stories ({len(eligible) - len(top_20[:len(eligible)])} carried over)."
    )


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        fetch_and_store_articles("US Politics")

# news_fetcher/rss_fetcher.py

import feedparser
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

RSS_FEEDS = [
    # Wire services / Center
    "https://feeds.apnews.com/rss/topnews",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.npr.org/1001/rss.xml",
    "https://www.pbs.org/newshour/feeds/rss/headlines",
    "https://www.economist.com/the-world-this-week/rss.xml",
    # Center-Left
    "https://rss.cnn.com/rss/edition.rss",
    "https://feeds.nbcnews.com/nbcnews/public/news",
    "https://feeds.washingtonpost.com/rss/world",
    "https://www.nytimes.com/svc/collections/v1/publish/https://www.nytimes.com/section/world/rss.xml",
    "https://www.theguardian.com/world/rss",
    # Center-Right
    "https://moxie.foxnews.com/google-publisher/latest.xml",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
    "https://nypost.com/feed/",
    # Political / Neutral
    "https://thehill.com/feed/",
    "https://api.axios.com/feed/",
    "https://rss.politico.com/politics-news.xml",
    # International / Additional Networks
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.abcnews.com/abcnews/topstories",
    "https://www.cbsnews.com/latest/rss/main",
]


def _parse_published(entry):
    """Convert feedparser's published_parsed struct_time to datetime."""
    try:
        if entry.get("published_parsed"):
            return datetime(*entry.published_parsed[:6])
        if entry.get("updated_parsed"):
            return datetime(*entry.updated_parsed[:6])
    except Exception:
        pass
    return datetime.utcnow()


def _extract_image(entry):
    """Try to pull an image URL from various RSS media fields."""
    media = entry.get("media_content", [])
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url
    enclosures = entry.get("enclosures", [])
    if enclosures:
        url = enclosures[0].get("url") or enclosures[0].get("href")
        if url:
            return url
    thumbnail = entry.get("media_thumbnail", [])
    if thumbnail and isinstance(thumbnail, list):
        url = thumbnail[0].get("url")
        if url:
            return url
    return None


def fetch_feed(feed_url):
    """
    Fetch a single RSS feed.
    Returns (source_name, list of normalized article dicts).
    """
    try:
        feed = feedparser.parse(feed_url)
        source_name = (
            feed.feed.get("title", feed_url.split("/")[2])
            if feed.feed else feed_url.split("/")[2]
        )

        articles = []
        for entry in feed.entries[:30]:
            title = entry.get("title", "").strip()
            url = entry.get("link", "").strip()
            if not title or not url:
                continue

            # Pass description as fallback content — scraper will attempt full text first
            content = entry.get("summary", "") or entry.get("description", "") or ""

            articles.append({
                "title":        title,
                "content":      content,
                "url":          url,
                "source_name":  source_name,
                "published_at": _parse_published(entry),
                "image_url":    _extract_image(entry),
            })

        logger.info(f"  [RSS] Got {len(articles)} articles from {feed_url[:60]}")
        return source_name, articles

    except Exception as e:
        logger.warning(f"  [RSS] Failed to fetch {feed_url[:60]}: {e}")
        return None, []


def fetch_and_store_rss():
    """
    Fetch all RSS feeds and store articles via the normal ingestion pipeline.
    Each article goes through the same dedup, scraping, embedding, topic
    classification, and story grouping as NewsAPI/GNews articles.
    Must be called within a Flask app context.
    """
    from news_fetcher.fetch_and_store_articles import store_articles

    logger.info("=== RSS fetch starting ===")
    total = 0

    for feed_url in RSS_FEEDS:
        source_name, articles = fetch_feed(feed_url)
        if articles:
            store_articles(articles, "Global News")
            total += len(articles)

    logger.info(f"=== RSS fetch complete. Processed {total} articles. ===")

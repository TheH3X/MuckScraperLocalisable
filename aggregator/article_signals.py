import math
import re
from datetime import datetime as dt
from urllib.parse import urlparse


# Paywall / login / captcha phrases reused for frank accessibility checks.
# Kept here (not imported from news_fetcher.scraper) to avoid heavy scraper deps
# in display/summary code paths.
STRONG_WALL_INDICATORS = (
    "unusual activity detected",
    "verify you are human",
    "enable javascript to continue",
    "you have been blocked",
    "access to this page has been denied",
    "please sign in to continue",
    "subscribe to continue reading",
    "please verify you're not a robot",
    "complete the security check",
    "403 forbidden",
    "this content is for subscribers",
    "create a free account to read",
    "sign up to read",
    "your access to this article",
    "to continue reading, please",
    "this article is for paying subscribers",
    "subscribers only",
    "become a subscriber",
    "unlock this article",
    "paywall",
)

WEAK_WALL_INDICATORS = (
    "sign in",
    "log in",
    "subscribe",
    "premium content",
)

ACCESSIBLE_SCRAPE_STATUSES = frozenset({"success", "fallback"})
INACCESSIBLE_SCRAPE_STATUSES = frozenset({
    "blocked", "failed", "skipped", "pending",
})
AUTH_HTTP_STATUSES = frozenset({401, 403})
FAILURE_REASON_WALL_HINTS = (
    "paywall",
    "login",
    "sign_in",
    "sign in",
    "subscriber",
    "blocked",
    "captcha",
    "forbidden",
    "http_401",
    "http_403",
    "domain_blocked",
    "bad scrape",
)
MIN_ACCESSIBLE_CONTENT_CHARS = 500

ROUNDUP_TITLE_PATTERNS = (
    re.compile(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?:"),
    re.compile(r"\b(?:morning|afternoon|evening|night)\s+(?:rundown|roundup|briefing)\b", re.IGNORECASE),
    re.compile(r"\b(?:daily|news)\s+(?:rundown|roundup|briefing)\b", re.IGNORECASE),
    re.compile(r"\btop stories\b", re.IGNORECASE),
    re.compile(r"\bwhat to know\b", re.IGNORECASE),
)

ROUNDUP_URL_HINTS = (
    "morning-rundown",
    "evening-rundown",
    "nightly-rundown",
    "roundup",
    "briefing",
    "top-stories",
)

LOW_VALUE_URL_HINTS = (
    "/video/",
    "/videos/",
    "/watch/",
    "/live/",
    "/live-updates/",
    "/liveblog/",
    "/podcast/",
    "/podcasts/",
    "/audio/",
    "/listen/",
    "/photos/",
    "/photo/",
    "/gallery/",
    "/galleries/",
    "/sounds/",
    "/iplayer/",
    "/newsletters/",
    "/newsletter/",
    "/briefings/",
    "/opinion/letters/",
)

LOW_VALUE_TITLE_PATTERNS = (
    re.compile(r"^\s*(?:watch|video|listen)\s*:", re.IGNORECASE),
    re.compile(r"\bletters?\s+to\s+the\s+editor\b", re.IGNORECASE),
    re.compile(r"\blive updates?\b", re.IGNORECASE),
    re.compile(r"\bphoto(?:s| gallery)?\b", re.IGNORECASE),
    re.compile(r"\bgallery\b", re.IGNORECASE),
    re.compile(r"\bnewsletter\b", re.IGNORECASE),
)


def is_roundup_article(title=None, url=None):
    normalized_title = (title or "").strip()
    if any(pattern.search(normalized_title) for pattern in ROUNDUP_TITLE_PATTERNS):
        return True

    parsed_path = urlparse(url or "").path.lower()
    return any(hint in parsed_path for hint in ROUNDUP_URL_HINTS)


def bias_bucket_for_score(score):
    if score is None:
        return "unrated"
    
    bucket = int(round(score))
    if bucket < 1: bucket = 1
    if bucket > 5: bucket = 5
    return str(bucket)


def bias_side_for_score(score):
    """
    Map a numeric bias score to a coarse editorial side for balance logic.
    1-2 = leftish, 3 = center, 4-5 = rightish, None = unrated.
    """
    if score is None:
        return "unrated"
    bucket = int(round(score))
    if bucket <= 2:
        return "leftish"
    if bucket == 3:
        return "center"
    return "rightish"


def low_value_article_reason(title=None, url=None):
    if is_roundup_article(title, url):
        return "roundup"

    normalized_title = (title or "").strip()
    if any(pattern.search(normalized_title) for pattern in LOW_VALUE_TITLE_PATTERNS):
        return "low_value_title"

    parsed = urlparse(url or "")
    parsed_path = parsed.path.lower()

    if any(hint in parsed_path for hint in LOW_VALUE_URL_HINTS):
        return "low_value_url"

    if parsed_path.endswith((".m3u8", ".mp4", ".m4v", ".mov", ".webm")):
        return "video_asset"

    return None


def _strip_html_plain(text):
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def detect_paywall_or_login_wall(content):
    """
    Scan article HTML/text for paywall, login, or captcha copy.
    Returns (is_wall: bool, reason: str or None).
    """
    if not content:
        return False, None

    clean = _strip_html_plain(content).lower()
    if not clean:
        return False, None

    for indicator in STRONG_WALL_INDICATORS:
        if indicator in clean:
            return True, f"paywall_or_login: strong indicator '{indicator}'"

    if len(clean) < 300:
        for indicator in WEAK_WALL_INDICATORS:
            if indicator in clean:
                return True, (
                    f"paywall_or_login: weak indicator '{indicator}' "
                    f"in short content ({len(clean)} chars)"
                )

    return False, None


def accessibility_failure_reason(article, *, for_lead=True):
    """
    Frank reasons an article must not be treated as readable / lead-eligible.
    Returns None when accessible.
    """
    status = (getattr(article, "scrape_status", None) or "pending").lower()
    if status in INACCESSIBLE_SCRAPE_STATUSES:
        return f"scrape_status={status}"

    if status not in ACCESSIBLE_SCRAPE_STATUSES:
        return f"scrape_status={status or 'unknown'}"

    http_status = getattr(article, "scrape_http_status", None)
    try:
        http_status = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        http_status = None
    if http_status in AUTH_HTTP_STATUSES:
        return f"http_status={http_status}"

    failure_reason = (getattr(article, "scrape_failure_reason", None) or "").lower()
    if failure_reason:
        for hint in FAILURE_REASON_WALL_HINTS:
            if hint in failure_reason:
                return f"scrape_failure_reason={failure_reason[:120]}"

    content = getattr(article, "content", None) or ""
    is_wall, wall_reason = detect_paywall_or_login_wall(content)
    if is_wall:
        return wall_reason

    plain = _strip_html_plain(content)
    if len(plain) < MIN_ACCESSIBLE_CONTENT_CHARS:
        return f"content_too_short ({len(plain)} chars)"

    if for_lead:
        low_value = low_value_article_reason(
            getattr(article, "title", None),
            getattr(article, "url", None),
        )
        if low_value:
            return f"low_value={low_value}"

    return None


def is_article_accessible(article, *, for_lead=True):
    """True when the article clears frank paywall/accessibility checks."""
    return accessibility_failure_reason(article, for_lead=for_lead) is None


def accessible_articles(articles, *, for_lead=True):
    """Filter an iterable to accessible articles only."""
    return [a for a in articles if is_article_accessible(a, for_lead=for_lead)]


def _is_aggregator_article(article):
    outlet = getattr(article, "outlet", None)
    outlet_name = getattr(outlet, "name", None) or "" if outlet is not None else ""
    if not outlet_name:
        return False
    try:
        from aggregator.constants import AGGREGATORS
        return any(agg in outlet_name for agg in AGGREGATORS)
    except Exception:
        return False


def _cosine_similarity(vec1, vec2):
    try:
        a = list(vec1)
        b = list(vec2)
    except TypeError:
        return 0.0
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        norm_a += fx * fx
        norm_b += fy * fy
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _content_length(article):
    return len(_strip_html_plain(getattr(article, "content", None) or ""))


def _article_embedding(article):
    emb = getattr(article, "embedding", None)
    if emb is None:
        return None
    try:
        return list(emb)
    except TypeError:
        return None


def article_substance_score(article):
    """Length-based substance with a bonus for a successful full scrape."""
    length = _content_length(article)
    status = (getattr(article, "scrape_status", None) or "").lower()
    scrape_mult = 2.0 if status == "success" else 1.0
    return math.log1p(length) * scrape_mult


def article_centrality_score(article, peers):
    """Mean cosine similarity of this article's embedding to other peers."""
    self_emb = _article_embedding(article)
    if self_emb is None:
        return 0.0
    scores = []
    for other in peers:
        if other is article:
            continue
        other_emb = _article_embedding(other)
        if other_emb is None:
            continue
        scores.append(_cosine_similarity(self_emb, other_emb))
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def best_article_score(article, peers):
    """
    Substance × centrality score for settled / non-developing lead picks.
    best_score = substance * (0.5 + 0.5 * centrality)
    """
    substance = article_substance_score(article)
    centrality = article_centrality_score(article, peers)
    return substance * (0.5 + 0.5 * centrality)


def is_developing_story(story, edition_story=None):
    """
    Match public._story_kind developing heuristic without edition rank logic.
    Developing when has_updates on an edition slot, or cluster has ≥3 articles.
    """
    if edition_story is not None and getattr(edition_story, "has_updates", False):
        return True
    articles = list(getattr(story, "articles", None) or [])
    return len(articles) >= 3


def select_lead_article(story, edition_story=None, candidates=None):
    """
    Pick the external lead among accessible articles only.

    Developing → newest by published date (among accessible).
    Otherwise → highest substance × embedding-centrality score.
    Prefer non-aggregator outlets; fall back to accessible aggregators if needed.
    Returns None when no accessible candidate exists.
    """
    if candidates is None:
        display = getattr(story, "display_articles", None)
        pool = list(display) if display is not None else list(getattr(story, "articles", None) or [])
    else:
        pool = list(candidates)

    accessible = accessible_articles(pool, for_lead=True)
    if not accessible:
        return None

    non_aggregators = [a for a in accessible if not _is_aggregator_article(a)]
    pool = non_aggregators or accessible

    developing = is_developing_story(story, edition_story=edition_story)

    def _date_key(article):
        return getattr(article, "date", None) or dt.min

    def _success_key(article):
        status = (getattr(article, "scrape_status", None) or "").lower()
        return 1 if status == "success" else 0

    if developing:
        return max(pool, key=lambda a: (_date_key(a), _success_key(a), _content_length(a)))

    return max(
        pool,
        key=lambda a: (
            best_article_score(a, pool),
            _success_key(a),
            _date_key(a),
        ),
    )


def story_earns_deep_report(story):
    """
    Deep reports are earned by multi-outlet contested coverage, not headline_score alone.
    Requires ≥3 articles and mixed left+right bias coverage.
    """
    articles = list(getattr(story, "articles", None) or [])
    if len(articles) < 3:
        return False

    counts = {"leftish": 0, "rightish": 0}
    for article in articles:
        score = getattr(article, "bias_score", None)
        if score is None:
            outlet = getattr(article, "outlet", None)
            if outlet is not None:
                score = getattr(outlet, "bias_score", None)
        side = bias_side_for_score(score)
        if side in counts:
            counts[side] += 1
    return bool(counts["leftish"]) and bool(counts["rightish"])

import re
from urllib.parse import urlparse


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

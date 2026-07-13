from datetime import datetime as dt

from aggregator.article_signals import (
    accessibility_failure_reason,
    bias_side_for_score,
    is_article_accessible,
    select_lead_article,
)
from aggregator.constants import AGGREGATORS


def apply_aggregator_filter(story, edition_story=None):
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

    # Accessible sources first; inaccessible last with frank reason labels.
    accessible = []
    inaccessible = []
    for art in story.display_articles:
        reason = accessibility_failure_reason(art, for_lead=False)
        if reason is None:
            accessible.append(art)
        else:
            art.accessibility_reason = reason
            inaccessible.append(art)
    # Keep relative date order within each bucket.
    story.display_articles = accessible + inaccessible
    for art in accessible:
        art.accessibility_reason = None

    lead = select_lead_article(story, edition_story=edition_story)
    story.lead_article = lead
    if lead is not None:
        # Surface the lead first among accessible sources.
        reordered = [lead] + [a for a in story.display_articles if a is not lead]
        story.display_articles = reordered

    # Collect unique outlets for display
    unique_outlets = []
    seen_outlet_ids = set()
    for art in story.display_articles:
        if art.outlet_id and art.outlet_id not in seen_outlet_ids:
            unique_outlets.append(art.outlet)
            seen_outlet_ids.add(art.outlet_id)
    story.unique_outlets = unique_outlets

    status_counts = {
        "success": 0,
        "fallback": 0,
        "blocked": 0,
    }
    for article in story.display_articles:
        status = (article.scrape_status or "blocked").lower()
        if status == "success":
            status_counts["success"] += 1
        elif status == "fallback":
            status_counts["fallback"] += 1
        else:
            status_counts["blocked"] += 1

    total_articles = len(story.display_articles)
    readable_articles = status_counts["success"] + status_counts["fallback"]
    story.scrape_quality = {
        "total": total_articles,
        "success": status_counts["success"],
        "fallback": status_counts["fallback"],
        "blocked": status_counts["blocked"],
        "readable_pct": round((readable_articles / total_articles) * 100) if total_articles else 0,
        "full_pct": round((status_counts["success"] / total_articles) * 100) if total_articles else 0,
        "accessible_count": sum(
            1 for a in story.display_articles if is_article_accessible(a, for_lead=False)
        ),
    }


def compute_bias_breakdown(story):
    """
    Count a story's articles by editorial side (leftish/center/rightish/unrated),
    falling back to outlet bias when an article has no bias score of its own.
    Returns a dict with counts and percentages for rendering a balance bar.
    """
    counts = {"leftish": 0, "center": 0, "rightish": 0, "unrated": 0}
    for article in story.articles:
        score = article.bias_score
        if score is None and article.outlet:
            score = article.outlet.bias_score
        side = bias_side_for_score(score)
        counts[side] = counts.get(side, 0) + 1

    total = len(story.articles) or 1
    return {
        "counts": counts,
        "percents": {side: round((count / total) * 100) for side, count in counts.items()},
        "has_mixed_coverage": bool(counts["leftish"]) and bool(counts["rightish"]),
    }

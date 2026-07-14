from datetime import datetime as dt

from aggregator.article_signals import (
    accessibility_failure_reason,
    is_article_accessible,
    select_lead_article,
)
from aggregator.constants import AGGREGATORS
from aggregator.outlet_prefs import (
    current_prefs_map,
    filter_articles_by_prefs,
    sort_articles_by_prefs,
)


def apply_aggregator_filter(story, edition_story=None, outlet_prefs=None):
    if outlet_prefs is None:
        try:
            outlet_prefs = current_prefs_map()
        except RuntimeError:
            # Outside a request context (scheduler / publish snapshot).
            outlet_prefs = {}

    originals = []
    aggregators = []
    has_good_original = False
    sorted_articles = sorted(
        filter_articles_by_prefs(story.articles, outlet_prefs),
        key=lambda x: x.date or dt.min,
        reverse=True,
    )
    seen_articles = set()
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
    # Keep relative date order within each bucket, then boost preferred outlets.
    story.display_articles = (
        sort_articles_by_prefs(accessible, outlet_prefs)
        + sort_articles_by_prefs(inaccessible, outlet_prefs)
    )
    for art in accessible:
        art.accessibility_reason = None

    lead = select_lead_article(story, edition_story=edition_story)
    # If lead came from a muted outlet (edition snapshot), fall back to first display article.
    if lead is not None and lead.outlet_id and outlet_prefs.get(lead.outlet_id) == "mute":
        lead = story.display_articles[0] if story.display_articles else None
    elif lead is None and story.display_articles:
        # Prefer a preferred outlet as lead when present among accessible articles.
        for art in story.display_articles:
            if art.outlet_id and outlet_prefs.get(art.outlet_id) == "prefer":
                if accessibility_failure_reason(art, for_lead=True) is None:
                    lead = art
                    break
    story.lead_article = lead
    if lead is not None:
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
    story.hidden_by_prefs = len(story.articles) > 0 and len(story.display_articles) == 0

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

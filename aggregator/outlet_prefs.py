"""Per-user outlet preference helpers (allow / prefer / mute)."""

from datetime import datetime, timedelta

from flask import g
from flask_login import current_user
from sqlalchemy import desc, func

from aggregator import db
from aggregator.models import Article, Edition, EditionStory, Outlet, UserOutletPreference

VALID_PREFERENCES = frozenset({"allow", "prefer", "mute"})


def prefs_map_for_user(user):
    """Return {outlet_id: preference} for a user (empty if anonymous/missing)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return {}
    rows = UserOutletPreference.query.filter_by(user_id=user.id).all()
    return {row.outlet_id: row.preference for row in rows}


def current_prefs_map():
    """Request-cached prefs for the logged-in user."""
    cached = getattr(g, "_outlet_prefs_map", None)
    if cached is not None:
        return cached
    prefs = prefs_map_for_user(current_user) if current_user.is_authenticated else {}
    g._outlet_prefs_map = prefs
    return prefs


def status_for_outlet(outlet_or_id, prefs=None):
    """Return allow|prefer|mute|unrated for an outlet."""
    if outlet_or_id is None:
        return "unrated"
    outlet_id = outlet_or_id if isinstance(outlet_or_id, int) else getattr(outlet_or_id, "id", None)
    if outlet_id is None:
        return "unrated"
    if prefs is None:
        prefs = current_prefs_map()
    return prefs.get(outlet_id, "unrated")


def set_outlet_preference(user, outlet_id, preference):
    """
    Set or clear a preference.
    preference: allow|prefer|mute, or None/''/'clear' to remove (back to unrated).
    """
    if preference in (None, "", "clear", "unrated"):
        existing = UserOutletPreference.query.filter_by(
            user_id=user.id, outlet_id=outlet_id
        ).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()
        current_prefs_map().pop(outlet_id, None)
        return None

    if preference not in VALID_PREFERENCES:
        raise ValueError(f"Invalid preference: {preference}")

    row = UserOutletPreference.query.filter_by(
        user_id=user.id, outlet_id=outlet_id
    ).first()
    if row:
        row.preference = preference
        row.updated_at = datetime.utcnow()
    else:
        row = UserOutletPreference(
            user_id=user.id,
            outlet_id=outlet_id,
            preference=preference,
        )
        db.session.add(row)
    db.session.commit()
    current_prefs_map()[outlet_id] = preference
    return row


def filter_articles_by_prefs(articles, prefs=None):
    """Drop articles from muted outlets. Prefer keeps them (boost happens separately)."""
    if prefs is None:
        prefs = current_prefs_map()
    if not prefs:
        return list(articles)
    kept = []
    for article in articles:
        oid = article.outlet_id
        if oid is not None and prefs.get(oid) == "mute":
            continue
        kept.append(article)
    return kept


def sort_articles_by_prefs(articles, prefs=None):
    """Preferred outlets first, then allow/unrated, preserving relative order."""
    if prefs is None:
        prefs = current_prefs_map()
    if not prefs:
        return list(articles)

    def rank(article):
        status = prefs.get(article.outlet_id, "unrated")
        if status == "prefer":
            return 0
        if status == "allow":
            return 1
        return 2

    indexed = list(enumerate(articles))
    indexed.sort(key=lambda pair: (rank(pair[1]), pair[0]))
    return [article for _, article in indexed]


def preferred_outlet_boost(story, prefs=None):
    """Small score boost when a story includes a preferred outlet."""
    if prefs is None:
        prefs = current_prefs_map()
    if not prefs:
        return 0
    for article in story.articles:
        if article.outlet_id and prefs.get(article.outlet_id) == "prefer":
            return 1
    return 0


def unrated_outlets_recent(user, days=14, limit=50):
    """
    Outlets that appeared in recent editions (or recent articles) and have no
    preference set for this user — the triage queue.
    """
    prefs = prefs_map_for_user(user)
    rated_ids = set(prefs.keys())
    cutoff = datetime.utcnow() - timedelta(days=days)

    # Prefer outlets that showed up in published editions recently.
    edition_outlet_ids = (
        db.session.query(Article.outlet_id, func.count(Article.id).label("cnt"))
        .join(EditionStory, EditionStory.story_id == Article.story_id)
        .join(Edition, Edition.id == EditionStory.edition_id)
        .filter(
            Edition.published.is_(True),
            Edition.created_at >= cutoff,
            Article.outlet_id.isnot(None),
        )
        .group_by(Article.outlet_id)
        .order_by(desc("cnt"))
        .all()
    )

    ordered_ids = []
    seen = set()
    for outlet_id, _cnt in edition_outlet_ids:
        if outlet_id in rated_ids or outlet_id in seen:
            continue
        seen.add(outlet_id)
        ordered_ids.append(outlet_id)

    if len(ordered_ids) < limit:
        recent_article_outlets = (
            db.session.query(Article.outlet_id, func.count(Article.id).label("cnt"))
            .filter(
                Article.fetched_at >= cutoff,
                Article.outlet_id.isnot(None),
            )
            .group_by(Article.outlet_id)
            .order_by(desc("cnt"))
            .all()
        )
        for outlet_id, _cnt in recent_article_outlets:
            if outlet_id in rated_ids or outlet_id in seen:
                continue
            seen.add(outlet_id)
            ordered_ids.append(outlet_id)
            if len(ordered_ids) >= limit:
                break

    if not ordered_ids:
        return []

    outlets = Outlet.query.filter(Outlet.id.in_(ordered_ids[:limit])).all()
    by_id = {o.id: o for o in outlets}
    return [by_id[oid] for oid in ordered_ids[:limit] if oid in by_id]


def outlets_by_preference(user, preference):
    rows = (
        UserOutletPreference.query.filter_by(user_id=user.id, preference=preference)
        .order_by(UserOutletPreference.updated_at.desc())
        .all()
    )
    outlet_ids = [r.outlet_id for r in rows]
    if not outlet_ids:
        return []
    outlets = Outlet.query.filter(Outlet.id.in_(outlet_ids)).all()
    by_id = {o.id: o for o in outlets}
    return [by_id[oid] for oid in outlet_ids if oid in by_id]


def count_unrated_in_outlets(outlets, prefs=None):
    if prefs is None:
        prefs = current_prefs_map()
    if not current_user.is_authenticated:
        return 0
    return sum(1 for o in outlets if o and o.id not in prefs)

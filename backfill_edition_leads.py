#!/usr/bin/env python3
"""Backfill EditionStory.lead_article_id (and optional image credit) for existing editions.

Uses the live lead-selection / accessibility rules so historical edition cards
get a snapshotted accessible lead without republishing.
"""
from aggregator import create_app, db
from aggregator.models import EditionStory
from aggregator.story_view import apply_aggregator_filter
from aggregator.article_signals import select_lead_article


def backfill_edition_leads():
    updated_leads = 0
    cleared_leads = 0
    images_set = 0
    scanned = 0

    for es in EditionStory.query.order_by(EditionStory.id).all():
        story = es.story
        if not story:
            continue
        scanned += 1

        apply_aggregator_filter(story, edition_story=es)
        lead = select_lead_article(story, edition_story=es)

        if lead is None:
            if es.lead_article_id is not None:
                es.lead_article_id = None
                cleared_leads += 1
            continue

        if es.lead_article_id != lead.id:
            es.lead_article_id = lead.id
            updated_leads += 1

        if lead.image_url and not es.source_image_url:
            es.source_image_url = lead.image_url
            if lead.outlet and not es.image_credit_text:
                es.image_credit_text = lead.outlet.name
            images_set += 1
        elif lead.outlet and not es.image_credit_text:
            es.image_credit_text = lead.outlet.name

    db.session.commit()
    return {
        "scanned": scanned,
        "leads_set_or_changed": updated_leads,
        "leads_cleared": cleared_leads,
        "images_filled": images_set,
    }


def main():
    app = create_app()
    with app.app_context():
        result = backfill_edition_leads()
        print(
            "[backfill_edition_leads] "
            f"scanned={result['scanned']} "
            f"leads_set_or_changed={result['leads_set_or_changed']} "
            f"leads_cleared={result['leads_cleared']} "
            f"images_filled={result['images_filled']}"
        )


if __name__ == "__main__":
    main()

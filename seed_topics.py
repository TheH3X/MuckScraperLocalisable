#!/usr/bin/env python3
"""
seed_topics.py
==============
Bootstraps Topic rows in the database from country_config.py.

Run once after the migration, or whenever you add a new country config.
Safe to re-run — it is fully idempotent.

What it does:
  1. Reads country_config topics + scheduled_fetches.
  2. For each canonical topic, upserts a Topic row with label, icon,
     description, display_order, fetch config, and prompt presets.
  3. Unifies any existing DB topics whose name matched an old scheduled_fetch
     label (e.g. "Business & Economy" → "Buss/Fin") by reassigning all
     story_topics and article_topics associations and deleting the old row.

Usage:
  docker compose exec app python seed_topics.py
  # or locally:
  python seed_topics.py
"""

import json
import sys

# ---------------------------------------------------------------------------
# Prompt presets keyed by canonical analysis type.
# Placeholders: {combined}, {persona}, {country_name}, {source_availability},
#               {prompt_structure}
# ---------------------------------------------------------------------------

PROMPT_PRESETS = {
    "politics": {
        "analysis_persona": "political analyst",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Explain what happened, why it matters, and the most important current development\n"
            "- No bullet points\n"
            "- No section labels\n"
            "- No markdown or prefatory text\n"
            "- Begin directly with the first sentence of the summary. "
            "Do not use phrases like \"Here is the summary:\".\n"
            "- Keep it sharp and readable for a front-page briefing\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are an experienced media analyst writing a detailed report on how different "
            "news outlets are covering the same political story.\n\n"
            "Below are articles from the current source set, grouped by available outlet bias.\n\n"
            "Source availability:\n{source_availability}\n\n"
            "{combined}\n\n"
            "Write a detailed analytical report using this EXACT format:\n\n"
            "The story: [Write 2-3 sentences explaining what happened factually]\n\n"
            "{prompt_structure}\n\n"
            "What's contested: [Describe where the different sides disagree most sharply, "
            "what facts or framings are in dispute]\n\n"
            "What's missing: [Identify what angles or perspectives seem absent from the coverage, "
            "what questions aren't being asked]\n\n"
            "What's next: [Write one sentence on what to watch for]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you. Do not include the brackets or the "
            "instruction text in your final response. Replace them with your actual analysis.\n"
            "- Be specific about framing differences, not just topic differences\n"
            "- Do not infer, invent, or speculate about how a missing source bucket would cover the story\n"
            "- Stay neutral and analytical in your own voice\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
    "international": {
        "analysis_persona": "international affairs analyst",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same international story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Explain what happened, why it matters globally, and the most important development\n"
            "- No bullet points, no section labels, no markdown\n"
            "- Begin directly with the first sentence\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are an international affairs analyst writing a detailed report on a global news story.\n\n"
            "Below are articles covering the same story:\n\n"
            "{combined}\n\n"
            "Write a detailed analytical report using this EXACT format:\n\n"
            "The story: [Write 2-3 sentences explaining what happened factually]\n\n"
            "Why it matters globally: [Explain the international significance — which countries, "
            "alliances, or global systems are affected and how]\n\n"
            "Key players: [Identify the governments, organisations, or figures involved and their positions]\n\n"
            "Regional context: [Explain the broader regional or geopolitical background that shapes this event]\n\n"
            "Different perspectives: [Describe how different countries or media ecosystems are framing this story]\n\n"
            "What's next: [Write one sentence on what to watch for]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you — replace them with your analysis\n"
            "- Stay neutral and factual\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
    "science": {
        "analysis_persona": "science journalist",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same scientific story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Explain the discovery or development, why it matters, and the most important detail\n"
            "- No bullet points, no section labels, no markdown\n"
            "- Begin directly with the first sentence\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are a science journalist writing a detailed report on a scientific development.\n\n"
            "Below are articles covering the same story:\n\n"
            "{combined}\n\n"
            "Write a detailed analytical report using this EXACT format:\n\n"
            "The discovery or development: [Write 2-3 sentences explaining what happened or was discovered factually]\n\n"
            "Why it matters: [Explain the scientific significance — what does this change or enable?]\n\n"
            "What the research shows: [Detail key findings, data points, or technical details from the coverage]\n\n"
            "Real world impact: [Explain how this affects people, industries, or society in practical terms]\n\n"
            "What experts are saying: [Include notable quotes or expert opinions from the coverage. "
            "If none available, say \"Expert commentary not available in current coverage.\"]\n\n"
            "What's still unknown: [Note open questions, limitations of the research, or what needs further study]\n\n"
            "What's next: [Write one sentence on upcoming developments or what to watch for]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you — replace them with your analysis\n"
            "- Focus on accuracy and significance over drama\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
    "technology": {
        "analysis_persona": "technology journalist",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same technology story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Explain the development, why it matters, and the most important detail\n"
            "- No bullet points, no section labels, no markdown\n"
            "- Begin directly with the first sentence\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are a technology journalist writing a detailed report on a tech industry development.\n\n"
            "Below are articles covering the same story:\n\n"
            "{combined}\n\n"
            "Write a detailed analytical report using this EXACT format:\n\n"
            "The development: [Write 2-3 sentences explaining what happened factually]\n\n"
            "Why it matters: [Explain the technological significance — what does this change or enable?]\n\n"
            "Key details: [Detail key features, data points, or technical details from the coverage]\n\n"
            "Real world impact: [Explain how this affects users, the market, or society in practical terms]\n\n"
            "What experts are saying: [Include notable quotes or expert opinions from the coverage. "
            "If none available, say \"Expert commentary not available in current coverage.\"]\n\n"
            "What's next: [Write one sentence on upcoming developments or what to watch for]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you — replace them with your analysis\n"
            "- Focus on accuracy and significance over drama\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
    "medicine": {
        "analysis_persona": "medical and health journalist",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same health or medical story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Explain the medical discovery or health news, why it matters, and the most important detail\n"
            "- No bullet points, no section labels, no markdown\n"
            "- Begin directly with the first sentence\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are a medical journalist writing a detailed report on a health or medical development.\n\n"
            "Below are articles covering the same story:\n\n"
            "{combined}\n\n"
            "Write a detailed analytical report using this EXACT format:\n\n"
            "The discovery or development: [Write 2-3 sentences explaining the medical news factually]\n\n"
            "Clinical significance: [Explain the significance — what does this mean for treatment or public health?]\n\n"
            "What the research shows: [Detail key findings, trial results, or data points from the coverage]\n\n"
            "Patient impact: [Explain how this affects patients, public health, or medical practice]\n\n"
            "What experts are saying: [Include notable quotes or expert opinions from the coverage. "
            "If none available, say \"Expert commentary not available in current coverage.\"]\n\n"
            "What's still unknown: [Note open questions, limitations of the research, or what needs further study]\n\n"
            "What's next: [Write one sentence on upcoming developments or what to watch for]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you — replace them with your analysis\n"
            "- Focus on accuracy and avoid medical sensationalism\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
    "gaming": {
        "analysis_persona": "gaming and esports journalist",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same gaming or esports story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Explain the announcement or event, why it matters, and the most important detail\n"
            "- No bullet points, no section labels, no markdown\n"
            "- Begin directly with the first sentence\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are a gaming journalist writing a detailed report on a gaming industry or esports development.\n\n"
            "Below are articles covering the same story:\n\n"
            "{combined}\n\n"
            "Write a detailed analytical report using this EXACT format:\n\n"
            "The story: [Write 2-3 sentences explaining the announcement, release, or event factually]\n\n"
            "Why it matters: [Explain the significance for the gaming industry or player community]\n\n"
            "Key details: [Detail key features, tournament results, or data points from the coverage]\n\n"
            "Player and community impact: [Explain how the gaming community has reacted or will be affected]\n\n"
            "What reviewers or pros are saying: [Include notable quotes or opinions from the coverage. "
            "If none available, say \"Commentary not available in current coverage.\"]\n\n"
            "What's next: [Write one sentence on upcoming releases, patches, or developments to watch]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you — replace them with your analysis\n"
            "- Focus on industry impact and factual reporting\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
    "sports": {
        "analysis_persona": "sports journalist",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same sports story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Cover the key result, standout performance, and most important context\n"
            "- No bullet points, no section labels, no markdown\n"
            "- Begin directly with the first sentence\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are a sports journalist writing a factual recap and analysis of a sports story.\n\n"
            "Below are articles covering the same story:\n\n"
            "{combined}\n\n"
            "Write a detailed report using this EXACT format:\n\n"
            "What happened: [Write 2-3 sentences with the key facts — scores, results, or news]\n\n"
            "Key performances: [Describe standout players, teams, or moments from the coverage. "
            "If not a game recap, describe the key people involved.]\n\n"
            "The bigger picture: [Explain what this means for standings, playoffs, championships, contracts, "
            "or the sport more broadly]\n\n"
            "By the numbers: [Include key stats, records, or figures mentioned in the coverage. "
            "If none available, say \"Detailed statistics not available in current coverage.\"]\n\n"
            "What's next: [Write one sentence on upcoming games, decisions, or developments to watch]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you — replace them with your analysis\n"
            "- Focus on facts and context over opinion\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
    "business": {
        "analysis_persona": "financial journalist",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same business or financial story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Explain what happened, market/economic significance, and the most important detail\n"
            "- No bullet points, no section labels, no markdown\n"
            "- Begin directly with the first sentence\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are a financial journalist writing a detailed report on a business or markets story.\n\n"
            "Below are articles covering the same story:\n\n"
            "{combined}\n\n"
            "Write a detailed analytical report using this EXACT format:\n\n"
            "The story: [Write 2-3 sentences explaining what happened factually]\n\n"
            "Market impact: [Describe how markets, stocks, or prices have reacted based on the coverage]\n\n"
            "What companies or sectors are affected: [Identify key players, industries, or markets "
            "involved and how they are impacted]\n\n"
            "What analysts are saying: [Include expert or analyst opinions from the coverage. "
            "If none available, say \"Analyst commentary not available in current coverage.\"]\n\n"
            "The broader economic picture: [Explain how this fits into wider economic trends, policy, or conditions]\n\n"
            "Risks and opportunities: [Identify key risks or opportunities this creates for investors, "
            "businesses, or consumers]\n\n"
            "What's next: [Write one sentence on key dates, decisions, or developments to watch]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you — replace them with your analysis\n"
            "- Focus on market and economic significance\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
    "default": {
        "analysis_persona": "professional news analyst",
        "summary_prompt": (
            "You are a {persona} writing an executive summary for a news briefing.\n\n"
            "Below are multiple news articles covering the same story. "
            "Write a concise executive summary.\n\n"
            "Rules:\n"
            "- Write exactly one short paragraph\n"
            "- Use 3 to 5 sentences\n"
            "- Explain what happened, why it matters, and the most important current development\n"
            "- No bullet points, no section labels, no markdown\n"
            "- Begin directly with the first sentence\n\n"
            "Articles:\n{combined}\n\nExecutive Summary:"
        ),
        "deep_report_prompt": (
            "You are an experienced journalist writing a detailed report on a news story.\n\n"
            "Below are articles covering the same story:\n\n"
            "{combined}\n\n"
            "Write a detailed analytical report using this EXACT format:\n\n"
            "The story: [Write 2-3 sentences explaining what happened factually]\n\n"
            "Why it matters: [Explain the significance of this story — who it affects and how]\n\n"
            "Key details: [List the most important facts, figures, or developments from the coverage]\n\n"
            "Different perspectives: [Describe how different outlets or sources are framing this story. "
            "If coverage is uniform, say what angle is being emphasized.]\n\n"
            "What's missing: [Identify what angles or questions seem absent from the coverage]\n\n"
            "What's next: [Write one sentence on what to watch for]\n\n"
            "Rules:\n"
            "- Use EXACTLY the labels shown above including the colon\n"
            "- The brackets [ ] are instructions for you — replace them with your analysis\n"
            "- Stay neutral and analytical\n"
            "- No markdown, no extra formatting\n"
            "- Do not add any text before or after the structure above"
        ),
    },
}

# ---------------------------------------------------------------------------
# Mapping from canonical topic name → prompt preset key.
# Extend this as you add new topic types.
# ---------------------------------------------------------------------------

def _guess_preset_key(name: str, label: str) -> str:
    combined = (name + " " + (label or "")).lower()
    if any(k in combined for k in ("politics", "parliament", "government", "election")):
        return "politics"
    if any(k in combined for k in ("international", "world", "global")):
        return "international"
    if any(k in combined for k in ("medicine", "health", "medical")):
        return "medicine"
    if any(k in combined for k in ("tech", "ai", "artificial intelligence", "software", "gadget")):
        return "technology"
    if any(k in combined for k in ("gaming", "game", "esport")):
        return "gaming"
    if any(k in combined for k in ("sci", "science", "research")):
        return "science"
    if any(k in combined for k in ("sport", "sports")):
        return "sports"
    if any(k in combined for k in ("buss", "business", "fin", "finance", "economy", "economics", "market")):
        return "business"
    return "default"


CLASSIFIER_HINTS = {
    "AI": (
        "Articles about artificial intelligence, machine learning, LLMs, generative AI, "
        "and AI policy/regulation — not general gadget/tech news unless AI is the main subject."
    ),
}


def run():
    from aggregator.app import app
    from aggregator import db
    from aggregator.models import Topic
    from aggregator.country_config import get_config, get_scheduled_fetches, get_topics

    with app.app_context():
        print("=== seed_topics.py ===")
        cfg = get_config()
        bias_modes = cfg.get("bias_modes", {})
        default_bias = cfg.get("default_bias_mode", "none")

        display_topics = get_topics()
        scheduled_fetches = get_scheduled_fetches()
        if not display_topics:
            print("  [error] No topics defined in country_config — aborting")
            return

        # Build a lookup: label → scheduled fetch dict
        fetch_by_label = {f["label"]: f for f in scheduled_fetches}

        # Build a set of known canonical names (from display_topics)
        canonical_names = {t["label"] for t in display_topics}

        print(f"  country={cfg.get('country_code')} topics={len(display_topics)} fetches={len(scheduled_fetches)}")

        # --- Step 1: Upsert canonical topics from country_config display list ---
        for order, topic_def in enumerate(display_topics):
            canonical_name = topic_def["label"]
            icon = topic_def.get("icon", "")
            label = canonical_name  # label = name for now; admin can customise

            # Find matching scheduled fetch (same label) or None
            fetch = fetch_by_label.get(canonical_name)

            # Determine description from scheduled fetch mode
            if fetch:
                if fetch.get("mode") == "top" and fetch.get("category"):
                    description = f"Top headlines — category: {fetch['category']}"
                elif fetch.get("query"):
                    description = f"Query fetch: {fetch['query'][:80]}"
                else:
                    description = ""
            else:
                description = ""

            preset_key = _guess_preset_key(canonical_name, label)
            preset = PROMPT_PRESETS[preset_key]

            existing = Topic.query.filter_by(name=canonical_name).first()
            if existing:
                print(f"  [update] {canonical_name}")
                existing.label = label
                existing.icon = icon
                existing.description = description
                existing.display_order = order
                existing.is_active = True
                if fetch:
                    existing.fetch_mode = fetch.get("mode")
                    existing.fetch_country = fetch.get("country")
                    existing.fetch_category = fetch.get("category")
                    existing.fetch_query = fetch.get("query")
                    existing.gnews_query = fetch.get("gnews_query")
                    existing.gnews_category = fetch.get("gnews_category")
                existing.bias_mode = bias_modes.get(canonical_name, default_bias)
                hint = CLASSIFIER_HINTS.get(canonical_name)
                if hint and not existing.classifier_hint:
                    existing.classifier_hint = hint
                # Only set prompts if not already customised
                if not existing.analysis_persona:
                    existing.analysis_persona = preset["analysis_persona"]
                if not existing.summary_prompt:
                    existing.summary_prompt = preset["summary_prompt"]
                if not existing.deep_report_prompt:
                    existing.deep_report_prompt = preset["deep_report_prompt"]
            else:
                print(f"  [create] {canonical_name}")
                new_topic = Topic(
                    name=canonical_name,
                    label=label,
                    icon=icon,
                    description=description,
                    display_order=order,
                    is_active=True,
                    fetch_mode=fetch.get("mode") if fetch else None,
                    fetch_country=fetch.get("country") if fetch else None,
                    fetch_category=fetch.get("category") if fetch else None,
                    fetch_query=fetch.get("query") if fetch else None,
                    gnews_query=fetch.get("gnews_query") if fetch else None,
                    gnews_category=fetch.get("gnews_category") if fetch else None,
                    bias_mode=bias_modes.get(canonical_name, default_bias),
                    classifier_hint=CLASSIFIER_HINTS.get(canonical_name),
                    analysis_persona=preset["analysis_persona"],
                    summary_prompt=preset["summary_prompt"],
                    deep_report_prompt=preset["deep_report_prompt"],
                )
                db.session.add(new_topic)

        db.session.flush()

        # --- Step 2a: Handle scheduled_fetch labels that differ from display topics ---
        # e.g. "Business & Economy" != "Buss/Fin"
        # If a DB row existed for the old label: reassign story/article associations,
        # then delete the stale row.
        # Either way, apply the fetch config to the canonical topic matched by preset.
        for fetch in scheduled_fetches:
            fetch_label = fetch["label"]
            if fetch_label in canonical_names:
                continue  # Already handled in Step 1

            # Find the canonical topic to merge/apply config to — match by preset key
            pk = _guess_preset_key(fetch_label, "")
            canonical = None
            for t in display_topics:
                if _guess_preset_key(t["label"], "") == pk:
                    canonical = Topic.query.filter_by(name=t["label"]).first()
                    break
            if not canonical:
                canonical = Topic.query.filter_by(name="Other").first()
            if not canonical:
                print(f"  [skip] Cannot find canonical for '{fetch_label}'")
                continue

            # Apply fetch config to canonical if it has none yet
            if not canonical.fetch_mode:
                print(f"  [fetch-config] Applying '{fetch_label}' fetch config → '{canonical.name}'")
                canonical.fetch_mode = fetch.get("mode")
                canonical.fetch_country = fetch.get("country")
                canonical.fetch_category = fetch.get("category")
                canonical.fetch_query = fetch.get("query")
                canonical.gnews_query = fetch.get("gnews_query")
                canonical.gnews_category = fetch.get("gnews_category")
                
            canonical.bias_mode = bias_modes.get(canonical.name, default_bias)

            stale = Topic.query.filter_by(name=fetch_label).first()
            if not stale:
                continue  # Never created by classifier, nothing to merge

            print(f"  [merge] '{fetch_label}' → '{canonical.name}'")

            # Reassign story associations
            db.session.execute(
                db.text(
                    "INSERT INTO story_topics (story_id, topic_id) "
                    "SELECT story_id, :cid FROM story_topics WHERE topic_id = :sid "
                    "ON CONFLICT DO NOTHING"
                ),
                {"cid": canonical.id, "sid": stale.id},
            )
            db.session.execute(
                db.text("DELETE FROM story_topics WHERE topic_id = :sid"),
                {"sid": stale.id},
            )

            # Reassign article associations
            db.session.execute(
                db.text(
                    "INSERT INTO article_topics (article_id, topic_id) "
                    "SELECT article_id, :cid FROM article_topics WHERE topic_id = :sid "
                    "ON CONFLICT DO NOTHING"
                ),
                {"cid": canonical.id, "sid": stale.id},
            )
            db.session.execute(
                db.text("DELETE FROM article_topics WHERE topic_id = :sid"),
                {"sid": stale.id},
            )

            db.session.delete(stale)

        # --- Step 2b: Any other stale topic rows not in canonical list ---
        # Topics created by the LLM that exactly match a scheduled_fetch label
        # have been handled above. Any remaining orphaned names just get
        # display_order set high and is_active set to False so they are retired.
        all_db_topics = Topic.query.all()
        canonical_and_fetch_names = canonical_names | {f["label"] for f in scheduled_fetches}
        next_order = len(display_topics)
        for t in all_db_topics:
            if t.name not in canonical_and_fetch_names:
                # Deprecate the old topic (e.g. "Sci/Tech")
                t.is_active = False
                t.display_order = next_order
                next_order += 1
                if not t.label:
                    t.label = t.name
                preset_key = _guess_preset_key(t.name, "")
                preset = PROMPT_PRESETS[preset_key]
                if not t.analysis_persona:
                    t.analysis_persona = preset["analysis_persona"]
                if not t.summary_prompt:
                    t.summary_prompt = preset["summary_prompt"]
                if not t.deep_report_prompt:
                    t.deep_report_prompt = preset["deep_report_prompt"]

        db.session.commit()
        print("Done.")
        remaining = Topic.query.order_by(Topic.display_order).all()
        print(f"\nTopics in DB ({len(remaining)} total):")
        for t in remaining:
            fetch_info = f"  fetch={t.fetch_mode}" if t.fetch_mode else ""
            print(f"  [{t.display_order}] {t.name!r:30s} label={t.display_label!r:30s}{fetch_info}")


if __name__ == "__main__":
    run()


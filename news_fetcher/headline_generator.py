# muckscraperHeadlinesGoogleNEW/news_fetcher/headline_generator.py
# news_fetcher/headline_generator.py
import logging
import os
from datetime import datetime

from news_fetcher.langfuse_client import langfuse
from langfuse.decorators import observe, langfuse_context
from news_fetcher.llm_client import generate

logger = logging.getLogger(__name__)

HEADLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "Wire service headline, max 15 words",
        }
    },
    "required": ["headline"],
}


@observe()
def generate_story_headline(story):
    """
    Generate a news wire style headline for a multi-article story.
    Returns a headline string or None if Ollama is unavailable.
    Only runs if the story has 2+ articles.
    """
    if os.environ.get("OLLAMA_HOST") == "":
        logger.warning("Ollama not configured, skipping headline generation.")
        return None

    if len(story.articles) < 2:
        logger.debug(f"Story '{story.title}' has only 1 article, skipping headline.")
        return None

    titles = "\n".join(
        f"- {article.title}" for article in story.articles[:10]
    )

    # Static prefix first so consecutive headline calls share KV cache.
    prompt = f"""You are a wire service editor writing a single headline.

Write ONE headline for this story in wire service style.

Rules:
- Who/what/where in one line
- Maximum 15 words
- Present tense, active voice
- No punctuation at the end
- Do not include source names or outlet names

Articles covering the same story:
{titles}"""

    langfuse_context.update_current_observation(
        input=prompt
    )
    try:
        import json
        headline_response = generate(prompt, task="headline", schema=HEADLINE_SCHEMA)
        if not headline_response:
            return None
            
        langfuse_context.update_current_observation(
            output=headline_response
        )

        try:
            parsed = json.loads(headline_response)
            headline = parsed.get("headline") or ""
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse headline JSON: '{headline_response}'")
            return None

        # Clean up common LLM artifacts
        headline = headline.strip('"\'').strip()
        if headline and len(headline.split()) <= 20:
            logger.info(f"Generated headline: '{headline}'")
            return headline

        logger.warning(f"Headline too long or empty: '{headline}'")
        return None

    except Exception as e:
        logger.error(f"Error generating headline for '{story.title}': {e}")
        return None


def generate_missing_headlines(story_ids=None, recent_hours=24):
    """
    Find multi-article stories without headlines and generate them.
    When story_ids is provided, only those stories are considered.
    """
    from datetime import timedelta

    from aggregator import db
    from aggregator.models import Story
    from news_fetcher.llm_client import check_ollama_status

    if not check_ollama_status():
        logger.info("Ollama offline, skipping headline generation.")
        return

    query = Story.query
    if story_ids:
        story_ids = [story_id for story_id in story_ids if story_id]
        if not story_ids:
            logger.info("No touched stories supplied for headline generation.")
            return
        query = query.filter(Story.id.in_(story_ids))
    else:
        cutoff = datetime.utcnow() - timedelta(hours=recent_hours)
        query = query.filter(Story.created_at >= cutoff)

    stories = query.all()
    missing = [s for s in stories if len(s.articles) >= 2 and not s.headline]

    if not missing:
        logger.info("All multi-article stories have headlines.")
        return

    logger.info(f"Generating headlines for {len(missing)} stories...")
    count = 0
    for story in missing:
        headline = generate_story_headline(story)
        if headline:
            story.headline = headline
            count += 1

    db.session.commit()
    logger.info(f"Generated {count} headlines.")

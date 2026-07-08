# muckscraperHeadlinesGoogleNEW/news_fetcher/topic_classifier.py
# news_fetcher/topic_classifier.py
import os
import logging
from langfuse import Langfuse
from langfuse.decorators import observe, langfuse_context
from news_fetcher.llm_client import generate

logger = logging.getLogger(__name__)

langfuse = Langfuse(
    public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
    secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
    host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
)

from aggregator.country_config import get_config

_cfg = get_config()


def get_valid_topics():
    """
    Return the list of active topic names from the DB.
    Falls back to country_config if no DB topics are available.
    Must be called inside an active app context.
    """
    try:
        from aggregator.models import Topic
        db_topics = Topic.query.filter_by(is_active=True).order_by(Topic.display_order).all()
        if db_topics:
            return [t.name for t in db_topics]
    except Exception as e:
        logger.warning("[Classifier] Could not load topics from DB: %s — falling back to config", e)
    # Fallback to country config
    return [t["label"] for t in _cfg.get("topics", [])] or ["Other"]


def get_topic_hints():
    """
    Return a dict of {topic_name: classifier_hint} for topics that have a hint set.
    Must be called inside an active app context.
    """
    hints = {}
    try:
        from aggregator.models import Topic
        for t in Topic.query.filter(
            Topic.is_active == True,
            Topic.classifier_hint.isnot(None),
        ).all():
            if t.classifier_hint:
                hints[t.name] = t.classifier_hint.strip()
    except Exception:
        pass
    return hints


@observe()
def classify_article(title, content_snippet=""):
    """
    Ask Ollama to classify an article into one or more topics.
    Returns a list of topic label strings.
    Falls back to ["Other"] if Ollama is unavailable or classification fails.
    """
    if os.environ.get("OLLAMA_HOST") == "":
        return ["Other"]

    # Use title + first 200 chars of content for classification
    text = title
    if content_snippet:
        clean = content_snippet[:200].strip()
        if clean:
            text += f"\n{clean}"

    country_name = _cfg.get("country_name", "the given country")
    valid_topics = get_valid_topics()
    topic_hints = get_topic_hints()

    # Build the topics list
    topic_lines = []
    for t in valid_topics:
        if t == "Other":
            continue
        hint = topic_hints.get(t)
        if hint:
            topic_lines.append(f"- {t}: {hint}")
        else:
            topic_lines.append(f"- {t}")
    topics_list = "\n".join(topic_lines)

    prompt = f"""Classify this article into categories. Respond with ONLY a JSON object.

Article: "{text}"

Categories:
{topics_list}
- Other

Rules:
- Use EXACT category names only
- Maximum 2 categories
- If none apply, use "Other"

{{"categories": ["Category1"]}}"""

    langfuse_context.update_current_observation(
        input=prompt
    )
    try:
        result = generate(prompt, task="classification", json_mode=True)
        if not result:
            return ["Other"]
            
        langfuse_context.update_current_observation(
            output=result
        )

        import json
        lines = []
        try:
            data = json.loads(result)
            if "categories" in data and isinstance(data["categories"], list):
                lines = [str(x).strip() for x in data["categories"]]
            else:
                lines = [result]
        except json.JSONDecodeError:
            lines = [line.strip() for line in result.splitlines() if line.strip()]

        matched = []
        for line in lines:
            for valid in valid_topics:
                if valid.lower() in line.lower():
                    if valid not in matched:
                        matched.append(valid)

        if matched:
            # Remove "Other" if any real categories were found
            matched = [t for t in matched if t != "Other"]
            if matched:
                logger.info(f"  [Classifier] Tagged as: {', '.join(matched)}")
                return matched

        logger.info(f"  [Classifier] No match found, using Other")
        return ["Other"]

    except Exception as e:
        logger.info(f"  [Classifier] Error: {e}, using Other")
        return ["Other"]

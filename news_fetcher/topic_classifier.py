# muckscraperHeadlinesGoogleNEW/news_fetcher/topic_classifier.py
# news_fetcher/topic_classifier.py
import os
import re
import logging
from news_fetcher.langfuse_client import langfuse
from langfuse.decorators import observe, langfuse_context
from news_fetcher.llm_client import generate

logger = logging.getLogger(__name__)
from aggregator.country_config import get_config, get_topics

_cfg = get_config()

_CLASSIFICATION_BATCH_CACHE = {}
_TRUSTED_FETCH_TOPIC_SKIP = frozenset({"Custom", "Global News"})


def begin_classification_batch():
    """Cache topic metadata for classify_article calls in one ingest batch."""
    valid_topics = get_valid_topics()
    topic_hints = get_topic_hints()
    topic_lines = []
    for t in valid_topics:
        if t == "Other":
            continue
        hint = topic_hints.get(t)
        if hint:
            topic_lines.append(f"- {t}: {hint}")
        else:
            topic_lines.append(f"- {t}")
    _CLASSIFICATION_BATCH_CACHE["valid_topics"] = valid_topics
    _CLASSIFICATION_BATCH_CACHE["topics_list"] = "\n".join(topic_lines)


def clear_classification_batch():
    _CLASSIFICATION_BATCH_CACHE.clear()


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
    # Fallback to country config (e.g. SA Politics / SA News before seed)
    return [t["label"] for t in get_topics()] or ["Other"]


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
    cache = _CLASSIFICATION_BATCH_CACHE
    if cache.get("topics_list") and cache.get("valid_topics"):
        valid_topics = cache["valid_topics"]
        topics_list = cache["topics_list"]
    else:
        valid_topics = get_valid_topics()
        topic_hints = get_topic_hints()
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

    # Static prefix first so consecutive classify calls share KV cache.
    prompt = f"""Classify this article into categories.

Categories:
{topics_list}
- Other

Rules:
- Use EXACT category names only from the list above.
- Maximum 2 categories.
- If no categories apply, use "Other".

Article:
"{text}"
"""

    category_enum = [t for t in valid_topics if t != "Other"] + ["Other"]
    schema = {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "items": {"type": "string", "enum": category_enum},
                "maxItems": 2,
            }
        },
        "required": ["categories"],
    }

    langfuse_context.update_current_observation(
        input=prompt
    )
    try:
        result = generate(prompt, task="classification", schema=schema)
        if not result:
            return ["Other"]

        langfuse_context.update_current_observation(
            output=result
        )

        import json

        try:
            data = json.loads(result)
            categories = data.get("categories", [])
            if not isinstance(categories, list):
                categories = []
        except json.JSONDecodeError:
            categories = []

        matched = []
        for cat in categories:
            cat = str(cat).strip()
            if cat in valid_topics and cat not in matched:
                matched.append(cat)

        if matched:
            matched = [t for t in matched if t != "Other"]
            if matched:
                logger.info(f"  [Classifier] Tagged as: {', '.join(matched)}")
                return matched

        logger.info(f"  [Classifier] No match found, using Other")
        return ["Other"]

    except Exception as e:
        logger.info(f"  [Classifier] Error: {e}, using Other")
        return ["Other"]

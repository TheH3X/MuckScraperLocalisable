# muckscraperHeadlinesGoogleNEW/news_fetcher/outlet_bias_llm.py
# news_fetcher/outlet_bias_llm.py

import requests
import json
import os
import logging
from langfuse import Langfuse
from langfuse.decorators import observe, langfuse_context

logger = logging.getLogger(__name__)

langfuse = Langfuse(
    public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
    secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
    host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "")
MODEL = os.environ.get("OLLAMA_MODEL", "")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", 600))

from aggregator.country_config import get_config
_cfg = get_config()

BIAS_LABELS = _cfg["bias_labels"]
BIAS_DESCRIPTIONS = _cfg["bias_descriptions"]


@observe()
def _ask_ollama(prompt):
    """Send a prompt to Ollama and return the raw response string or None."""
    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"model": MODEL}
    )
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "num_predict": 100,
                    "num_ctx": 2048,
                }
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json().get("response", "").strip()
        langfuse_context.update_current_observation(
            output=result
        )
        return result
    except Exception as e:
        logger.info(f"  Error calling Ollama: {e}")
        return None


def _parse_bias_score(raw, label):
    """Parse a 1-5 integer from Ollama's response."""
    if raw is None:
        return None
    import json
    try:
        data = json.loads(raw)
        rating = data.get("rating")
        if str(rating).lower() == "unknown":
            logger.info(f"  Ollama could not determine bias for: {label}")
            return None
        score = int(rating)
        if 1 <= score <= 5:
            return score
        return None
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.info(f"  Could not parse Ollama response for '{label}': {raw}")
        return None


@observe()
def get_outlet_bias_from_llm(outlet_name):
    """
    Ask Ollama to rate the political bias of a news outlet by name.
    Returns an integer 1-5 or None if it can't determine.
    """
    country_name = _cfg.get("country_name", "the given country")
    scale_text = "\n".join([f"{k} = {v} ({BIAS_DESCRIPTIONS[k]})" for k, v in BIAS_LABELS.items()])

    prompt = f"""You are a media bias analyst for {country_name}. Rate the political bias of the news outlet "{outlet_name}" on this scale:
{scale_text}

Rules:
- If you have never heard of the outlet or genuinely cannot determine its bias, the rating should be "unknown"

Respond ONLY with a JSON object in this EXACT format:
{{"rating": 3}}
or
{{"rating": "unknown"}}

Outlet: {outlet_name}"""

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"model": MODEL}
    )
    raw = _ask_ollama(prompt)
    langfuse_context.update_current_observation(
        output=raw
    )
    logger.info(f"  Ollama rated outlet '{outlet_name}': {raw}")
    return _parse_bias_score(raw, outlet_name)


@observe()
def get_article_bias_from_llm(title, content=None):
    """
    Ask Ollama to rate the political bias of a specific article
    based on its title and content snippet.
    Returns an integer 1-5 or None if it can't determine.
    """
    article_text = f"Title: {title}"
    if content:
        snippet = content[:600].strip()
        article_text += f"\n\nContent snippet: {snippet}"

    country_name = _cfg.get("country_name", "the given country")
    scale_text = "\n".join([f"{k} = {v} ({BIAS_DESCRIPTIONS[k]})" for k, v in BIAS_LABELS.items()])

    prompt = f"""You are a media bias analyst for {country_name}. Read the following news article and rate its political bias on this scale:
{scale_text}

Consider the language used, framing, and perspective presented in the article itself.

Rules:
- If you genuinely cannot determine the bias from the content, the rating should be "unknown"

Respond ONLY with a JSON object in this EXACT format:
{{"rating": 3}}
or
{{"rating": "unknown"}}

Article:
{article_text}"""

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"model": MODEL}
    )
    raw = _ask_ollama(prompt)
    langfuse_context.update_current_observation(
        output=raw
    )
    logger.info(f"  Ollama rated article '{title[:60]}...': {raw}")
    return _parse_bias_score(raw, title)

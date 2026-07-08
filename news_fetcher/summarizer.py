# muckscraperHeadlinesGoogleNEW/news_fetcher/summarizer.py
# news_fetcher/summarizer.py

import requests
import os
import re
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

if not MODEL:
    logging.warning("OLLAMA_MODEL environment variable is not set. All summarization will fail.")

from aggregator.country_config import get_config
_cfg = get_config()


def check_ollama_status():
    """Returns True if Ollama is reachable, False otherwise."""
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


def strip_html(text):
    """Strip HTML tags and clean up whitespace for LLM input."""
    if not text:
        return ""
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common HTML entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&nbsp;', ' ').replace('&quot;', '"').replace('&#39;', "'")
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


STORY_FILTER_STOPWORDS = {
    "about", "after", "again", "against", "amid", "among", "and", "are",
    "around", "before", "being", "but", "can", "could", "did", "does",
    "during", "for", "from", "has", "have", "her", "his", "how", "into",
    "its", "may", "more", "new", "news", "not", "over", "says", "she",
    "that", "the", "their", "this", "through", "with", "what", "when",
    "where", "who", "why", "will", "you", "your",
}


def _story_filter_tokens(text):
    tokens = re.findall(r"[a-z0-9][a-z0-9'-]{2,}", (text or "").lower())
    return {
        token.strip("-'")
        for token in tokens
        if token.strip("-'") and token.strip("-'") not in STORY_FILTER_STOPWORDS
    }


def _article_filter_text(article):
    return article.title or ""


def _jaccard(left, right):
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _select_story_prompt_articles(story, limit=10):
    """
    Return articles to use for story-level LLM prompts.

    This is intentionally conservative: it only removes clear outliers from
    multi-source clusters and does not alter persisted story membership.
    """
    articles = list(story.articles[:limit])
    if len(articles) < 3:
        return articles, []

    token_sets = [_story_filter_tokens(_article_filter_text(article)) for article in articles]
    story_tokens = _story_filter_tokens(" ".join([story.headline or "", story.title or ""]))

    # Pick the article that best represents the cluster based on title/content
    # overlap with the story label and neighboring articles.
    anchor_index = 0
    best_score = -1.0
    for idx, tokens in enumerate(token_sets):
        peer_scores = [
            _jaccard(tokens, other)
            for other_idx, other in enumerate(token_sets)
            if other_idx != idx
        ]
        score = (sum(peer_scores) / len(peer_scores)) if peer_scores else 0.0
        if story_tokens:
            score += _jaccard(tokens, story_tokens)
        if score > best_score:
            anchor_index = idx
            best_score = score

    anchor_tokens = token_sets[anchor_index]
    selected = []
    excluded = []
    for article, tokens in zip(articles, token_sets):
        anchor_similarity = _jaccard(tokens, anchor_tokens)
        story_similarity = _jaccard(tokens, story_tokens)
        shared_anchor_terms = len(tokens & anchor_tokens)
        shared_story_terms = len(tokens & story_tokens)
        include = (
            article is articles[anchor_index] or
            anchor_similarity >= 0.08 or
            story_similarity >= 0.08 or
            shared_anchor_terms >= 3 or
            shared_story_terms >= 2
        )
        if include:
            selected.append(article)
        else:
            excluded.append(article)

    # Avoid starving the prompt on small or unusually diverse stories.
    if len(selected) < max(2, len(articles) // 2):
        return articles, []

    if excluded:
        logger.info(
            "  [StoryFilter] Excluding %s likely outlier article(s) from story %s prompt: %s",
            len(excluded),
            getattr(story, "id", "unknown"),
            "; ".join((article.title or "")[:80] for article in excluded),
        )
    return selected, excluded


def get_topics_list(obj):
    """Get the topic names for a Story or Article as a list of strings."""
    try:
        return [t.name for t in obj.topics]
    except Exception:
        return []


def _analysis_text(obj):
    parts = [
        getattr(obj, "headline", None) or "",
        getattr(obj, "title", None) or "",
    ]
    for article in list(getattr(obj, "articles", []) or [])[:8]:
        parts.append(article.title or "")
    return " ".join(parts).lower()


def _contains_any(text, keywords):
    return any(keyword in text for keyword in keywords)


POLITICAL_ANALYSIS_KEYWORDS = {
    "administration", "agency", "bill", "campaign", "court",
    "diplomat", "election", "executive order", "federal",
    "government", "law", "lawsuit", "minister", "policy", "president", 
    "prime minister", "ruling", "sanction", "tariff",
} | _cfg.get("political_keywords", set())

PUBLIC_SAFETY_ANALYSIS_KEYWORDS = {
    "accident", "arrested", "attack", "blaze", "crash", "dead", "death",
    "disaster", "earthquake", "evacuation", "explosion", "fire", "flood",
    "hostage", "injured", "killed", "missing", "police", "rescue", "search",
    "shooting", "storm", "victim",
}

BUSINESS_ANALYSIS_KEYWORDS = {
    "bank", "bankruptcy", "bond", "ceo", "company", "earnings", "economy",
    "finance", "inflation", "investor", "layoff",
    "market", "merger", "mortgage", "price", "profit", "rate", "revenue",
    "stock", "trade",
} | _cfg.get("business_keywords", set())


def detect_analysis_type(obj):
    """
    Determine which type of specialized persona to use based on topics.
    Checks DB topic analysis_keywords_json first, then falls back to
    hardcoded keyword sets.
    Returns one of: 'politics', 'science', 'sports', 'business', 'default'
    """
    topics = get_topics_list(obj)
    topics_lower = [t.lower() for t in topics]
    text = _analysis_text(obj)

    # Check DB topics for analysis_persona or analysis_keywords_json overrides
    if topics:
        try:
            import json as _json
            from aggregator.models import Topic as _Topic
            for topic_name in topics:
                db_topic = _Topic.query.filter_by(name=topic_name).first()
                if not db_topic:
                    continue
                if db_topic.analysis_keywords_json:
                    try:
                        kws = _json.loads(db_topic.analysis_keywords_json)
                        if isinstance(kws, list) and any(k.lower() in text for k in kws):
                            if db_topic.analysis_persona:
                                # Map persona string to analysis type
                                p = db_topic.analysis_persona.lower()
                                if "political" in p:
                                    return "politics"
                                if "science" in p or "technology" in p:
                                    return "science"
                                if "sports" in p:
                                    return "sports"
                                if "financial" in p or "business" in p:
                                    return "business"
                    except Exception:
                        pass
        except Exception:
            pass

    # Fallback to general categorization if DB lookup failed
    if _contains_any(text, PUBLIC_SAFETY_ANALYSIS_KEYWORDS):
        return 'default'
    if any("politic" in t or "government" in t for t in topics_lower) or _contains_any(text, POLITICAL_ANALYSIS_KEYWORDS):
        return 'politics'
    if any(t in ["sci/tech", "science", "technology", "ai", "medicine", "health"] for t in topics_lower):
        return 'science'
    if any(t == 'sports' for t in topics_lower):
        return 'sports'
    if (
        any(t in ['buss/fin', 'business'] for t in topics_lower)
        or _contains_any(text, BUSINESS_ANALYSIS_KEYWORDS)
    ):
        return 'business'
    return 'default'


def get_persona(analysis_type, obj=None):
    """Return the specialized journalist persona for a given analysis type.
    If obj has topics with a DB-configured analysis_persona, use that instead.
    """
    if obj is not None:
        try:
            from aggregator.models import Topic as _Topic
            for topic_name in get_topics_list(obj):
                db_topic = _Topic.query.filter_by(name=topic_name).first()
                if db_topic and db_topic.analysis_persona:
                    return db_topic.analysis_persona
        except Exception:
            pass
    mapping = {
        'politics': 'political analyst',
        'science': 'science and technology journalist',
        'sports': 'sports journalist',
        'business': 'financial journalist',
        'default': 'professional news analyst'
    }
    return mapping.get(analysis_type, mapping['default'])


def article_needs_deep_analysis(article):
    """Only generate article-level deep analysis for domains where it adds value."""
    return detect_analysis_type(article) in {"politics", "science", "business"}


@observe()
def summarize_story(story):
    """
    Given a Story object with related articles, ask Ollama to generate
    a detailed summary of the story using a specialized journalist persona.
    Returns summary string or None if Ollama is unavailable.
    """
    if not story.articles:
        return None

    if not check_ollama_status():
        return None

    analysis_type = detect_analysis_type(story)
    persona = get_persona(analysis_type, obj=story)

    prompt_articles, excluded_articles = _select_story_prompt_articles(story, limit=10)
    readable_articles = [
        article for article in prompt_articles
        if len(strip_html(article.content or "").strip()) >= 200
    ]
    if not readable_articles:
        logger.info(
            "  Skipping story summary for '%s': no readable article content.",
            story.title[:80],
        )
        langfuse_context.update_current_observation(
            metadata={
                "model": MODEL,
                "analysis_type": analysis_type,
                "persona": persona,
                "prompt_articles": len(prompt_articles),
                "excluded_prompt_articles": len(excluded_articles),
                "skipped_reason": "no_readable_article_content",
            }
        )
        return None

    article_texts = []
    for i, article in enumerate(prompt_articles, 1):
        text = f"{i}. Title: {article.title}"
        if article.content:
            # Strip HTML before sending to Ollama
            clean_content = strip_html(article.content)
            # Use more content now that we have full scraped articles
            snippet = clean_content[:1500].strip()
            text += f"\n   Content: {snippet}"
        article_texts.append(text)

    combined = "\n\n".join(article_texts)

    # Check if any of the story's topics has a DB-stored summary prompt override
    db_summary_prompt = None
    try:
        from aggregator.models import Topic as _Topic
        for topic_name in get_topics_list(story):
            db_topic = _Topic.query.filter_by(name=topic_name).first()
            if db_topic and db_topic.summary_prompt:
                db_summary_prompt = db_topic.summary_prompt
                break
    except Exception:
        pass

    if db_summary_prompt:
        try:
            prompt = db_summary_prompt.format(combined=combined, persona=persona)
        except (KeyError, ValueError):
            prompt = db_summary_prompt  # Use as-is if placeholders are wrong
    else:
        prompt = f"""You are a {persona} writing an executive summary for a news briefing.

Below are multiple news articles covering the same story. Write a concise executive summary.

Rules:
- Write exactly one short paragraph
- Use 3 to 5 sentences
- Explain what happened, why it matters, and the most important current development
- No bullet points
- No section labels
- No markdown or prefatory text
- Begin directly with the first sentence of the summary. Do not use phrases like "Here is the summary:".
- Keep it sharp and readable for a front-page briefing

Articles:
{combined}

Executive Summary:"""

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={
            "model": MODEL,
            "analysis_type": analysis_type,
            "persona": persona,
            "prompt_articles": len(prompt_articles),
            "excluded_prompt_articles": len(excluded_articles),
        }
    )
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": 400,
                    "num_ctx": 4096,
                }
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()

        result = response.json()
        summary = result.get("response", "").strip()

        langfuse_context.update_current_observation(
            output=summary
        )

        if summary:
            logger.info(f"  Generated {analysis_type} summary for story: {story.title[:60]}...")
            return summary
        return None

    except Exception as e:
        logger.info(f"  Error generating summary for '{story.title}': {e}")
        return None


@observe()
def generate_deep_report(story):
    """
    Generate an in-depth analytical report for a multi-source story.
    Uses topic-aware prompts based on the story's classification.
    Returns report string or None if Ollama is unavailable.
    """
    if not story.articles:
        return None

    if not check_ollama_status():
        return None

    analysis_type = detect_analysis_type(story)

    # Group articles by exact bias category
    bias_groups = {1: [], 2: [], 3: [], 4: [], 5: [], "unrated": []}

    prompt_articles, excluded_articles = _select_story_prompt_articles(story, limit=15)
    readable_articles = [
        article for article in prompt_articles
        if len(strip_html(article.content or "").strip()) >= 200
    ]
    if not readable_articles:
        logger.info(
            "  Skipping deep report for '%s': no readable article content.",
            story.title[:80],
        )
        langfuse_context.update_current_observation(
            metadata={
                "model": MODEL,
                "analysis_type": analysis_type,
                "prompt_articles": len(prompt_articles),
                "excluded_prompt_articles": len(excluded_articles),
                "skipped_reason": "no_readable_article_content",
            }
        )
        return None

    for article in prompt_articles:
        score = article.bias_score
        if score is None and article.outlet:
            score = article.outlet.bias_score
            
        if score is None:
            bias_groups["unrated"].append(article)
        else:
            bucket = int(round(score))
            if bucket < 1: bucket = 1
            if bucket > 5: bucket = 5
            bias_groups[bucket].append(article)

    def format_articles(articles, label, include_empty=False):
        if not articles:
            return f"\n{label} Sources:\n- None found in the current source set." if include_empty else ""
        lines = [f"\n{label} Sources:"]
        for a in articles:
            outlet_name = a.outlet.name if a.outlet else (a.source or "Unknown source")
            lines.append(f"- {outlet_name}: {a.title}")
            if a.content:
                snippet = strip_html(a.content)[:300].strip()
                if snippet:
                    lines.append(f"  Excerpt: {snippet}")
        return "\n".join(lines)

    def format_all_articles(articles):
        """Format all articles without bias grouping for non-political analysis."""
        lines = []
        for a in articles:
            outlet_name = a.outlet.name if a.outlet else (a.source or "Unknown source")
            lines.append(f"- {outlet_name}: {a.title}")
            if a.content:
                snippet = strip_html(a.content)[:300].strip()
                if snippet:
                    lines.append(f"  Excerpt: {snippet}")
        return "\n".join(lines)

    # Build formatting variables based on analysis type
    combined = ""
    source_availability = ""
    prompt_structure = ""
    
    if analysis_type == 'politics':
        availability_lines = []
        prompt_structure_lines = []
        
        for i in range(1, 6):
            label = _cfg["bias_labels"][i]
            section = format_articles(bias_groups[i], label.upper(), include_empty=True)
            combined += section
            availability_lines.append(f"- {label} sources found: {len(bias_groups[i])}")
            
            prompt_structure_lines.append(
                f"How {label} is covering it: [Only describe coverage if sources are listed. If none, write exactly: \"No {label} sources were found in the current coverage.\"]"
            )
            
        unrated_section = format_articles(bias_groups["unrated"], "UNRATED", include_empty=True)
        combined += unrated_section
        availability_lines.append(f"- Unrated sources found: {len(bias_groups['unrated'])}")

        if not combined.strip():
            return None

        source_availability = "\n".join(availability_lines)
        prompt_structure = "\n\n".join(prompt_structure_lines)
    else:
        combined = format_all_articles(prompt_articles)
        if not combined.strip():
            return None

    # Retrieve DB prompt override if available
    db_deep_prompt = None
    try:
        from aggregator.models import Topic as _Topic
        for topic_name in get_topics_list(story):
            db_topic = _Topic.query.filter_by(name=topic_name).first()
            if db_topic and db_topic.deep_report_prompt:
                db_deep_prompt = db_topic.deep_report_prompt
                break
    except Exception:
        pass

    if db_deep_prompt:
        try:
            prompt = db_deep_prompt.format(
                combined=combined,
                source_availability=source_availability,
                prompt_structure=prompt_structure
            )
        except (KeyError, ValueError):
            prompt = db_deep_prompt
    else:
        # Generic fallback
        prompt = f"""You are an experienced journalist writing a detailed report on a news story.

Below are articles covering the same story:

{combined}

Write a detailed analytical report using this EXACT format:

The story: [Write 2-3 sentences explaining what happened factually]

Why it matters: [Explain the significance of this story — who it affects and how]

Key details: [List the most important facts, figures, or developments from the coverage]

Different perspectives: [Describe how different outlets or sources are framing this story. If coverage is uniform, say what angle is being emphasized.]

What's missing: [Identify what angles or questions seem absent from the coverage]

What's next: [Write one sentence on what to watch for]

Rules:
- Use EXACTLY the labels shown above including the colon
- The brackets [ ] are instructions for you. Do not include the brackets or the instruction text in your final response. Replace them with your actual analysis.
- Stay neutral and analytical
- Compare only the outlets and perspectives actually present in the article list
- Do not use left/right political framing unless the story is explicitly about politics, government, law, elections, or policy
- No markdown, no extra formatting
- Do not add any text before or after the structure above"""

    # Check if any of the story's topics has a DB-stored deep report prompt override
    db_deep_report_prompt = None
    try:
        from aggregator.models import Topic as _Topic
        for topic_name in get_topics_list(story):
            db_topic = _Topic.query.filter_by(name=topic_name).first()
            if db_topic and db_topic.deep_report_prompt:
                db_deep_report_prompt = db_topic.deep_report_prompt
                break
    except Exception:
        pass

    if db_deep_report_prompt:
        try:
            prompt = db_deep_report_prompt.format(
                combined=combined,
                source_availability=source_availability if analysis_type == 'politics' else '',
                prompt_structure=prompt_structure if analysis_type == 'politics' else ''
            )
        except (KeyError, ValueError):
            prompt = db_deep_report_prompt  # Use as-is if placeholders are wrong

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={
            "model": MODEL,
            "analysis_type": analysis_type,
            "prompt_articles": len(prompt_articles),
            "excluded_prompt_articles": len(excluded_articles),
        }
    )

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": 400,
                    "num_ctx": 4096,
                }
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        report = result.get("response", "").strip()
        langfuse_context.update_current_observation(output=report)
        if report:
            logger.info(f"  Generated {analysis_type} deep report for: {story.title[:60]}...")
            return report
        return None
    except Exception as e:
        logger.error(f"  Error generating deep report for '{story.title}': {e}")
        return None


@observe()
def summarize_article(article):
    """
    Generate a concise Smart Brevity briefing for a single article using a
    specialized journalist persona.
    Used for the per-article summary button in the article reader.
    Returns summary string or None if Ollama is unavailable.
    """
    if not article or not article.content:
        return None

    if not check_ollama_status():
        return None

    analysis_type = detect_analysis_type(article)
    persona = get_persona(analysis_type)

    clean_content = strip_html(article.content)[:3000].strip()
    if not clean_content:
        return None

    prompt = f"""You are a {persona} writing a tight Smart Brevity-style article briefing.

Below is a news article. Write a concise briefing using EXACTLY this format:

The big picture: [Write one direct sentence on what happened.]

Why it matters: [Write 1-2 short sentences on why this story matters.]

Quick analysis: [Write 1-2 short sentences on the framing, tension, consequence, uncertainty, or what stands out most.]

What's next: [Write one sentence on what to watch for next.]

Rules:
- Use EXACTLY the labels shown above including the colon
- The brackets [ ] are instructions for you. Do not include the brackets or the instruction text in your final response. Replace them with your actual analysis.
- No bullets
- Keep the full response to 4 short sections only
- Be concrete, not generic
- Do not repeat the same idea in multiple sections
- No markdown, no extra formatting, no commentary
- Do not add any text before or after the structure above

Article title: {article.title}

Article content:
{clean_content}

Summary:"""

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"model": MODEL, "analysis_type": analysis_type, "persona": persona}
    )

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": 400,
                    "num_ctx": 4096,
                }
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        summary = result.get("response", "").strip()
        langfuse_context.update_current_observation(output=summary)
        if summary:
            logger.info(f"  Generated {analysis_type} summary for article: {article.title[:60]}...")
            return summary
        return None
    except Exception as e:
        logger.error(f"  Error generating summary for article '{article.title}': {e}")
        return None


@observe()
def generate_article_deep_analysis(article):
    """
    Generate a deeper article-level analysis for topics that benefit from it.
    Returns analysis string or None if this topic should only receive a summary.
    """
    if not article or not article.content or not article_needs_deep_analysis(article):
        return None

    if not check_ollama_status():
        return None

    analysis_type = detect_analysis_type(article)
    clean_content = strip_html(article.content)[:3500].strip()
    if not clean_content:
        return None

    if analysis_type == "politics":
        prompt = f"""You are a political analyst writing a focused article analysis.

Analyze this political article using EXACTLY this format:

Core argument: [Write 2-3 sentences summarizing the article's main thesis and factual basis]

How it frames the issue: [Describe what assumptions, emphasis, or political framing the piece uses]

What evidence it relies on: [Identify the main facts, sources, or claims used to support the argument]

What to question or watch: [Note potential blind spots, unresolved questions, or what future reporting should clarify]

Rules:
- Use EXACTLY the labels shown above including the colon
- The brackets [ ] are instructions for you. Do not include the brackets or the instruction text in your final response. Replace them with your actual analysis.
- Stay analytical, not partisan
- No markdown, no extra formatting
- Do not add any text before or after the structure above

Article title: {article.title}

Article content:
{clean_content}

Analysis:"""
    elif analysis_type == "science":
        prompt = f"""You are a science and technology journalist writing a technical analysis.

Analyze this article using EXACTLY this format:

What the article says: [Write 2-3 sentences summarizing the core finding or development]

Technical substance: [Describe the key mechanism, data, or technical concept explained in the article]

Why this matters: [Explain what the development changes in practical or scientific terms]

What remains uncertain: [Note limitations, caveats, unanswered questions, or hype risk]

Rules:
- Use EXACTLY the labels shown above including the colon
- The brackets [ ] are instructions for you. Do not include the brackets or the instruction text in your final response. Replace them with your actual analysis.
- Prioritize clarity and technical accuracy
- No markdown, no extra formatting
- Do not add any text before or after the structure above

Article title: {article.title}

Article content:
{clean_content}

Analysis:"""
    elif analysis_type == "business":
        prompt = f"""You are a financial journalist writing a markets and business analysis.

Analyze this article using EXACTLY this format:

What happened: [Write 2-3 sentences summarizing the business or market event]

What is driving it: [Explain the main financial, operational, or policy factors behind it]

Who is affected: [Identify the companies, sectors, investors, or consumers most affected]

What to watch next: [Note risks, catalysts, or decision points that matter going forward]

Rules:
- Use EXACTLY the labels shown above including the colon
- The brackets [ ] are instructions for you. Do not include the brackets or the instruction text in your final response. Replace them with your actual analysis.
- Focus on economic significance, not fluff
- No markdown, no extra formatting
- Do not add any text before or after the structure above

Article title: {article.title}

Article content:
{clean_content}

Analysis:"""
    else:
        return None

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"model": MODEL, "analysis_type": analysis_type, "scope": "article_deep_analysis"}
    )

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": 400,
                    "num_ctx": 4096,
                }
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        analysis = result.get("response", "").strip()
        langfuse_context.update_current_observation(output=analysis)
        if analysis:
            logger.info(f"  Generated {analysis_type} article analysis: {article.title[:60]}...")
            return analysis
        return None
    except Exception as e:
        logger.error(f"  Error generating deep analysis for article '{article.title}': {e}")
        return None

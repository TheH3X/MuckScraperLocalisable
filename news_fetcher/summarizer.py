# muckscraperHeadlinesGoogleNEW/news_fetcher/summarizer.py
# news_fetcher/summarizer.py
import os
import re
import logging
from news_fetcher.langfuse_client import langfuse
from langfuse.decorators import observe, langfuse_context
from news_fetcher.llm_client import generate, check_ollama_status, truncate_to_token_budget

logger = logging.getLogger(__name__)
from aggregator.country_config import get_config
_cfg = get_config()

STORY_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
    },
    "required": ["executive_summary"],
}

DEEP_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "the_story": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "key_details": {"type": "string"},
        "different_perspectives": {"type": "string"},
        "whats_missing": {"type": "string"},
        "whats_next": {"type": "string"},
    },
    "required": [
        "the_story",
        "why_it_matters",
        "key_details",
        "different_perspectives",
        "whats_missing",
        "whats_next",
    ],
}

ARTICLE_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "the_big_picture": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "quick_analysis": {"type": "string"},
        "whats_next": {"type": "string"},
    },
    "required": ["the_big_picture", "why_it_matters", "quick_analysis", "whats_next"],
}

STORY_SUMMARY_MAX_ARTICLES = 5
STORY_SUMMARY_CHARS_PER_ARTICLE = 700
ARTICLE_SUMMARY_MAX_CHARS = 1500
DEEP_REPORT_INPUT_TOKEN_BUDGET = 3000


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


_SUMMARY_JSON_SUFFIX = (
    '\n\nReturn ONLY a JSON object with a single key "executive_summary" '
    "containing the summary paragraph as a string. "
    "Do not echo the articles, rules, or task back in the JSON."
)


def _build_story_articles_block(articles, chars_per_article=STORY_SUMMARY_CHARS_PER_ARTICLE):
    """Format articles for story-level prompts with per-article char cap."""
    article_texts = []
    for i, article in enumerate(articles, 1):
        text = f"{i}. Title: {article.title}"
        if article.content:
            clean_content = strip_html(article.content)
            snippet = clean_content[:chars_per_article].strip()
            text += f"\n   Content: {snippet}"
        article_texts.append(text)
    return "\n\n".join(article_texts)


def _finalize_story_summary_prompt(prompt):
    """Normalize DB/default prompts so json_mode returns a single summary field."""
    prompt = prompt.rstrip()
    if prompt.endswith("Executive Summary:"):
        prompt = prompt[: -len("Executive Summary:")].rstrip()
    if "executive_summary" not in prompt.lower():
        prompt += _SUMMARY_JSON_SUFFIX
    return prompt


def _extract_story_summary_text(response):
    """Parse story summary output from JSON or plain-text fallback."""
    if not response:
        return None

    import json

    try:
        parsed = json.loads(response)
        if isinstance(parsed, str):
            return parsed.strip() or None
        if isinstance(parsed, dict):
            if any(key in parsed for key in ("articles", "rules", "task")):
                return None
            for key in ("executive_summary", "summary", "text", "content"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in parsed.values():
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except json.JSONDecodeError:
        pass

    match = re.search(
        r'"executive_summary"\s*:\s*"((?:\\.|[^"\\])*)"',
        response,
        re.DOTALL,
    )
    if match:
        try:
            return json.loads(f'"{match.group(1)}"').strip() or None
        except json.JSONDecodeError:
            text = match.group(1).strip()
            return text or None

    cleaned = response.strip()
    if cleaned:
        cleaned = re.sub(r"(?i)^executive summary:\s*", "", cleaned, count=1).strip()
        return cleaned or None
    return None


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

    Hard-excludes paywalled/inaccessible articles first, then removes clear
    outliers from multi-source clusters. Does not alter persisted membership.
    """
    from aggregator.article_signals import (
        accessibility_failure_reason,
    )

    all_articles = list(story.articles)
    inaccessible_excluded = []
    accessible_pool = []
    for article in all_articles:
        # for_lead=False: still allow non-roundup content that clears wall checks
        reason = accessibility_failure_reason(article, for_lead=False)
        if reason is not None:
            inaccessible_excluded.append(article)
        else:
            accessible_pool.append(article)

    if inaccessible_excluded:
        logger.info(
            "  [StoryFilter] Excluding %s inaccessible article(s) from story %s prompt: %s",
            len(inaccessible_excluded),
            getattr(story, "id", "unknown"),
            "; ".join(
                f"{(article.title or '')[:60]} ({accessibility_failure_reason(article, for_lead=False)})"
                for article in inaccessible_excluded[:5]
            ),
        )

    articles = accessible_pool[:limit]
    if not articles:
        return [], inaccessible_excluded

    if len(articles) < 3:
        return articles, inaccessible_excluded

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
    outlier_excluded = []
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
            outlier_excluded.append(article)

    # Avoid starving the prompt on small or unusually diverse stories.
    if len(selected) < max(2, len(articles) // 2):
        return articles, inaccessible_excluded

    if outlier_excluded:
        logger.info(
            "  [StoryFilter] Excluding %s likely outlier article(s) from story %s prompt: %s",
            len(outlier_excluded),
            getattr(story, "id", "unknown"),
            "; ".join((article.title or "")[:80] for article in outlier_excluded),
        )
    return selected, inaccessible_excluded + outlier_excluded


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

    prompt_articles, excluded_articles = _select_story_prompt_articles(
        story, limit=STORY_SUMMARY_MAX_ARTICLES
    )
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
                "analysis_type": analysis_type,
                "persona": persona,
                "prompt_articles": len(prompt_articles),
                "excluded_prompt_articles": len(excluded_articles),
                "skipped_reason": "no_readable_article_content",
            }
        )
        return None

    combined = _build_story_articles_block(readable_articles[:STORY_SUMMARY_MAX_ARTICLES])

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
        # Static prefix first so consecutive summary calls share KV cache.
        prompt = f"""You are a {persona} writing an executive summary for a news briefing.

Write a concise executive summary for the story below.

Rules:
- Write exactly one short paragraph
- Use 3 to 5 sentences
- Explain what happened, why it matters, and the most important current development
- Keep it sharp and readable for a front-page briefing

Articles:
{combined}"""

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={
            "analysis_type": analysis_type,
            "persona": persona,
            "prompt_articles": len(prompt_articles),
            "excluded_prompt_articles": len(excluded_articles),
        }
    )
    try:
        summary_response = generate(prompt, task="summary", schema=STORY_SUMMARY_SCHEMA)

        langfuse_context.update_current_observation(
            output=summary_response
        )

        summary = _extract_story_summary_text(summary_response)
        if not summary and summary_response:
            # Fallback for legacy plain-text custom DB prompts (see generate_deep_report)
            summary = summary_response.strip()
            summary = re.sub(r"(?i)^executive summary:\s*", "", summary, count=1).strip() or None
        if not summary:
            logger.warning(
                "  Failed to parse summary JSON: '%s'",
                (summary_response or "")[:500],
            )
            return None

        logger.info(f"  Generated {analysis_type} summary for story: {story.title[:60]}...")
        return summary

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

    # Resolve bias_mode for story
    bias_mode = "none"
    try:
        if story.topics:
            modes = [t.bias_mode for t in story.topics if t.bias_mode]
            if "political" in modes:
                bias_mode = "political"
            elif "editorial" in modes:
                bias_mode = "editorial"
    except Exception:
        pass

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
                "analysis_type": analysis_type,
                "prompt_articles": len(prompt_articles),
                "excluded_prompt_articles": len(excluded_articles),
                "skipped_reason": "no_readable_article_content",
            }
        )
        return None

    for article in prompt_articles:
        score = article.bias_score
        
        # Note: we deliberately DO NOT fall back to article.outlet.bias_score here.
        # If article.bias_score is None, it means bias was intentionally suppressed 
        # (e.g., non-political topic) or it's unrated. Using the outlet score would
        # falsely inject bias categorization into non-political reports.
            
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
    
    if bias_mode == 'political':
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

    combined = truncate_to_token_budget(combined, DEEP_REPORT_INPUT_TOKEN_BUDGET)

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
        # Static prefix first so consecutive report calls share KV cache.
        prompt = f"""You are an experienced journalist writing a detailed report on a news story.

Write a detailed analytical report covering:
- the_story: 2-3 sentences explaining what happened factually
- why_it_matters: significance — who it affects and how
- key_details: most important facts, figures, or developments
- different_perspectives: how outlets frame the story (or what angle is emphasized if uniform)
- whats_missing: angles or questions absent from the coverage
- whats_next: one sentence on what to watch for

Rules:
- Stay neutral and analytical
- Compare only the outlets and perspectives actually present in the article list
- Do not use left/right political framing unless the story is explicitly about politics, government, law, elections, or policy

Articles:
{combined}"""

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={
            "analysis_type": analysis_type,
            "prompt_articles": len(prompt_articles),
            "excluded_prompt_articles": len(excluded_articles),
        }
    )

    try:
        import json
        report_response = generate(prompt, task="report", schema=DEEP_REPORT_SCHEMA)
        langfuse_context.update_current_observation(output=report_response)
        
        if not report_response:
            return None

        try:
            parsed = json.loads(report_response)
            
            # Reconstruct the expected text format
            sections = []
            if "the_story" in parsed:
                sections.append(f"The story: {parsed['the_story']}")
            if "why_it_matters" in parsed:
                sections.append(f"Why it matters: {parsed['why_it_matters']}")
            if "key_details" in parsed:
                sections.append(f"Key details: {parsed['key_details']}")
            if "different_perspectives" in parsed:
                sections.append(f"Different perspectives: {parsed['different_perspectives']}")
            if "whats_missing" in parsed:
                sections.append(f"What's missing: {parsed['whats_missing']}")
            if "whats_next" in parsed:
                sections.append(f"What's next: {parsed['whats_next']}")
            
            report = "\n\n".join(sections)
            if not report.strip():
                report = report_response # fallback
        except json.JSONDecodeError:
            report = report_response # Fallback for old custom DB prompts
            
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

    clean_content = strip_html(article.content)[:ARTICLE_SUMMARY_MAX_CHARS].strip()
    if not clean_content:
        return None

    # Static prefix first so consecutive article-summary calls share KV cache.
    prompt = f"""You are a {persona} writing a tight Smart Brevity-style article briefing.

Write a concise briefing with four sections:
- the_big_picture: one direct sentence on what happened
- why_it_matters: 1-2 short sentences on why this story matters
- quick_analysis: 1-2 short sentences on framing, tension, consequence, or what stands out
- whats_next: one sentence on what to watch for next

Rules:
- Keep the full response to 4 short sections only
- Be concrete, not generic
- Do not repeat the same idea in multiple sections

Article title: {article.title}

Article content:
{clean_content}"""

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"analysis_type": analysis_type, "persona": persona}
    )

    try:
        import json
        summary_response = generate(prompt, task="summary", schema=ARTICLE_SUMMARY_SCHEMA)
        langfuse_context.update_current_observation(output=summary_response)
        
        if not summary_response:
            return None
            
        try:
            parsed = json.loads(summary_response)
            sections = []
            if "the_big_picture" in parsed:
                sections.append(f"The big picture: {parsed['the_big_picture']}")
            if "why_it_matters" in parsed:
                sections.append(f"Why it matters: {parsed['why_it_matters']}")
            if "quick_analysis" in parsed:
                sections.append(f"Quick analysis: {parsed['quick_analysis']}")
            if "whats_next" in parsed:
                sections.append(f"What's next: {parsed['whats_next']}")
            
            summary = "\n\n".join(sections)
            if not summary.strip():
                summary = summary_response
        except json.JSONDecodeError:
            summary = summary_response
            
        if summary:
            logger.info(f"  Generated {analysis_type} summary for article: {article.title[:60]}...")
            return summary
        return None
    except Exception as e:
        logger.error(f"  Error generating summary for article '{article.title}': {e}")
        return None


ARTICLE_DEEP_POLITICS_SCHEMA = {
    "type": "object",
    "properties": {
        "core_argument": {"type": "string"},
        "how_it_frames_the_issue": {"type": "string"},
        "what_evidence_it_relies_on": {"type": "string"},
        "what_to_question_or_watch": {"type": "string"},
    },
    "required": [
        "core_argument",
        "how_it_frames_the_issue",
        "what_evidence_it_relies_on",
        "what_to_question_or_watch",
    ],
}

ARTICLE_DEEP_SCIENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "what_the_article_says": {"type": "string"},
        "technical_substance": {"type": "string"},
        "why_this_matters": {"type": "string"},
        "what_remains_uncertain": {"type": "string"},
    },
    "required": [
        "what_the_article_says",
        "technical_substance",
        "why_this_matters",
        "what_remains_uncertain",
    ],
}

ARTICLE_DEEP_BUSINESS_SCHEMA = {
    "type": "object",
    "properties": {
        "what_happened": {"type": "string"},
        "what_is_driving_it": {"type": "string"},
        "who_is_affected": {"type": "string"},
        "what_to_watch_next": {"type": "string"},
    },
    "required": [
        "what_happened",
        "what_is_driving_it",
        "who_is_affected",
        "what_to_watch_next",
    ],
}


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
    clean_content = strip_html(article.content)[:ARTICLE_SUMMARY_MAX_CHARS].strip()
    if not clean_content:
        return None

    analysis_schema = None
    if analysis_type == "politics":
        analysis_schema = ARTICLE_DEEP_POLITICS_SCHEMA
        prompt = f"""You are a political analyst writing a focused article analysis.

Analyze this political article covering:
- core_argument: main thesis and factual basis (2-3 sentences)
- how_it_frames_the_issue: assumptions, emphasis, or political framing
- what_evidence_it_relies_on: main facts, sources, or claims
- what_to_question_or_watch: blind spots, unresolved questions, or future reporting needs

Rules:
- Stay analytical, not partisan

Article title: {article.title}

Article content:
{clean_content}"""
    elif analysis_type == "science":
        analysis_schema = ARTICLE_DEEP_SCIENCE_SCHEMA
        prompt = f"""You are a science and technology journalist writing a technical analysis.

Analyze this article covering:
- what_the_article_says: core finding or development (2-3 sentences)
- technical_substance: key mechanism, data, or technical concept
- why_this_matters: practical or scientific significance
- what_remains_uncertain: limitations, caveats, unanswered questions, or hype risk

Rules:
- Prioritize clarity and technical accuracy

Article title: {article.title}

Article content:
{clean_content}"""
    elif analysis_type == "business":
        analysis_schema = ARTICLE_DEEP_BUSINESS_SCHEMA
        prompt = f"""You are a financial journalist writing a markets and business analysis.

Analyze this article covering:
- what_happened: business or market event (2-3 sentences)
- what_is_driving_it: main financial, operational, or policy factors
- who_is_affected: companies, sectors, investors, or consumers most affected
- what_to_watch_next: risks, catalysts, or decision points going forward

Rules:
- Focus on economic significance, not fluff

Article title: {article.title}

Article content:
{clean_content}"""
    else:
        return None

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"analysis_type": analysis_type, "scope": "article_deep_analysis"}
    )

    try:
        import json
        analysis_response = generate(prompt, task="report", schema=analysis_schema)
        langfuse_context.update_current_observation(output=analysis_response)
        
        if not analysis_response:
            return None
            
        try:
            parsed = json.loads(analysis_response)
            sections = []
            
            if analysis_type == "politics":
                if "core_argument" in parsed: sections.append(f"Core argument: {parsed['core_argument']}")
                if "how_it_frames_the_issue" in parsed: sections.append(f"How it frames the issue: {parsed['how_it_frames_the_issue']}")
                if "what_evidence_it_relies_on" in parsed: sections.append(f"What evidence it relies on: {parsed['what_evidence_it_relies_on']}")
                if "what_to_question_or_watch" in parsed: sections.append(f"What to question or watch: {parsed['what_to_question_or_watch']}")
            elif analysis_type == "science":
                if "what_the_article_says" in parsed: sections.append(f"What the article says: {parsed['what_the_article_says']}")
                if "technical_substance" in parsed: sections.append(f"Technical substance: {parsed['technical_substance']}")
                if "why_this_matters" in parsed: sections.append(f"Why this matters: {parsed['why_this_matters']}")
                if "what_remains_uncertain" in parsed: sections.append(f"What remains uncertain: {parsed['what_remains_uncertain']}")
            elif analysis_type == "business":
                if "what_happened" in parsed: sections.append(f"What happened: {parsed['what_happened']}")
                if "what_is_driving_it" in parsed: sections.append(f"What is driving it: {parsed['what_is_driving_it']}")
                if "who_is_affected" in parsed: sections.append(f"Who is affected: {parsed['who_is_affected']}")
                if "what_to_watch_next" in parsed: sections.append(f"What to watch next: {parsed['what_to_watch_next']}")
            
            analysis = "\n\n".join(sections)
            if not analysis.strip():
                analysis = analysis_response
        except json.JSONDecodeError:
            analysis = analysis_response

        if analysis:
            logger.info(f"  Generated {analysis_type} article analysis: {article.title[:60]}...")
            return analysis
        return None
    except Exception as e:
        logger.error(f"  Error generating deep analysis for article '{article.title}': {e}")
        return None

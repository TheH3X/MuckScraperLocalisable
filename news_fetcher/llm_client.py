import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
CLASSIFIER_MODEL = os.environ.get("OLLAMA_CLASSIFIER_MODEL", DEFAULT_MODEL)
TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", 600))
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

# Unified num_ctx avoids model reloads when alternating task types.
UNIFIED_NUM_CTX = 4096

TASK_PRESETS = {
    "classification": {
        "temperature": 0,
        "num_predict": 100,
        "num_ctx": UNIFIED_NUM_CTX,
    },
    "headline": {
        "temperature": 0.3,
        "num_predict": 50,
        "num_ctx": UNIFIED_NUM_CTX,
    },
    "summary": {
        "temperature": 0.4,
        "num_predict": 350,
        "num_ctx": UNIFIED_NUM_CTX,
    },
    "report": {
        "temperature": 0.5,
        "num_predict": 1000,
        "num_ctx": UNIFIED_NUM_CTX,
    },
}

LOAD_DURATION_WARN_NS = 2_000_000_000  # 2s — likely a model reload


def estimate_tokens(text):
    """Rough token estimate for logging (chars / 4)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def truncate_to_token_budget(text, max_tokens):
    """Truncate text to an approximate token budget."""
    if not text or max_tokens <= 0:
        return ""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated.rstrip() + "..."


def _extract_response_text(data):
    text = (data.get("response") or "").strip()
    if not text:
        # Some reasoning models put JSON in thinking when format=json.
        text = (data.get("thinking") or "").strip()
    return text or None


def _log_metrics(task, data, prompt_tokens_est, attempt):
    prompt_eval_count = data.get("prompt_eval_count", 0)
    eval_count = data.get("eval_count", 0)
    prompt_eval_duration = data.get("prompt_eval_duration", 0)
    eval_duration = data.get("eval_duration", 0)
    load_duration = data.get("load_duration", 0)
    done_reason = data.get("done_reason", "unknown")
    total_duration = data.get("total_duration", 0)

    prompt_tps = (
        prompt_eval_count / (prompt_eval_duration / 1e9)
        if prompt_eval_duration
        else 0
    )
    gen_tps = eval_count / (eval_duration / 1e9) if eval_duration else 0

    logger.info(
        "[LLM] task=%s attempt=%s prompt_tokens=%s prompt_eval=%s gen=%s "
        "prompt_tps=%.1f gen_tps=%.1f load_ms=%.0f total_ms=%.0f done=%s",
        task,
        attempt,
        prompt_eval_count or prompt_tokens_est,
        prompt_eval_count,
        eval_count,
        prompt_tps,
        gen_tps,
        load_duration / 1e6,
        total_duration / 1e6,
        done_reason,
    )

    if done_reason == "length":
        logger.warning(
            "[LLM] Output truncated (done_reason=length) for task=%s — "
            "consider raising num_predict or reducing prompt size",
            task,
        )
    if load_duration > LOAD_DURATION_WARN_NS:
        logger.warning(
            "[LLM] Model reload detected (load_duration=%.1fs) for task=%s",
            load_duration / 1e9,
            task,
        )

    metrics = {
        "task": task,
        "attempt": attempt,
        "prompt_tokens_est": prompt_tokens_est,
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
        "prompt_eval_duration_ms": round(prompt_eval_duration / 1e6, 1),
        "eval_duration_ms": round(eval_duration / 1e6, 1),
        "load_duration_ms": round(load_duration / 1e6, 1),
        "total_duration_ms": round(total_duration / 1e6, 1),
        "done_reason": done_reason,
        "prompt_tps": round(prompt_tps, 1),
        "gen_tps": round(gen_tps, 1),
    }

    try:
        from langfuse.decorators import langfuse_context

        langfuse_context.update_current_observation(metadata=metrics)
    except Exception:
        pass

    return metrics


def _call_ollama(payload):
    response = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def generate(
    prompt,
    task="classification",
    json_mode=False,
    schema=None,
    model=None,
    retry_on_empty=True,
):
    """Central LLM call with task-appropriate settings.

    Args:
        prompt: Full prompt text (static prefix first for KV-cache reuse).
        task: One of classification, headline, summary, report.
        json_mode: Request JSON output (use schema= for constrained decoding).
        schema: Optional JSON schema dict for grammar-constrained output.
        model: Override model name.
        retry_on_empty: Retry once when response is empty.
    """
    options = TASK_PRESETS.get(task, TASK_PRESETS["classification"]).copy()
    payload = {
        "model": model or (
            CLASSIFIER_MODEL if task == "classification" else DEFAULT_MODEL
        ),
        "prompt": prompt,
        "stream": False,
        "options": options,
        # qwen3 defaults to thinking mode; on /api/generate that can consume
        # num_predict and leave response empty (especially with format=json).
        "think": False,
        "keep_alive": KEEP_ALIVE,
    }

    if schema is not None:
        payload["format"] = schema
    elif json_mode:
        payload["format"] = "json"

    prompt_tokens_est = estimate_tokens(prompt)
    max_attempts = 2 if retry_on_empty else 1

    for attempt in range(1, max_attempts + 1):
        try:
            t0 = time.monotonic()
            data = _call_ollama(payload)
            text = _extract_response_text(data)
            _log_metrics(task, data, prompt_tokens_est, attempt)

            if text:
                return text

            if attempt < max_attempts:
                logger.warning(
                    "[LLM] Empty response for task=%s, retrying (attempt %s)",
                    task,
                    attempt + 1,
                )
        except Exception as e:
            logger.error(
                "[LLM] Error communicating with Ollama (task=%s attempt=%s): %s",
                task,
                attempt,
                e,
            )
            if attempt >= max_attempts:
                return None

    return None


def check_ollama_status():
    """Returns True if Ollama is reachable, False otherwise."""
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


def get_loaded_models():
    """Return loaded model info from /api/ps, or None on failure."""
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/ps", timeout=5)
        response.raise_for_status()
        return response.json().get("models", [])
    except Exception:
        return None

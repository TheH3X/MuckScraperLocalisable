import os
import requests
import logging

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
CLASSIFIER_MODEL = os.environ.get("OLLAMA_CLASSIFIER_MODEL", DEFAULT_MODEL)
TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", 600))

TASK_PRESETS = {
    "classification": {
        "temperature": 0.1, 
        "num_predict": 100, 
        "num_ctx": 2048,
        "stop": ["\n\nNote:", "\n\nI ", "\n\nPlease", "\n\n---"]
    },
    "headline": {
        "temperature": 0.3, 
        "num_predict": 50,  
        "num_ctx": 2048,
        "stop": ["\n\nNote:", "\n\nI ", "\n\nPlease", "\n\n---"]
    },
    "summary": {
        "temperature": 0.4, 
        "num_predict": 250, 
        "num_ctx": 4096,
        "stop": ["\n\nNote:", "\n\nI ", "\n\nPlease", "\n\n---"]
    },
    "report": {
        "temperature": 0.5, 
        "num_predict": 800, 
        "num_ctx": 6144,
        "stop": ["\n\nNote:", "\n\nI ", "\n\nPlease", "\n\n---"]
    },
}

def generate(prompt, task="classification", json_mode=False, model=None):
    """Central LLM call with task-appropriate settings."""
    options = TASK_PRESETS.get(task, TASK_PRESETS["classification"]).copy()
    payload = {
        "model": model or (CLASSIFIER_MODEL if task == "classification" else DEFAULT_MODEL),
        "prompt": prompt,
        "stream": False,
        "options": options,
        # qwen3 defaults to thinking mode; on /api/generate that can consume
        # num_predict and leave response empty (especially with format=json).
        "think": False,
    }
    if json_mode:
        payload["format"] = "json"

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json=payload,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        text = (data.get("response") or "").strip()
        if not text:
            # Some reasoning models put JSON in thinking when format=json.
            text = (data.get("thinking") or "").strip()
        return text or None
    except Exception as e:
        logger.error(f"Error communicating with Ollama: {e}")
        return None

def check_ollama_status():
    """Returns True if Ollama is reachable, False otherwise."""
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception:
        return False

# news_fetcher/allsides_lookup.py
# AllSides Media Bias Ratings mapped to MuckScraper's 1-5 scale.
# 1=Left, 2=Lean Left, 3=Center, 4=Lean Right, 5=Right
# Source: allsides.com/media-bias/media-bias-ratings
# Last updated: April 2026

from aggregator.country_config import get_config

_cfg = get_config()
OUTLET_BIAS = _cfg["outlet_bias"]


def get_outlet_bias_score(outlet_name):
    """
    Look up an outlet's bias score by name.
    Returns float score (1-5) or None if not found.
    Tries exact match first, then case-insensitive match.
    """
    if not outlet_name:
        return None

    # Exact match
    if outlet_name in OUTLET_BIAS:
        return float(OUTLET_BIAS[outlet_name])

    # Case-insensitive match
    outlet_lower = outlet_name.lower().strip()
    for key, score in OUTLET_BIAS.items():
        if key.lower() == outlet_lower:
            return float(score)

    return None

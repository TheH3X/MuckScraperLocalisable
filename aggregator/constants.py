from aggregator.country_config import get_config

_cfg = get_config()
TOPICS = _cfg["topics"]
AGGREGATORS = _cfg["aggregators"]

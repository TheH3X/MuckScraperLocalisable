from aggregator.country_config import get_config

_cfg = get_config()
# TOPICS removed — topics are now managed in the database (Topic model).
# Use Topic.query.filter_by(is_active=True).order_by(Topic.display_order).all() instead.
AGGREGATORS = _cfg["aggregators"]

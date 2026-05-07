import logging
import sys

# Configure logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

from aggregator import create_app
from news_fetcher.fetch_and_store_articles import force_regroup_all

app = create_app()
with app.app_context():
    logging.info("Starting force_regroup_all()")
    force_regroup_all()
    logging.info("Finished force_regroup_all()")

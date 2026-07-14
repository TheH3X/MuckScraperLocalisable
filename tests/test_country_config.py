import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_country_config_module():
    """Load country_config without importing aggregator package (avoids Flask)."""
    path = ROOT / "aggregator" / "country_config.py"
    spec = importlib.util.spec_from_file_location("country_config_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SouthAfricaCountryConfigTests(unittest.TestCase):
    def setUp(self):
        os.environ["MUCKSCRAPER_COUNTRY"] = "za"
        self.cfg_mod = load_country_config_module()

    def test_default_country_is_south_africa(self):
        cfg = self.cfg_mod.get_config()
        self.assertEqual(self.cfg_mod.get_country_code(), "za")
        self.assertEqual(cfg["country_name"], "South Africa")
        self.assertEqual(cfg["timezone"], "Africa/Johannesburg")

    def test_localised_sa_topics_are_present(self):
        labels = [t["label"] for t in self.cfg_mod.get_topics()]
        self.assertIn("SA Politics", labels)
        self.assertIn("SA News", labels)
        self.assertIn("International News", labels)
        self.assertIn("Business", labels)
        self.assertEqual(labels[0], "SA Politics")
        self.assertEqual(labels[1], "SA News")

        fetches = self.cfg_mod.get_scheduled_fetches()
        fetch_labels = {f["label"] for f in fetches}
        self.assertIn("SA Politics", fetch_labels)
        self.assertIn("SA News", fetch_labels)

        sa_politics = next(f for f in fetches if f["label"] == "SA Politics")
        self.assertIn("Ramaphosa", sa_politics["query"])
        self.assertEqual(sa_politics["mode"], "query")

        sa_news = next(f for f in fetches if f["label"] == "SA News")
        self.assertEqual(sa_news["country"], "za")
        self.assertEqual(sa_news["mode"], "top")

    def test_classifier_fallback_includes_sa_topics(self):
        # Simulate the classifier fallback path without importing langfuse/Flask.
        topics = [t["label"] for t in self.cfg_mod.get_topics()] or ["Other"]
        self.assertIn("SA Politics", topics)
        self.assertIn("SA News", topics)

    def test_sa_topics_present_without_bias_modes(self):
        cfg = self.cfg_mod.get_config()
        labels = [t["label"] for t in self.cfg_mod.get_topics()]
        self.assertIn("SA Politics", labels)
        self.assertIn("SA News", labels)
        self.assertNotIn("bias_modes", cfg)
        self.assertNotIn("outlet_bias", cfg)
        self.assertNotIn("bias_labels", cfg)

    def test_seed_topics_reads_from_country_config(self):
        seed_source = (ROOT / "seed_topics.py").read_text(encoding="utf-8")
        self.assertIn("get_topics()", seed_source)
        self.assertIn("get_scheduled_fetches()", seed_source)
        self.assertNotIn('{"label": "SA Politics", "icon": "SP"}', seed_source)

    def test_publish_edition_uses_country_timezone(self):
        source = (ROOT / "news_fetcher" / "fetch_and_store_articles.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("get_timezone()", source)
        self.assertNotIn("America/New_York", source)
        self.assertNotIn("now_eastern", source)

    def test_scheduler_job_name_uses_configured_timezone(self):
        source = (ROOT / "news_fetcher" / "scheduler.py").read_text(encoding="utf-8")
        self.assertIn('name=f"Scheduled news fetch ({TIMEZONE})"', source)
        self.assertNotIn("Scheduled news fetch (America/New_York)", source)


if __name__ == "__main__":
    unittest.main()

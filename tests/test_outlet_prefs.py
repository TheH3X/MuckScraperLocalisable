"""Unit tests for per-user outlet preference helpers (no Flask app required)."""

import unittest
from types import SimpleNamespace


class FilterArticlesByPrefsTests(unittest.TestCase):
    def test_mute_removes_articles(self):
        from aggregator.outlet_prefs import filter_articles_by_prefs

        articles = [
            SimpleNamespace(outlet_id=1, title="a"),
            SimpleNamespace(outlet_id=2, title="b"),
            SimpleNamespace(outlet_id=3, title="c"),
        ]
        kept = filter_articles_by_prefs(articles, {2: "mute", 3: "prefer"})
        self.assertEqual([a.title for a in kept], ["a", "c"])

    def test_empty_prefs_keeps_all(self):
        from aggregator.outlet_prefs import filter_articles_by_prefs

        articles = [SimpleNamespace(outlet_id=1), SimpleNamespace(outlet_id=2)]
        self.assertEqual(filter_articles_by_prefs(articles, {}), articles)


class SortArticlesByPrefsTests(unittest.TestCase):
    def test_prefer_sorts_first(self):
        from aggregator.outlet_prefs import sort_articles_by_prefs

        articles = [
            SimpleNamespace(outlet_id=1, title="unrated"),
            SimpleNamespace(outlet_id=2, title="prefer"),
            SimpleNamespace(outlet_id=3, title="allow"),
        ]
        ordered = sort_articles_by_prefs(
            articles, {2: "prefer", 3: "allow"}
        )
        self.assertEqual([a.title for a in ordered], ["prefer", "allow", "unrated"])


class StatusForOutletTests(unittest.TestCase):
    def test_missing_is_unrated(self):
        from aggregator.outlet_prefs import status_for_outlet

        self.assertEqual(status_for_outlet(SimpleNamespace(id=9), {}), "unrated")
        self.assertEqual(status_for_outlet(9, {9: "mute"}), "mute")
        self.assertEqual(status_for_outlet(None, {}), "unrated")


class CountryConfigNoBiasTests(unittest.TestCase):
    def test_enrichment_feeds_removed(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "aggregator" / "country_config.py"
        spec = importlib.util.spec_from_file_location("cfg_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg = mod.get_config()
        self.assertNotIn("left_enrichment_feeds", cfg)
        self.assertNotIn("right_enrichment_feeds", cfg)
        self.assertIn("rss_feeds", cfg)


class PublishEditionNoBiasTests(unittest.TestCase):
    def test_publish_edition_has_no_bias_caps(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "news_fetcher"
            / "fetch_and_store_articles.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("_story_balance_bucket", source)
        self.assertNotIn("bias_cap", source)
        self.assertIn("_story_unique_outlet_count", source)
        self.assertIn("discovery_source", source)


class OssBoundaryPrefsTests(unittest.TestCase):
    def test_prefs_blueprint_registered(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "aggregator" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("blueprints.prefs", source)
        self.assertIn("app.register_blueprint(prefs)", source)


if __name__ == "__main__":
    unittest.main()

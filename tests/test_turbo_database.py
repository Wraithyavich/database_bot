import unittest
from pathlib import Path

from turbo_database import TurboDatabase, normalize_number


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = PROJECT_DIR / "turbo_parts.sqlite"


class NormalizeNumberTests(unittest.TestCase):
    def test_removes_separators_and_ignores_case(self) -> None:
        self.assertEqual(normalize_number(" 17201-52010 "), "1720152010")
        self.assertEqual(normalize_number("gt 15-001"), "GT15001")

    def test_replaces_cyrillic_lookalikes(self) -> None:
        self.assertEqual(normalize_number("СT-Н01"), "CTH01")


class TurboDatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = TurboDatabase(DATABASE_PATH)
        cls.stats = cls.database.validate()

    def assert_search_contains(self, query: str, expected_article: str) -> None:
        result = self.database.search(query)
        articles = {match.article for match in result.matches}
        self.assertIn(expected_article, articles)

    def test_database_has_expected_content(self) -> None:
        self.assertEqual(self.stats.parts, 38401)
        self.assertEqual(self.stats.numbers, 458166)
        self.assertEqual(self.stats.crossrefs, 170922)
        self.assertEqual(self.stats.sources, 16)

    def test_turbo_number_search(self) -> None:
        self.assert_search_contains("17201-52010", "AC-T034e")

    def test_oem_number_search(self) -> None:
        result = self.database.search("ERR4893")
        articles = [match.article for match in result.matches]
        self.assertIn("T2-000", articles)
        self.assertEqual(len(articles), len(set(articles)))

    def test_jrone_number_search(self) -> None:
        self.assert_search_contains("1000-010-006", "GT17-013")

    def test_flp_number_search(self) -> None:
        self.assert_search_contains("49131-07000", "FLP-020")

    def test_partial_search(self) -> None:
        result = self.database.search("ERR4")
        self.assertFalse(result.exact)
        self.assertTrue(result.matches)

    def test_legacy_970_fallback(self) -> None:
        result = self.database.search("1000-123-0000")
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.matched_query, "10009700000")
        self.assertTrue(result.matches)

    def test_empty_normalized_query(self) -> None:
        result = self.database.search("---")
        self.assertFalse(result.matches)
        self.assertEqual(result.normalized_query, "")


if __name__ == "__main__":
    unittest.main()

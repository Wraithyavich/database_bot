import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from turbo_database import (
    TurboDatabase,
    ensure_sqlite_database,
    is_sqlite_database,
    normalize_number,
    read_git_lfs_pointer,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = PROJECT_DIR / "turbo_search.sqlite"


class NormalizeNumberTests(unittest.TestCase):
    def test_removes_separators_and_ignores_case(self) -> None:
        self.assertEqual(normalize_number(" 17201-52010 "), "1720152010")
        self.assertEqual(normalize_number("gt 15-001"), "GT15001")

    def test_replaces_cyrillic_lookalikes(self) -> None:
        self.assertEqual(normalize_number("СT-Н01"), "CTH01")


class DatabaseBootstrapTests(unittest.TestCase):
    def test_downloads_database_when_checkout_contains_lfs_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.sqlite"
            with closing(sqlite3.connect(source_path)) as connection:
                connection.execute("CREATE TABLE sources(id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE parts(id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE numbers(id INTEGER PRIMARY KEY)")
                connection.commit()

            payload = source_path.read_bytes()
            oid = hashlib.sha256(payload).hexdigest()
            pointer_path = root / "turbo_parts.sqlite"
            pointer_path.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{oid}\n"
                f"size {len(payload)}\n",
                encoding="ascii",
            )

            pointer = read_git_lfs_pointer(pointer_path)
            self.assertIsNotNone(pointer)
            self.assertEqual(pointer.oid, oid)
            self.assertFalse(is_sqlite_database(pointer_path))

            resolved_path = ensure_sqlite_database(
                pointer_path,
                download_url=source_path.as_uri(),
                cache_dir=root / "cache",
            )

            self.assertNotEqual(resolved_path, pointer_path)
            self.assertTrue(is_sqlite_database(resolved_path))
            self.assertEqual(resolved_path.read_bytes(), payload)

    def test_rejects_lfs_pointer_without_download_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pointer_path = Path(temp_dir) / "turbo_parts.sqlite"
            pointer_path.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{'0' * 64}\n"
                "size 123\n",
                encoding="ascii",
            )

            with self.assertRaisesRegex(sqlite3.DatabaseError, "Git LFS pointer"):
                ensure_sqlite_database(pointer_path, download_url=None)


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
        self.assertEqual(self.stats.numbers, 345396)
        self.assertEqual(self.stats.crossrefs, 0)
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

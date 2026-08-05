import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from build_search_database import build_search_database, collect_audit_stats
from turbo_database import TurboDatabase


class ReverseDatabaseBuildTests(unittest.TestCase):
    def _create_source(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE sources(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT,
                    kind TEXT,
                    category TEXT,
                    imported_at TEXT,
                    notes TEXT
                );
                CREATE TABLE parts(
                    id INTEGER PRIMARY KEY,
                    article TEXT NOT NULL,
                    article_norm TEXT NOT NULL,
                    category TEXT NOT NULL,
                    model TEXT,
                    application TEXT,
                    car_make TEXT,
                    engine TEXT,
                    year TEXT,
                    cooled TEXT,
                    description TEXT,
                    raw_text TEXT,
                    source_id INTEGER,
                    page_start INTEGER,
                    page_end INTEGER,
                    quality TEXT
                );
                CREATE TABLE numbers(
                    id INTEGER PRIMARY KEY,
                    part_id INTEGER,
                    source_id INTEGER,
                    number TEXT NOT NULL,
                    number_norm TEXT NOT NULL,
                    number_type TEXT NOT NULL,
                    match_role TEXT,
                    confidence INTEGER,
                    page INTEGER,
                    raw_context TEXT
                );
                """
            )
            sources = (
                (1, "data.csv"),
                (2, "oemcross.csv"),
                (3, "jronecross.csv"),
                (7, "Turbocharger & Turbo.pdf"),
                (9, "Actuator.pdf"),
            )
            connection.executemany(
                "INSERT INTO sources(id, name) VALUES (?, ?)", sources
            )
            connection.execute(
                """INSERT INTO parts(
                    id, article, article_norm, category, raw_text, source_id
                ) VALUES (1, 'AC-G010-3', 'ACG0103', 'Актуаторы',
                    'AC-G010-3 TB2818 445963-0105 702365-0010 711380-0007', 1)"""
            )
            rows = (
                (1, 1, 1, "702365-0010", "7023650010", "Turbo P/N", "crossref_input", None),
                (2, 1, 1, "711380-0007", "7113800007", "Turbo P/N", "crossref_input", None),
                (3, 1, 9, "AC-G010-3", "ACG0103", "E&E P/N", "article", 3),
                (4, 1, 9, "445963-0105", "4459630105", "Turbo P/N", "catalog_number", 3),
                (5, 1, 9, "702365-0010", "7023650010", "Turbo P/N", "catalog_number", 3),
                (6, 1, 9, "711380-0007", "7113800007", "Turbo P/N", "catalog_number", 3),
                (7, 1, 9, "TB2818", "TB2818", "Vehicle OE No / OEM", "catalog_number", 3),
            )
            connection.executemany(
                """INSERT INTO numbers(
                    id, part_id, source_id, number, number_norm, number_type,
                    match_role, page, raw_context
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                    'AC-G010-3 TB2818 445963-0105 702365-0010 711380-0007')""",
                rows,
            )
            connection.commit()

    def test_repeatable_atomic_build_preserves_direct_and_reverse_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.sqlite"
            destination = root / "search.sqlite"
            audit = root / "audit.md"
            backup_dir = root / "backups"
            self._create_source(source)

            build_search_database(source, destination, audit_path=audit)
            first_size = destination.stat().st_size
            build_search_database(
                source,
                destination,
                backup_dir=backup_dir,
                audit_path=audit,
            )

            self.assertEqual(destination.stat().st_size, first_size)
            self.assertEqual(len(tuple(backup_dir.glob("*.bak"))), 1)
            database = TurboDatabase(destination)
            reverse = database.reverse_search("AC-G010-3")
            self.assertEqual(
                [number.number for number in reverse.numbers_of_kind("turbo_pn")],
                ["702365-0010", "711380-0007"],
            )
            self.assertEqual(
                [number.number for number in reverse.numbers_of_kind("component_pn")],
                ["445963-0105"],
            )
            self.assertFalse(reverse.numbers_of_kind("vehicle_oem"))
            self.assertIn(
                "AC-G010-3",
                [match.article for match in database.search("702365-0010").matches],
            )
            self.assertTrue(audit.is_file())
            self.assertEqual(collect_audit_stats(destination)["orphan_relations"], 0)


if __name__ == "__main__":
    unittest.main()

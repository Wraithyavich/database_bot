import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from reverse_search import (
    ClassificationContext,
    NumberEvidence,
    classify_number_evidence,
    compact_article,
    natural_sort_key,
    normalize_article,
)
from script import format_reverse_search_result, split_long_message
from turbo_database import ReverseNumber, ReverseSearchResult, TurboDatabase


PROJECT_DIR = Path(__file__).resolve().parents[1]
REAL_DATABASE_PATH = PROJECT_DIR / "turbo_search.sqlite"


def _empty_context(
    *,
    first: frozenset[tuple[int, int, str]] = frozenset(),
    turbo: dict[str, frozenset[str]] | None = None,
) -> ClassificationContext:
    return ClassificationContext(
        trusted_turbo_by_article=turbo or {},
        trusted_vehicle_by_article={},
        catalog_has_trusted_turbo=frozenset(),
        first_catalog_number=first,
        bearing_pre_application=frozenset(),
    )


def _evidence(
    *,
    source_id: int,
    number: str,
    number_type: str,
    part_id: int = 1,
    article_norm: str = "TEST-001",
) -> NumberEvidence:
    return NumberEvidence(
        part_id=part_id,
        article_norm=article_norm,
        source_id=source_id,
        source_catalog=f"source-{source_id}",
        number=number,
        number_norm="".join(character for character in number if character.isalnum()),
        number_type=number_type,
        match_role="catalog_number",
        source_page=1,
        raw_context="",
        catalog_order=1,
    )


class ArticleNormalizationTests(unittest.TestCase):
    def test_normalizes_case_dash_variants_and_spaces(self) -> None:
        self.assertEqual(normalize_article("  turbo — g189  "), "TURBO-G189")
        self.assertEqual(normalize_article("gt17‑092"), "GT17-092")

    def test_preserves_significant_suffixes(self) -> None:
        for article in (
            "GT17-092-1",
            "CW-1614BR",
            "B3-000-C1B",
            "B3-000TB",
            "TN-Turbo-H124-oe",
        ):
            with self.subTest(article=article):
                self.assertEqual(normalize_article(article), article.upper())

    def test_compact_key_is_separate_from_exact_normalization(self) -> None:
        self.assertEqual(normalize_article("GT17-092"), "GT17-092")
        self.assertEqual(compact_article("GT17-092"), "GT17092")

    def test_natural_sort(self) -> None:
        values = ["454163-0010", "454163-0002", "454163-0001"]
        self.assertEqual(
            sorted(values, key=natural_sort_key),
            ["454163-0001", "454163-0002", "454163-0010"],
        )


class SemanticClassificationTests(unittest.TestCase):
    def test_turbo_catalog_maps_columns_semantically(self) -> None:
        turbo = classify_number_evidence(
            _evidence(
                source_id=7,
                number="466898-0005",
                number_type="Turbo P/N",
            ),
            _empty_context(),
        )
        vehicle = classify_number_evidence(
            _evidence(source_id=7, number="2910099000", number_type="Number"),
            _empty_context(),
        )
        model = classify_number_evidence(
            _evidence(
                source_id=7,
                number="TB2518",
                number_type="Vehicle OE No / OEM",
            ),
            _empty_context(),
        )
        self.assertEqual(turbo.number_kind, "turbo_pn")
        self.assertEqual(vehicle.number_kind, "vehicle_oem")
        self.assertEqual(model.number_kind, "unknown")

    def test_chra_and_actuator_first_part_numbers_are_components(self) -> None:
        chra = _evidence(
            source_id=8,
            number="443854-0136",
            number_type="Turbo P/N",
        )
        actuator = _evidence(
            source_id=9,
            number="445963-0105",
            number_type="Turbo P/N",
        )
        context = _empty_context(
            first=frozenset(
                {
                    (1, 8, chra.number_norm),
                    (1, 9, actuator.number_norm),
                }
            )
        )
        self.assertEqual(
            classify_number_evidence(chra, context).number_kind,
            "component_pn",
        )
        self.assertEqual(
            classify_number_evidence(actuator, context).number_kind,
            "component_pn",
        )

    def test_repair_kit_application_is_turbo_pn(self) -> None:
        classified = classify_number_evidence(
            _evidence(
                source_id=15,
                number="454163-0002",
                number_type="Turbo P/N",
            ),
            _empty_context(),
        )
        self.assertEqual(classified.number_kind, "turbo_pn")


class ReverseSearchFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "fixture.sqlite"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE sources(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE parts(
                    id INTEGER PRIMARY KEY,
                    article TEXT NOT NULL,
                    article_norm TEXT NOT NULL,
                    article_compact TEXT NOT NULL,
                    category TEXT NOT NULL
                );
                CREATE TABLE numbers(
                    part_id INTEGER NOT NULL,
                    source_id INTEGER NOT NULL,
                    number_norm TEXT NOT NULL,
                    number_type TEXT NOT NULL
                );
                CREATE TABLE part_numbers(
                    id INTEGER PRIMARY KEY,
                    part_id INTEGER NOT NULL,
                    source_id INTEGER NOT NULL,
                    number TEXT NOT NULL,
                    number_norm TEXT NOT NULL,
                    number_kind TEXT NOT NULL,
                    source_catalog TEXT NOT NULL,
                    source_page INTEGER,
                    source_column TEXT NOT NULL,
                    confidence INTEGER NOT NULL,
                    catalog_order INTEGER NOT NULL
                );
                CREATE TABLE article_aliases(
                    alias_norm TEXT NOT NULL,
                    target_article_norm TEXT NOT NULL,
                    alias_kind TEXT NOT NULL,
                    source_note TEXT NOT NULL
                );
                INSERT INTO sources VALUES (1, 'fixture');
                """
            )
            articles = (
                (1, "Turbo-G189", "Турбины"),
                (2, "ONLY-T", "Турбины"),
                (3, "ONLY-O", "Турбины"),
                (4, "EMPTY-1", "Картриджи"),
                (5, "GT17-092", "Картриджи"),
                (6, "GT17-092-1", "Картриджи"),
                (7, "AB-12", "Прочее"),
                (8, "A-B12", "Прочее"),
                (9, "GK-0552", "Комплекты прокладок"),
            )
            connection.executemany(
                "INSERT INTO parts VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        part_id,
                        article,
                        normalize_article(article),
                        compact_article(article),
                        category,
                    )
                    for part_id, article, category in articles
                ),
            )
            relations = (
                (1, "466898-0005", "turbo_pn"),
                (1, "2910099000", "vehicle_oem"),
                (2, "700001-0001", "turbo_pn"),
                (3, "00001234", "vehicle_oem"),
                (5, "778400-0005", "turbo_pn"),
                (6, "778400-0002", "turbo_pn"),
                (9, "454163-0001", "turbo_pn"),
                (9, "454163-0002", "turbo_pn"),
                (9, "2 505 034", "component_pn"),
            )
            connection.executemany(
                """
                INSERT INTO part_numbers(
                    part_id, source_id, number, number_norm, number_kind,
                    source_catalog, source_page, source_column, confidence,
                    catalog_order
                ) VALUES (?, 1, ?, ?, ?, 'fixture', 1, 'fixture', 100, ?)
                """,
                (
                    (
                        part_id,
                        number,
                        "".join(c for c in number.upper() if c.isalnum()),
                        kind,
                        order,
                    )
                    for order, (part_id, number, kind) in enumerate(relations, 1)
                ),
            )
            connection.execute(
                "INSERT INTO numbers VALUES (1, 1, '99990001', 'Turbo P/N')"
            )
            connection.execute(
                """INSERT INTO article_aliases VALUES (
                    'CAST-G189', 'TURBO-G189', 'explicit_equivalence', 'fixture')"""
            )
            connection.executescript(
                """
                CREATE INDEX idx_numbers_norm ON numbers(number_norm);
                CREATE INDEX idx_parts_article_norm ON parts(article_norm);
                CREATE INDEX idx_parts_article_compact ON parts(article_compact);
                CREATE INDEX idx_part_numbers_part_kind
                    ON part_numbers(part_id, number_kind);
                CREATE INDEX idx_aliases_alias_norm ON article_aliases(alias_norm);
                """
            )
            connection.commit()
        self.database = TurboDatabase(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_exact_reverse_search_and_deduplicated_groups(self) -> None:
        result = self.database.reverse_search(" turbo – g189 ")
        self.assertTrue(result.found)
        self.assertEqual(result.resolution, "exact")
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("turbo_pn")],
            ["466898-0005"],
        )
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("vehicle_oem")],
            ["2910099000"],
        )

    def test_explicit_alias_tn_and_tn_oe(self) -> None:
        self.assertEqual(
            self.database.reverse_search("CAST-G189").resolution,
            "alias",
        )
        self.assertEqual(
            self.database.reverse_search("TN-Turbo-G189").resolution,
            "tn_alias",
        )
        self.assertEqual(
            self.database.reverse_search("TN-Turbo-G189-oe").matched_article,
            "Turbo-G189",
        )
        self.assertIsNone(self.database.reverse_search("Turbo-G189-oe"))

    def test_suffix_variants_do_not_mix(self) -> None:
        base = self.database.reverse_search("GT17-092")
        suffix = self.database.reverse_search("GT17-092-1")
        self.assertEqual(base.matched_article, "GT17-092")
        self.assertEqual(suffix.matched_article, "GT17-092-1")
        self.assertNotEqual(base.numbers, suffix.numbers)

    def test_only_turbo_only_oem_and_article_without_numbers(self) -> None:
        only_turbo = self.database.reverse_search("ONLY-T")
        only_oem = self.database.reverse_search("ONLY-O")
        empty = self.database.reverse_search("EMPTY-1")
        self.assertTrue(only_turbo.numbers_of_kind("turbo_pn"))
        self.assertFalse(only_turbo.numbers_of_kind("vehicle_oem"))
        self.assertTrue(only_oem.numbers_of_kind("vehicle_oem"))
        self.assertFalse(only_oem.numbers_of_kind("turbo_pn"))
        self.assertTrue(empty.found)
        self.assertFalse(empty.numbers)
        self.assertIn(
            "Артикул найден, но в текущей базе",
            "\n".join(format_reverse_search_result(empty)),
        )

    def test_component_is_not_vehicle_oem(self) -> None:
        result = self.database.reverse_search("GK-0552")
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("component_pn")],
            ["2 505 034"],
        )
        self.assertFalse(result.numbers_of_kind("vehicle_oem"))

    def test_ambiguous_compact_fallback_returns_candidates(self) -> None:
        result = self.database.reverse_search("AB12")
        self.assertFalse(result.found)
        self.assertEqual(result.resolution, "compact_ambiguous")
        self.assertEqual(set(result.candidates), {"AB-12", "A-B12"})

    def test_nonexistent_article_falls_through_to_direct_search(self) -> None:
        self.assertIsNone(self.database.reverse_search("9999-0001"))
        direct = self.database.search("9999-0001")
        self.assertEqual([match.article for match in direct.matches], ["Turbo-G189"])

    def test_sql_injection_is_data_not_sql(self) -> None:
        attack = "' OR 1=1 --"
        self.assertIsNone(self.database.reverse_search(attack))
        self.assertFalse(self.database.search(attack).matches)

    def test_gasket_kit_fixture_has_full_expected_semantics(self) -> None:
        result = self.database.reverse_search("GK-0552")
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("turbo_pn")],
            ["454163-0001", "454163-0002"],
        )
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("component_pn")],
            ["2 505 034"],
        )


class ReverseFormattingTests(unittest.TestCase):
    def test_format_has_counts_code_elements_and_no_empty_groups(self) -> None:
        result = ReverseSearchResult(
            original_query="A-1",
            normalized_query="A-1",
            matched_article="A-1",
            matched_article_norm="A-1",
            categories=("Турбины",),
            numbers=(
                ReverseNumber("10-2", "102", "turbo_pn", "fixture", 1),
                ReverseNumber("10-10", "1010", "turbo_pn", "fixture", 1),
            ),
            resolution="exact",
        )
        text = "\n".join(format_reverse_search_result(result))
        self.assertIn("Turbo P/N — 2", text)
        self.assertIn("<code>10-2</code>", text)
        self.assertNotIn("OEM / Vehicle OE", text)

    def test_long_result_is_split_without_losing_numbers(self) -> None:
        lines = ["Заголовок"] + [f"<code>N-{index:04d}</code>" for index in range(80)]
        chunks = split_long_message(lines, limit=180, number_parts=True)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith(f"1/{len(chunks)}\n"))
        self.assertTrue(chunks[-1].startswith(f"{len(chunks)}/{len(chunks)}\n"))
        joined = "\n".join(chunks)
        for index in range(80):
            self.assertEqual(joined.count(f"<code>N-{index:04d}</code>"), 1)
        self.assertTrue(all(len(chunk) <= 180 for chunk in chunks))


@unittest.skipUnless(REAL_DATABASE_PATH.is_file(), "working database is absent")
class RealDatabaseReverseSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = TurboDatabase(REAL_DATABASE_PATH)
        if cls.database.validate().reverse_numbers == 0:
            raise unittest.SkipTest("working database has no reverse-search schema")

    def test_turbo_g189(self) -> None:
        result = self.database.reverse_search("Turbo-G189")
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("turbo_pn")],
            [],
        )
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("component_pn")],
            [
                "466898-0005",
                "466898-0006",
                "466898-0007",
                "466898-0008",
                "466898-0009",
            ],
        )
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("vehicle_oem")],
            ["2910099000", "2910099001"],
        )

    def test_ac_g010_3_component_is_separate(self) -> None:
        result = self.database.reverse_search("AC-G010-3")
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("turbo_pn")],
            ["702365-0010", "702365-0015", "702365-0018", "711380-0007"],
        )
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("component_pn")],
            ["445963-0105"],
        )
        self.assertNotIn(
            "445963-0105",
            [number.number for number in result.numbers_of_kind("vehicle_oem")],
        )

    def test_gk_0552_uses_reviewed_service_number_types(self) -> None:
        result = self.database.reverse_search("GK-0552")
        self.assertFalse(result.numbers_of_kind("turbo_pn"))
        self.assertEqual(
            [number.number for number in result.numbers_of_kind("component_pn")],
            ["2 505 034"],
        )


if __name__ == "__main__":
    unittest.main()

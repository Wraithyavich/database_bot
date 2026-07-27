import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from turbo_database import TurboDatabase
from vin_search import (
    NhtsaVinDecoder,
    VinFitment,
    VinRecord,
    VinSource,
    VinStore,
    extract_vin,
    format_online_vin,
    format_pending_vin,
    format_verified_vin,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
VIN_SEED_PATH = PROJECT_DIR / "vin_verified.json"
TURBO_DATABASE_PATH = PROJECT_DIR / "turbo_search.sqlite"
KNOWN_VIN = "SALLSAAG4AA249280"


class VinRecognitionTests(unittest.TestCase):
    def test_extracts_direct_vin(self) -> None:
        self.assertEqual(extract_vin(KNOWN_VIN.lower()), KNOWN_VIN)

    def test_extracts_vin_from_message(self) -> None:
        self.assertEqual(extract_vin(f"VIN: {KNOWN_VIN}"), KNOWN_VIN)

    def test_rejects_part_numbers_and_forbidden_letters(self) -> None:
        self.assertIsNone(extract_vin("778400-0003"))
        self.assertIsNone(extract_vin("SALLSAAG4AI249280"))
        self.assertIsNone(extract_vin("12345678901234567"))


class VinStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "vin_cache.sqlite"
        self.store = VinStore(self.database_path)
        self.store.initialize(seed_path=VIN_SEED_PATH)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_imports_verified_seed(self) -> None:
        record = self.store.lookup(KNOWN_VIN)

        self.assertIsNotNone(record)
        self.assertEqual(record.status, "verified")
        self.assertEqual(record.engine, "3.0 TDV6")
        self.assertEqual(
            [fitment.articles for fitment in record.fitments],
            [("GT17-092-1",), ("GT14-009",)],
        )

    def test_records_unknown_vin_and_request_count(self) -> None:
        unknown_vin = "SALWR2VF0FA000001"
        decoded = VinRecord(
            vin=unknown_vin,
            status="pending",
            make="LAND ROVER",
            model_year="2015",
        )

        self.store.record_request(unknown_vin, decoded=decoded)
        self.store.record_request(unknown_vin, decoded=decoded)

        record = self.store.lookup(unknown_vin)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "pending")
        self.assertEqual(self.store.stats().pending, 1)
        self.assertEqual(self.store.stats().requests, 2)
        self.assertEqual(self.store.pending(), (record,))

    def test_pending_request_does_not_downgrade_verified_record(self) -> None:
        pending = VinRecord(vin=KNOWN_VIN, status="pending", make="WRONG")

        record = self.store.record_request(KNOWN_VIN, decoded=pending)

        self.assertEqual(record.status, "verified")
        self.assertEqual(record.make, "Land Rover")

    def test_claims_daily_event_only_once_per_date(self) -> None:
        self.assertTrue(
            self.store.claim_daily_event("special-greeting", "2026-07-27")
        )
        self.assertFalse(
            self.store.claim_daily_event("special-greeting", "2026-07-27")
        )
        self.assertTrue(
            self.store.claim_daily_event("special-greeting", "2026-07-28")
        )

    def test_saves_manual_verified_result(self) -> None:
        unknown_vin = "SALWR2VF0FA000001"
        self.store.record_request(
            unknown_vin,
            decoded=VinRecord(
                vin=unknown_vin,
                status="pending",
                make="LAND ROVER",
            ),
        )
        verified = VinRecord(
            vin=unknown_vin,
            status="verified",
            make="LAND ROVER",
            fitments=(
                VinFitment(
                    position="Левая",
                    oem_numbers=(),
                    turbo_numbers=("KP39-015",),
                    articles=(),
                ),
            ),
        )

        self.store.save_verified(verified)

        self.assertEqual(self.store.lookup(unknown_vin), verified)
        self.assertEqual(self.store.stats().verified, 2)
        self.assertEqual(self.store.stats().pending, 0)

    def test_saves_observer_result_without_incrementing_requests(self) -> None:
        unknown_vin = "SALWR2VF0FA000002"
        pending = VinRecord(
            vin=unknown_vin,
            status="pending",
            make="LAND ROVER",
            fitments=(
                VinFitment(
                    position="Турбина",
                    oem_numbers=(),
                    turbo_numbers=("778400-0003",),
                    articles=("GT17-092-1",),
                ),
            ),
            online_search_at="2026-07-27T00:00:00+00:00",
        )

        self.store.save_pending(pending)

        self.assertEqual(self.store.lookup(unknown_vin), pending)
        self.assertEqual(self.store.stats().requests, 0)

    def test_persists_preliminary_online_result(self) -> None:
        unknown_vin = "SALWR2VF0FA000001"
        preliminary = VinRecord(
            vin=unknown_vin,
            status="pending",
            fitments=(
                VinFitment(
                    position="Левая",
                    oem_numbers=("LR012345",),
                    turbo_numbers=("778400-0003",),
                    articles=("GT17-092-1",),
                    evidence="Каталожное совпадение.",
                ),
            ),
            online_search_at="2026-07-26T00:00:00+00:00",
            online_search_provider="Gemini + Google Search",
        )

        self.store.record_request(unknown_vin, decoded=preliminary)
        stored = self.store.lookup(unknown_vin)

        self.assertEqual(stored, preliminary)

    def test_seed_articles_exist_for_stored_turbo_numbers(self) -> None:
        record = self.store.lookup(KNOWN_VIN)
        self.assertIsNotNone(record)
        turbo_database = TurboDatabase(TURBO_DATABASE_PATH)

        for fitment in record.fitments:
            expected = set(fitment.articles)
            for turbo_number in fitment.turbo_numbers:
                result = turbo_database.search(turbo_number)
                actual = {match.article for match in result.matches}
                self.assertTrue(expected <= actual)


class NhtsaVinDecoderTests(unittest.TestCase):
    def test_decodes_supported_fields(self) -> None:
        payload = json.dumps(
            {
                "Results": [
                    {
                        "Make": "LAND ROVER",
                        "Model": "RANGE ROVER SPORT",
                        "ModelYear": "2010",
                        "EngineModel": "306DT",
                        "EngineKW": "180",
                    }
                ]
            }
        ).encode("utf-8")

        with patch(
            "vin_search.urllib.request.urlopen",
            return_value=io.BytesIO(payload),
        ):
            record = NhtsaVinDecoder().decode(KNOWN_VIN)

        self.assertEqual(record.status, "pending")
        self.assertEqual(record.make, "LAND ROVER")
        self.assertEqual(record.model, "RANGE ROVER SPORT")
        self.assertEqual(record.model_year, "2010")
        self.assertEqual(record.engine, "306DT")
        self.assertEqual(record.power_kw, "180")


class VinFormattingTests(unittest.TestCase):
    def test_formats_verified_result_with_articles_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=VIN_SEED_PATH)
            record = store.lookup(KNOWN_VIN)

        self.assertIsNotNone(record)
        message = "\n".join(format_verified_vin(record))
        self.assertIn("Проверенный результат", message)
        self.assertIn("GT17-092-1", message)
        self.assertIn("GT14-009", message)
        self.assertIn("Jaguar Land Rover", message)

    def test_formats_pending_result_without_part_numbers(self) -> None:
        record = VinRecord(
            vin="SALWR2VF0FA000001",
            status="pending",
            make="LAND ROVER",
            sources=(VinSource("NHTSA", "https://example.test"),),
        )

        message = "\n".join(format_pending_vin(record))

        self.assertIn("очереди на проверку", message)
        self.assertNotIn("Turbo P/N", message)

    def test_formats_online_result_as_unverified(self) -> None:
        record = VinRecord(
            vin="SALWR2VF0FA000001",
            status="pending",
            make="LAND ROVER",
            fitments=(
                VinFitment(
                    position="Левая",
                    oem_numbers=("LR012345",),
                    turbo_numbers=("778400-0003",),
                    articles=("GT17-092-1",),
                    evidence="Найдено в открытом каталоге.",
                ),
            ),
            sources=(
                VinSource("Parts catalog", "https://example.test/fitment"),
            ),
            online_search_at="2026-07-26T00:00:00+00:00",
        )

        message = "\n".join(format_online_vin(record))

        self.assertIn("ПРЕДВАРИТЕЛЬНЫЙ", message)
        self.assertIn("778400-0003", message)
        self.assertIn("GT17-092-1", message)
        self.assertIn("могут быть неточными", message)
        self.assertIn("https://example.test/fitment", message)


if __name__ == "__main__":
    unittest.main()

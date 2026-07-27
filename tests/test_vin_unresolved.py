import csv
import tempfile
import unittest
from pathlib import Path

from vin_admin import _export_unresolved_csv
from vin_search import VinRecord
from vin_unresolved import UnresolvedVinStore


UNKNOWN_VIN = "SALWR2VF0FA000001"


class UnresolvedVinStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temp_dir.name) / "vin_unresolved.sqlite"
        )
        self.store = UnresolvedVinStore(self.database_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_records_failures_in_dedicated_database(self) -> None:
        record = VinRecord(
            vin=UNKNOWN_VIN,
            status="pending",
            make="LAND ROVER",
            model="RANGE ROVER SPORT",
            model_year="2015",
            engine="306DT",
        )

        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="no_supported_turbo_numbers",
            failure_detail="No supported numbers found.",
            record=record,
        )
        stored = self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="online_search_error",
            record=VinRecord(vin=UNKNOWN_VIN, status="pending"),
        )

        self.assertEqual(stored.request_count, 2)
        self.assertEqual(stored.failure_code, "online_search_error")
        self.assertEqual(stored.make, "LAND ROVER")
        self.assertEqual(stored.engine, "306DT")
        self.assertEqual(self.store.stats().unique_vins, 1)
        self.assertEqual(self.store.stats().requests, 2)

    def test_removes_vin_after_successful_processing(self) -> None:
        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="online_search_error",
        )

        self.assertTrue(self.store.remove(UNKNOWN_VIN))
        self.assertFalse(self.store.remove(UNKNOWN_VIN))
        self.assertEqual(self.store.list(), ())

    def test_claims_each_admin_notification_only_once(self) -> None:
        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="online_search_error",
        )

        self.assertTrue(self.store.claim_notification(UNKNOWN_VIN, 1219230738))
        self.assertFalse(self.store.claim_notification(UNKNOWN_VIN, 1219230738))
        self.assertTrue(self.store.claim_notification(UNKNOWN_VIN, 479066342))

        self.store.mark_notification_sent(
            UNKNOWN_VIN,
            1219230738,
            5001,
        )
        self.assertEqual(
            self.store.find_notification_vin(1219230738, 5001),
            UNKNOWN_VIN,
        )

    def test_failed_notification_can_be_retried(self) -> None:
        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="online_search_error",
        )
        self.assertTrue(self.store.claim_notification(UNKNOWN_VIN, 1219230738))
        self.assertTrue(
            self.store.release_notification(UNKNOWN_VIN, 1219230738)
        )
        self.assertTrue(self.store.claim_notification(UNKNOWN_VIN, 1219230738))

    def test_removing_vin_removes_notification_mapping(self) -> None:
        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="online_search_error",
        )
        self.store.claim_notification(UNKNOWN_VIN, 1219230738)
        self.store.mark_notification_sent(UNKNOWN_VIN, 1219230738, 5002)

        self.store.remove(UNKNOWN_VIN)

        self.assertIsNone(
            self.store.find_notification_vin(1219230738, 5002)
        )

    def test_exports_anonymous_csv_sample(self) -> None:
        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="no_supported_turbo_numbers",
            record=VinRecord(
                vin=UNKNOWN_VIN,
                status="pending",
                make="LAND ROVER",
            ),
        )
        output = Path(self.temp_dir.name) / "sample.csv"

        _export_unresolved_csv(output, self.store.list())

        with output.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["vin"], UNKNOWN_VIN)
        self.assertEqual(rows[0]["make"], "LAND ROVER")
        self.assertNotIn("user_id", rows[0])
        self.assertNotIn("chat_id", rows[0])


if __name__ == "__main__":
    unittest.main()

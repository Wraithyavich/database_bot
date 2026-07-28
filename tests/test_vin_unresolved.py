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

    def test_observer_claims_due_job_and_reschedules_it(self) -> None:
        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="no_supported_turbo_numbers",
            observer_delay_seconds=0,
        )

        job = self.store.claim_due_observer_job(
            daily_limit=5,
            now="2099-01-01T00:00:00+00:00",
        )
        self.assertIsNotNone(job)
        self.assertEqual(job.vin, UNKNOWN_VIN)
        self.assertEqual(job.attempt_count, 0)

        completed = self.store.complete_observer_attempt(
            UNKNOWN_VIN,
            next_delay_seconds=3600,
            result="not found",
            now="2099-01-01T00:00:10+00:00",
        )
        self.assertEqual(completed.attempt_count, 1)
        self.assertEqual(completed.last_result, "not found")
        self.assertEqual(
            completed.next_attempt_at,
            "2099-01-01T01:00:10+00:00",
        )

    def test_observer_enforces_persistent_daily_limit(self) -> None:
        second_vin = "SALWR2VF0FA000002"
        for vin in (UNKNOWN_VIN, second_vin):
            self.store.record_failure(
                vin,
                failure_code="no_supported_turbo_numbers",
                observer_delay_seconds=0,
            )

        first = self.store.claim_due_observer_job(
            daily_limit=1,
            now="2099-01-01T00:00:00+00:00",
        )
        second = self.store.claim_due_observer_job(
            daily_limit=1,
            now="2099-01-01T01:00:00+00:00",
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_observer_attempt_report_survives_queue_removal(self) -> None:
        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="no_supported_turbo_numbers",
        )
        stored = self.store.record_observer_attempt(
            UNKNOWN_VIN,
            stage="emex",
            status="not_found",
            summary="Vehicle found, turbo group missing.",
            checked_sources=("https://ru.emexdwc.ae/Vehicles.aspx",),
            report={
                "vehicle_candidates": 1,
                "turbo_units": [],
            },
            now="2099-01-01T00:00:00+00:00",
        )

        self.store.remove(UNKNOWN_VIN)
        attempts = self.store.list_observer_attempts(vin=UNKNOWN_VIN)

        self.assertEqual(attempts, (stored,))
        self.assertEqual(attempts[0].stage, "emex")
        self.assertEqual(attempts[0].status, "not_found")
        self.assertEqual(attempts[0].report["vehicle_candidates"], 1)

    def test_result_subscription_tracks_delivery_after_queue_removal(self) -> None:
        self.store.record_failure(
            UNKNOWN_VIN,
            failure_code="no_supported_turbo_numbers",
        )
        subscription = self.store.subscribe_result(
            UNKNOWN_VIN,
            user_id=456,
            chat_id=456,
            username="requester",
            status_message_id=1001,
        )

        self.store.remove(UNKNOWN_VIN)
        self.assertEqual(
            self.store.pending_result_subscriptions(UNKNOWN_VIN),
            (subscription,),
        )

        self.store.mark_result_delivered(subscription.id)
        self.assertEqual(
            self.store.pending_result_subscriptions(UNKNOWN_VIN),
            (),
        )

    def test_manual_request_is_deduplicated_until_completed(self) -> None:
        request = self.store.claim_manual_request(
            UNKNOWN_VIN,
            user_id=456,
            chat_id=456,
            username="requester",
        )

        self.assertIsNotNone(request)
        self.assertIsNone(
            self.store.claim_manual_request(
                UNKNOWN_VIN,
                user_id=456,
                chat_id=456,
                username="requester",
            )
        )
        self.assertEqual(
            self.store.pending_manual_requests(UNKNOWN_VIN),
            (request,),
        )

        self.store.mark_manual_request_completed(request.id)
        self.assertEqual(
            self.store.pending_manual_requests(UNKNOWN_VIN),
            (),
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

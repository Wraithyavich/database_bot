import json
import io
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from turbo_database import TurboDatabase
from vin_agent_worker import (
    CodexRunner,
    TelegramNotifier,
    VinAgentService,
    parse_agent_result,
)
from vin_search import VinFitment, VinRecord, VinSource, VinStore
from vin_unresolved import UnresolvedVinStore, VinResultSubscription


VIN = "SALWR2VF0FA000007"
DATABASE_PATH = Path(__file__).resolve().parents[1] / "turbo_search.sqlite"


def result_document(*, status: str = "found") -> dict:
    return {
        "status": status,
        "vin": VIN,
        "vehicle": {
            "make": "LAND ROVER",
            "model": "RANGE ROVER SPORT",
            "model_year": "2015",
            "engine": "306DT",
            "power_kw": "215",
        },
        "fitments": [
            {
                "position": "Левая",
                "oem_numbers": ["LR056369"],
                "turbo_numbers": ["778400-0003"],
                "evidence": "Каталог связывает номер с двигателем 306DT.",
            }
        ],
        "sources": [
            {
                "label": "Публичный каталог",
                "url": "https://catalog.example/fitment",
            }
        ],
        "checked_sources": ["OEM catalog", "Turbo manufacturer"],
        "confidence": "medium",
        "summary": "Номер требует проверки по шильдику.",
    }


class AgentResultParsingTests(unittest.TestCase):
    def test_parses_sourced_part_numbers_as_pending_candidate(self) -> None:
        record = parse_agent_result(
            result_document(),
            base_record=VinRecord(vin=VIN, status="pending"),
        )

        self.assertEqual(record.status, "pending")
        self.assertEqual(record.fitments[0].oem_numbers, ("LR056369",))
        self.assertEqual(
            record.fitments[0].turbo_numbers,
            ("778400-0003",),
        )
        self.assertEqual(record.online_search_provider, "Codex agent + public web")

    def test_vehicle_identity_without_numbers_is_not_success(self) -> None:
        document = result_document()
        document["fitments"] = []

        record = parse_agent_result(
            document,
            base_record=VinRecord(vin=VIN, status="pending"),
        )

        self.assertEqual(record.fitments, ())
        self.assertIn("Проверены источники", record.notes)

    def test_discards_unsourced_model_numbers(self) -> None:
        document = result_document()
        document["sources"] = []

        record = parse_agent_result(
            document,
            base_record=VinRecord(vin=VIN, status="pending"),
        )

        self.assertEqual(record.fitments, ())

    def test_rejects_result_for_another_vin(self) -> None:
        document = result_document()
        document["vin"] = "SALLSAAG4AA249280"

        with self.assertRaises(RuntimeError):
            parse_agent_result(
                document,
                base_record=VinRecord(vin=VIN, status="pending"),
            )


class CodexRunnerTests(unittest.TestCase):
    def test_does_not_pass_bot_or_yandex_secrets_to_codex(self) -> None:
        captured_environment = {}

        def fake_run(command, **kwargs):
            captured_environment.update(kwargs["env"])
            output_path = Path(
                command[command.index("--output-last-message") + 1]
            )
            output_path.write_text(
                json.dumps(result_document()),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(
                os.environ,
                {
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": temp_dir,
                    "CODEX_HOME": str(Path(temp_dir) / ".codex"),
                    "API_TOKEN": "telegram-secret",
                    "YANDEX_API_KEY": "yandex-secret",
                },
                clear=True,
            ),
            patch("vin_agent_worker.subprocess.run", side_effect=fake_run),
        ):
            runner = CodexRunner(
                executable="codex",
                model="gpt-5.6-luna",
                reasoning_effort="low",
                schema_path=Path(temp_dir) / "schema.json",
                timeout_seconds=60,
            )
            runner.search(VinRecord(vin=VIN, status="pending"))

        self.assertNotIn("API_TOKEN", captured_environment)
        self.assertNotIn("YANDEX_API_KEY", captured_environment)
        self.assertIn("CODEX_HOME", captured_environment)


class TelegramNotifierTests(unittest.TestCase):
    def test_edits_waiting_message_with_manual_review_button(self) -> None:
        captured = {}

        def fake_urlopen(request, *, timeout):
            captured["url"] = request.full_url
            captured["data"] = urllib.parse.parse_qs(
                request.data.decode("utf-8")
            )
            return io.BytesIO(b'{"ok": true}')

        subscription = VinResultSubscription(
            id=1,
            vin=VIN,
            user_id=456,
            chat_id=456,
            username="requester",
            status_message_id=1001,
            requested_at="2026-07-29T00:00:00+00:00",
            delivered_at="",
        )
        with patch(
            "vin_agent_worker.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            delivered = TelegramNotifier("test-token").send_result(
                subscription,
                text=f"VIN: {VIN}\nOEM: LR012345",
                vin=VIN,
            )

        self.assertTrue(delivered)
        self.assertTrue(captured["url"].endswith("/editMessageText"))
        self.assertEqual(captured["data"]["message_id"], ["1001"])
        self.assertEqual(captured["data"]["parse_mode"], ["HTML"])
        self.assertIn(
            f"vin_review:{VIN}",
            captured["data"]["reply_markup"][0],
        )


class VinAgentServiceTests(unittest.TestCase):
    def test_emex_result_skips_codex_and_maps_local_articles(self) -> None:
        class UnexpectedRunner:
            def search(self, record):
                raise AssertionError("Codex must not run after Emex success")

        class CapturingNotifier:
            def __init__(self):
                self.messages = []

            def send_result(self, subscription, *, text, vin):
                self.messages.append(text)
                return True

        emex_record = VinRecord(
            vin=VIN,
            status="pending",
            make="Audi",
            model="A4/Avant",
            fitments=(
                VinFitment(
                    position="Турбокомпрессор, группа 145-020",
                    oem_numbers=("06L145874E", "06L145722TX"),
                    turbo_numbers=(),
                    articles=(),
                    evidence="VIN-фильтрованный каталог Emex DWC.",
                ),
            ),
            sources=(
                VinSource(
                    label="Emex DWC",
                    url=(
                        "https://ru.emexdwc.ae/Search.aspx?"
                        f"n=06L145874E&vin={VIN}"
                    ),
                ),
            ),
            online_search_provider="Emex DWC VIN-каталог",
        )
        emex = SimpleNamespace(
            search=lambda record: SimpleNamespace(
                record=emex_record,
                status="found",
                summary="Emex returned turbo OEM numbers.",
                checked_sources=("https://ru.emexdwc.ae/",),
                details={"oem_numbers": ["06L145874E", "06L145722TX"]},
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            vin_store = VinStore(Path(temp_dir) / "vin_cache.sqlite")
            unresolved = UnresolvedVinStore(
                Path(temp_dir) / "vin_unresolved.sqlite"
            )
            notifier = CapturingNotifier()
            service = VinAgentService(
                enabled=True,
                vin_store=vin_store,
                unresolved_store=unresolved,
                database=TurboDatabase(DATABASE_PATH),
                emex_catalog=emex,
                runner=UnexpectedRunner(),
                notifier=notifier,
                daily_limit=24,
                retry_seconds=604_800,
                notify_not_found=True,
            )
            service.initialize()
            unresolved.record_failure(
                VIN,
                failure_code="no_supported_turbo_numbers",
                observer_delay_seconds=0,
            )
            unresolved.subscribe_result(
                VIN,
                user_id=456,
                chat_id=456,
                username="tester",
                status_message_id=1001,
            )

            result = service.process_once()

            self.assertEqual(result, "candidates_found")
            pending = vin_store.lookup(VIN)
            self.assertIsNotNone(pending)
            self.assertEqual(
                pending.fitments[0].articles,
                ("AC-I085-1e", "RHF5-031BR"),
            )
            self.assertIn("06L145874E", notifier.messages[0])
            self.assertEqual(unresolved.list(), ())
            attempts = unresolved.list_observer_attempts(vin=VIN)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].stage, "emex")
            self.assertEqual(attempts[0].status, "found")


if __name__ == "__main__":
    unittest.main()

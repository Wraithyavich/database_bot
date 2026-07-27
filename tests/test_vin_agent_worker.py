import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vin_agent_worker import CodexRunner, parse_agent_result
from vin_search import VinRecord


VIN = "SALWR2VF0FA000007"


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


if __name__ == "__main__":
    unittest.main()

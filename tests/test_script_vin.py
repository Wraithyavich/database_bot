import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import script
from vin_search import VinFitment, VinRecord, VinSource, VinStore
from vin_unresolved import UnresolvedVinStore


PROJECT_DIR = Path(__file__).resolve().parents[1]


class VinMessageRoutingTests(unittest.TestCase):
    def test_verified_vin_is_routed_before_part_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            message = SimpleNamespace(
                text="VIN: SALLSAAG4AA249280",
                chat_id=123,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=456),
            )

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(
                    script.DATABASE,
                    "search",
                    side_effect=AssertionError("part search must not run for VIN"),
                ),
            ):
                asyncio.run(script.handle_message(update, None))

        reply = message.reply_text.await_args.args[0]
        self.assertIn("Проверенный результат по VIN", reply)
        self.assertIn("GT17-092-1", reply)
        self.assertIn("GT14-009", reply)

    def test_unknown_vin_uses_online_search_and_local_catalog(self) -> None:
        unknown_vin = "SALWR2VF0FA000001"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            message = SimpleNamespace(
                text=unknown_vin,
                chat_id=124,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=987654),
            )
            decoded = VinRecord(
                vin=unknown_vin,
                status="pending",
                make="LAND ROVER",
            )
            preliminary = VinRecord(
                vin=unknown_vin,
                status="pending",
                make="LAND ROVER",
                fitments=(
                    VinFitment(
                        position="Левая",
                        oem_numbers=(),
                        turbo_numbers=("778400-0003",),
                        articles=(),
                        evidence="Номер найден в каталоге.",
                    ),
                ),
                sources=(
                    VinSource(
                        "Parts catalog",
                        "https://example.test/fitment",
                    ),
                ),
                online_search_at="2026-07-26T00:00:00+00:00",
                online_search_provider="Gemini + Google Search",
            )
            searcher = SimpleNamespace(
                enabled=True,
                search=lambda record: preliminary,
            )

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(script, "VIN_ONLINE_SEARCHER", searcher),
                patch.object(
                    script.VIN_DECODER,
                    "decode",
                    return_value=decoded,
                ),
            ):
                asyncio.run(script.handle_message(update, None))

        reply = message.reply_text.await_args.args[0]
        self.assertIn("ПРЕДВАРИТЕЛЬНЫЙ", reply)
        self.assertIn("778400-0003", reply)
        self.assertIn("GT17-092-1", reply)
        self.assertIn("могут быть неточными", reply)

    def test_unknown_vin_is_still_queued_without_api_key(self) -> None:
        unknown_vin = "SALWR2VF0FA000002"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            message = SimpleNamespace(
                text=unknown_vin,
                chat_id=125,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=987655),
            )
            searcher = SimpleNamespace(enabled=False)

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(script, "VIN_ONLINE_SEARCHER", searcher),
                patch.object(
                    script.VIN_DECODER,
                    "decode",
                    return_value=VinRecord(
                        vin=unknown_vin,
                        status="pending",
                    ),
                ),
            ):
                asyncio.run(script.handle_message(update, None))

            queued = store.lookup(unknown_vin)

        self.assertIsNotNone(queued)
        self.assertEqual(queued.status, "pending")
        reply = message.reply_text.await_args.args[0]
        self.assertIn("сохранён в очереди", reply)
        self.assertIn("Онлайн-поиск пока не настроен", reply)

    def test_unprocessed_vin_is_written_to_separate_database(self) -> None:
        unknown_vin = "SALWR2VF0FA000003"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            unresolved_store = UnresolvedVinStore(
                Path(temp_dir) / "vin_unresolved.sqlite"
            )
            unresolved_store.initialize()
            message = SimpleNamespace(
                text=unknown_vin,
                chat_id=126,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=987656),
            )
            searcher = SimpleNamespace(enabled=False)

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(
                    script,
                    "VIN_UNRESOLVED_STORE",
                    unresolved_store,
                ),
                patch.object(script, "VIN_UNRESOLVED_READY", True),
                patch.object(script, "VIN_ONLINE_SEARCHER", searcher),
                patch.object(
                    script.VIN_DECODER,
                    "decode",
                    return_value=VinRecord(
                        vin=unknown_vin,
                        status="pending",
                        make="LAND ROVER",
                    ),
                ),
            ):
                asyncio.run(script.handle_message(update, None))

            unresolved = unresolved_store.list()

        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].vin, unknown_vin)
        self.assertEqual(
            unresolved[0].failure_code,
            "online_search_not_configured",
        )
        self.assertEqual(unresolved[0].make, "LAND ROVER")


if __name__ == "__main__":
    unittest.main()

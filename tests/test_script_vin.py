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
    def setUp(self) -> None:
        self.allowed_ids_patcher = patch.object(
            script,
            "VIN_ALLOWED_USER_IDS",
            frozenset({456, 987654, 987655, 987656, 872931508}),
        )
        self.allowed_ids_patcher.start()

    def tearDown(self) -> None:
        self.allowed_ids_patcher.stop()

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
                patch.object(
                    script.USER_TEXT_RATE_LIMITER,
                    "allow",
                    side_effect=AssertionError(
                        "VIN must bypass the text user rate limit"
                    ),
                ),
                patch.object(
                    script.GLOBAL_TEXT_RATE_LIMITER,
                    "allow",
                    side_effect=AssertionError(
                        "VIN must bypass the global text rate limit"
                    ),
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

    def test_rejects_vin_from_user_outside_allowlist(self) -> None:
        message = SimpleNamespace(
            text="SALLSAAG4AA249280",
            chat_id=127,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=999999),
        )

        with patch.object(
            script,
            "handle_vin_query",
            new=AsyncMock(),
        ) as handle_vin:
            asyncio.run(script.handle_message(update, None))

        handle_vin.assert_not_awaited()
        reply = message.reply_text.await_args.args[0]
        self.assertIn("только допущенным пользователям", reply)

    def test_sends_personal_message_last_on_first_vin_of_moscow_day(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")

            def make_update() -> tuple[SimpleNamespace, SimpleNamespace]:
                message = SimpleNamespace(
                    text="SALLSAAG4AA249280",
                    chat_id=128,
                    reply_text=AsyncMock(),
                )
                update = SimpleNamespace(
                    message=message,
                    effective_user=SimpleNamespace(id=872931508),
                )
                return update, message

            first_update, first_message = make_update()
            second_update, second_message = make_update()
            next_day_update, next_day_message = make_update()

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(
                    script,
                    "DAILY_GREETING_USER_ID",
                    872931508,
                ),
                patch.object(
                    script,
                    "DAILY_GREETING_TEXT",
                    "Daily greeting",
                ),
                patch.object(
                    script,
                    "current_moscow_date",
                    return_value="2026-07-27",
                ),
            ):
                asyncio.run(script.handle_message(first_update, None))
                asyncio.run(script.handle_message(second_update, None))

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(
                    script,
                    "DAILY_GREETING_USER_ID",
                    872931508,
                ),
                patch.object(
                    script,
                    "DAILY_GREETING_TEXT",
                    "Daily greeting",
                ),
                patch.object(
                    script,
                    "current_moscow_date",
                    return_value="2026-07-28",
                ),
            ):
                asyncio.run(script.handle_message(next_day_update, None))

        self.assertEqual(first_message.reply_text.await_count, 2)
        self.assertEqual(
            first_message.reply_text.await_args_list[-1].args[0],
            "Daily greeting",
        )
        self.assertEqual(second_message.reply_text.await_count, 1)
        self.assertEqual(next_day_message.reply_text.await_count, 2)
        self.assertEqual(
            next_day_message.reply_text.await_args_list[-1].args[0],
            "Daily greeting",
        )

    def test_sends_personal_message_last_after_part_number_search(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            message = SimpleNamespace(
                text="787556",
                chat_id=129,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=872931508),
            )

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(
                    script,
                    "DAILY_GREETING_USER_ID",
                    872931508,
                ),
                patch.object(
                    script,
                    "DAILY_GREETING_TEXT",
                    "Daily greeting",
                ),
                patch.object(
                    script,
                    "current_moscow_date",
                    return_value="2026-07-29",
                ),
            ):
                asyncio.run(script.handle_message(update, None))

        self.assertGreaterEqual(message.reply_text.await_count, 2)
        self.assertNotEqual(
            message.reply_text.await_args_list[0].args[0],
            "Daily greeting",
        )
        self.assertEqual(
            message.reply_text.await_args_list[-1].args[0],
            "Daily greeting",
        )

    def test_sends_personal_message_last_after_image_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            message = SimpleNamespace(
                photo=(
                    SimpleNamespace(
                        file_id="oversized-image",
                        file_size=script.MAX_IMAGE_FILE_BYTES + 1,
                    ),
                ),
                document=None,
                chat_id=130,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=872931508),
            )

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(
                    script,
                    "DAILY_GREETING_USER_ID",
                    872931508,
                ),
                patch.object(
                    script,
                    "DAILY_GREETING_TEXT",
                    "Daily greeting",
                ),
                patch.object(
                    script,
                    "current_moscow_date",
                    return_value="2026-07-30",
                ),
            ):
                asyncio.run(script.handle_image(update, None))

        self.assertEqual(message.reply_text.await_count, 2)
        self.assertIn(
            "Изображение слишком большое",
            message.reply_text.await_args_list[0].args[0],
        )
        self.assertEqual(
            message.reply_text.await_args_list[-1].args[0],
            "Daily greeting",
        )


class VinAllowlistParsingTests(unittest.TestCase):
    def test_parses_ids_with_common_separators_and_removes_duplicates(
        self,
    ) -> None:
        self.assertEqual(
            script.parse_allowed_user_ids("123, 456;123\n789"),
            frozenset({123, 456, 789}),
        )

    def test_rejects_invalid_ids(self) -> None:
        with self.assertRaises(ValueError):
            script.parse_allowed_user_ids("123,not-an-id")
        with self.assertRaises(ValueError):
            script.parse_allowed_user_ids("0")

    def test_parses_optional_single_user_id(self) -> None:
        self.assertEqual(script.parse_optional_user_id("872931508"), 872931508)
        self.assertIsNone(script.parse_optional_user_id(""))
        with self.assertRaises(ValueError):
            script.parse_optional_user_id("123,456")


if __name__ == "__main__":
    unittest.main()

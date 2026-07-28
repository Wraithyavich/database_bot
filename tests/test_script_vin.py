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
        self.admin_ids_patcher = patch.object(
            script,
            "VIN_ADMIN_USER_IDS",
            frozenset({1219230738}),
        )
        self.admin_ids_patcher.start()

    def tearDown(self) -> None:
        self.admin_ids_patcher.stop()
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
        self.assertIn("Результат поиска по VIN", reply)
        self.assertIn("778400-0003", reply)
        self.assertIn("GT17-092-1", reply)
        self.assertIn("сверьте номер", reply)
        self.assertIsNotNone(
            message.reply_text.await_args.kwargs["reply_markup"]
        )

    def test_vehicle_identity_without_part_numbers_goes_to_observer(self) -> None:
        unknown_vin = "SALWR2VF0FA000007"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            unresolved_store = UnresolvedVinStore(
                Path(temp_dir) / "vin_unresolved.sqlite"
            )
            unresolved_store.initialize()
            message = SimpleNamespace(
                text=unknown_vin,
                chat_id=127,
                reply_text=AsyncMock(
                    return_value=SimpleNamespace(message_id=6001)
                ),
            )
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=987656),
            )
            decoded = VinRecord(
                vin=unknown_vin,
                status="pending",
                make="LAND ROVER",
            )
            identity_only = VinRecord(
                vin=unknown_vin,
                status="pending",
                make="LAND ROVER",
                model="RANGE ROVER SPORT",
                model_year="2015",
                online_search_at="2026-07-27T00:00:00+00:00",
                online_search_provider="Yandex Search API + Alice AI (Emex)",
                notes="Автомобиль определён, номера турбин не найдены.",
            )
            searcher = SimpleNamespace(
                enabled=True,
                search=lambda record: identity_only,
            )

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
                patch.object(script, "VIN_AGENT_ENABLED", True),
                patch.object(
                    script,
                    "trigger_vin_agent",
                    AsyncMock(),
                ) as trigger,
                patch.object(
                    script.VIN_DECODER,
                    "decode",
                    return_value=decoded,
                ),
            ):
                asyncio.run(script.handle_message(update, None))

            unresolved = unresolved_store.list()
            subscriptions = (
                unresolved_store.pending_result_subscriptions(unknown_vin)
            )

        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].vin, unknown_vin)
        self.assertEqual(
            unresolved[0].failure_code,
            "no_supported_turbo_numbers",
        )
        reply = message.reply_text.await_args.args[0]
        self.assertIn("Ищу информацию по VIN", reply)
        self.assertNotIn("Yandex", reply)
        self.assertNotIn("Codex", reply)
        self.assertEqual(subscriptions[0].user_id, 987656)
        self.assertEqual(subscriptions[0].status_message_id, 6001)
        trigger.assert_awaited_once_with(unknown_vin)

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
        self.assertIn("Номера турбины по VIN не найдены", reply)
        self.assertNotIn("Онлайн-поиск", reply)
        self.assertIsNotNone(
            message.reply_text.await_args.kwargs["reply_markup"]
        )

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

    def test_unprocessed_vin_does_not_notify_admin_automatically(self) -> None:
        unknown_vin = "SALWR2VF0FA000004"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            unresolved_store = UnresolvedVinStore(
                Path(temp_dir) / "vin_unresolved.sqlite"
            )
            unresolved_store.initialize()
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=7001)
                )
            )
            context = SimpleNamespace(bot=bot)

            def make_update() -> SimpleNamespace:
                return SimpleNamespace(
                    message=SimpleNamespace(
                        text=unknown_vin,
                        chat_id=131,
                        reply_text=AsyncMock(),
                    ),
                    effective_user=SimpleNamespace(id=987656),
                )

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(
                    script,
                    "VIN_UNRESOLVED_STORE",
                    unresolved_store,
                ),
                patch.object(script, "VIN_UNRESOLVED_READY", True),
                patch.object(
                    script,
                    "VIN_ONLINE_SEARCHER",
                    SimpleNamespace(enabled=False),
                ),
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
                asyncio.run(script.handle_message(make_update(), context))
                asyncio.run(script.handle_message(make_update(), context))

            self.assertEqual(bot.send_message.await_count, 0)

    def test_manual_review_button_notifies_admin_and_returns_reply(self) -> None:
        vin = "SALWR2VF0FA000008"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            store.save_pending(
                VinRecord(
                    vin=vin,
                    status="pending",
                    make="LAND ROVER",
                    fitments=(
                        VinFitment(
                            position="Турбина",
                            oem_numbers=("LR012345",),
                            turbo_numbers=(),
                            articles=(),
                        ),
                    ),
                )
            )
            unresolved_store = UnresolvedVinStore(
                Path(temp_dir) / "vin_unresolved.sqlite"
            )
            unresolved_store.initialize()
            bot = SimpleNamespace(send_message=AsyncMock())
            query = SimpleNamespace(
                data=f"{script.VIN_MANUAL_CALLBACK_PREFIX}{vin}",
                message=SimpleNamespace(chat_id=132),
                answer=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
            )
            requester_update = SimpleNamespace(
                callback_query=query,
                effective_user=SimpleNamespace(
                    id=987656,
                    username="requester",
                ),
            )
            context = SimpleNamespace(bot=bot)

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(
                    script,
                    "VIN_UNRESOLVED_STORE",
                    unresolved_store,
                ),
                patch.object(script, "VIN_UNRESOLVED_READY", True),
            ):
                asyncio.run(
                    script.handle_vin_manual_review(
                        requester_update,
                        context,
                    )
                )
                admin_notification = bot.send_message.await_args.kwargs["text"]
                admin_message = SimpleNamespace(
                    text="OEM: LR099999",
                    chat_id=1219230738,
                    reply_to_message=SimpleNamespace(
                        message_id=9001,
                        text=admin_notification,
                    ),
                    reply_text=AsyncMock(),
                )
                asyncio.run(
                    script.handle_message(
                        SimpleNamespace(
                            message=admin_message,
                            effective_user=SimpleNamespace(id=1219230738),
                        ),
                        context,
                    )
                )

            verified = store.lookup(vin)
            pending_manual = unresolved_store.pending_manual_requests(vin)

        self.assertIn("Запрошена ручная проверка", admin_notification)
        self.assertIn("987656 (@requester)", admin_notification)
        self.assertNotIn("Ответьте именно", admin_notification)
        self.assertNotIn("KP39-015", admin_notification)
        self.assertEqual(query.answer.await_args.args[0], "Запрос отправлен.")
        self.assertIsNone(
            query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        )
        self.assertEqual(verified.status, "verified")
        self.assertEqual(verified.fitments[0].oem_numbers, ("LR099999",))
        self.assertEqual(pending_manual, ())
        user_delivery = bot.send_message.await_args_list[-1].kwargs
        self.assertEqual(user_delivery["chat_id"], 132)
        self.assertIn("Результат ручной проверки", user_delivery["text"])
        self.assertIn("LR099999", user_delivery["text"])

    def test_admin_reply_saves_verified_vin_and_clears_queue(self) -> None:
        unknown_vin = "SALWR2VF0FA000005"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            store.record_request(
                unknown_vin,
                decoded=VinRecord(
                    vin=unknown_vin,
                    status="pending",
                    make="LAND ROVER",
                ),
            )
            unresolved_store = UnresolvedVinStore(
                Path(temp_dir) / "vin_unresolved.sqlite"
            )
            unresolved_store.initialize()
            unresolved_store.record_failure(
                unknown_vin,
                failure_code="no_supported_turbo_numbers",
            )
            unresolved_store.claim_notification(unknown_vin, 1219230738)
            unresolved_store.mark_notification_sent(
                unknown_vin,
                1219230738,
                7002,
            )
            message = SimpleNamespace(
                text=(
                    "Левая: KP39-015\n"
                    "Правая: KP39-020\n"
                    "Источник: https://emex.ru/example"
                ),
                chat_id=1219230738,
                reply_to_message=SimpleNamespace(
                    message_id=7002,
                    text=f"VIN: {unknown_vin}",
                ),
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=1219230738),
            )

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(
                    script,
                    "VIN_UNRESOLVED_STORE",
                    unresolved_store,
                ),
                patch.object(script, "VIN_UNRESOLVED_READY", True),
            ):
                asyncio.run(script.handle_message(update, None))

            stored = store.lookup(unknown_vin)
            unresolved = unresolved_store.list()

        self.assertEqual(stored.status, "verified")
        self.assertEqual(
            [fitment.turbo_numbers for fitment in stored.fitments],
            [("KP39-015",), ("KP39-020",)],
        )
        self.assertEqual(unresolved, ())
        reply = message.reply_text.await_args.args[0]
        self.assertIn("Результат сохранён и отправлен пользователю", reply)

    def test_observer_finds_candidates_and_returns_them_to_user(self) -> None:
        unknown_vin = "SALWR2VF0FA000006"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = VinStore(Path(temp_dir) / "vin.sqlite")
            store.initialize(seed_path=PROJECT_DIR / "vin_verified.json")
            store.record_request(
                unknown_vin,
                decoded=VinRecord(
                    vin=unknown_vin,
                    status="pending",
                    make="LAND ROVER",
                ),
            )
            unresolved_store = UnresolvedVinStore(
                Path(temp_dir) / "vin_unresolved.sqlite"
            )
            unresolved_store.initialize()
            unresolved_store.record_failure(
                unknown_vin,
                failure_code="no_supported_turbo_numbers",
                observer_delay_seconds=0,
            )
            unresolved_store.subscribe_result(
                unknown_vin,
                user_id=987656,
                chat_id=132,
                username="requester",
                status_message_id=8001,
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
                        evidence="Найдено в Emex.",
                    ),
                ),
                online_search_at="2026-07-27T00:00:00+00:00",
                online_search_provider="Yandex + Emex",
            )
            searcher = SimpleNamespace(
                enabled=True,
                search=lambda record: preliminary,
            )
            bot = SimpleNamespace(
                edit_message_text=AsyncMock(),
                send_message=AsyncMock(),
            )

            with (
                patch.object(script, "VIN_STORE", store),
                patch.object(script, "VIN_SEARCH_READY", True),
                patch.object(
                    script,
                    "VIN_UNRESOLVED_STORE",
                    unresolved_store,
                ),
                patch.object(script, "VIN_UNRESOLVED_READY", True),
                patch.object(script, "VIN_OBSERVER_ENABLED", True),
                patch.object(script, "VIN_OBSERVER_DAILY_LIMIT", 5),
                patch.object(script, "VIN_ONLINE_SEARCHER", searcher),
            ):
                result = asyncio.run(script.run_vin_observer_once(bot))

            stored = store.lookup(unknown_vin)
            unresolved = unresolved_store.list()
            subscriptions = (
                unresolved_store.pending_result_subscriptions(unknown_vin)
            )

        self.assertEqual(result, "candidates_found")
        self.assertEqual(
            stored.fitments[0].turbo_numbers,
            ("778400-0003",),
        )
        self.assertEqual(stored.status, "pending")
        self.assertEqual(unresolved, ())
        self.assertEqual(subscriptions, ())
        self.assertEqual(bot.send_message.await_count, 0)
        self.assertIn(
            "778400-0003",
            bot.edit_message_text.await_args.kwargs["text"],
        )
        self.assertIsNotNone(
            bot.edit_message_text.await_args.kwargs["reply_markup"]
        )

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


class ProductionVinProviderTests(unittest.TestCase):
    def test_uses_only_yandex_emex_searcher(self) -> None:
        self.assertEqual(
            script.VIN_ONLINE_SEARCHER.searchers,
            (script.YANDEX_VIN_SEARCHER,),
        )
        self.assertIn("Emex", script.YANDEX_VIN_SEARCHER.provider_name)


if __name__ == "__main__":
    unittest.main()

import unittest

from vin_admin_reply import (
    VinAdminReplyError,
    confirm_admin_vin_record,
    is_admin_confirmation,
    is_admin_reply_candidate,
    parse_admin_vin_reply,
)
from vin_search import VinFitment, VinRecord


VIN = "SALLSAAG4AA249280"


class VinAdminReplyParserTests(unittest.TestCase):
    def test_parses_left_and_right_turbines(self) -> None:
        record = parse_admin_vin_reply(
            "Левая: KP39-015\n"
            "Правая: KP39-020\n"
            "OEM левая: LR012345, LR012346\n"
            "Источник: https://emex.ru/example\n"
            "Комментарий: проверено вручную",
            vin=VIN,
            base_record=VinRecord(
                vin=VIN,
                status="pending",
                make="LAND ROVER",
            ),
        )

        self.assertEqual(record.status, "verified")
        self.assertEqual(record.make, "LAND ROVER")
        self.assertEqual(
            record.fitments[0].turbo_numbers,
            ("KP39-015",),
        )
        self.assertEqual(
            record.fitments[0].oem_numbers,
            ("LR012345", "LR012346"),
        )
        self.assertEqual(
            record.fitments[1].turbo_numbers,
            ("KP39-020",),
        )
        self.assertEqual(record.sources[-1].url, "https://emex.ru/example")
        self.assertTrue(record.verified_at)

    def test_detects_direct_admin_update_but_not_plain_vin(self) -> None:
        self.assertTrue(
            is_admin_reply_candidate(
                f"VIN: {VIN}\nЛевая: KP39-015"
            )
        )
        self.assertFalse(is_admin_reply_candidate(VIN))

    def test_requires_at_least_one_part_number(self) -> None:
        with self.assertRaises(VinAdminReplyError):
            parse_admin_vin_reply(
                "Комментарий: ничего не найдено",
                vin=VIN,
            )

    def test_rejects_invalid_source(self) -> None:
        with self.assertRaises(VinAdminReplyError):
            parse_admin_vin_reply(
                "Турбина: KP39-015\nИсточник: emex.ru/example",
                vin=VIN,
            )

    def test_confirms_preliminary_observer_result(self) -> None:
        preliminary = VinRecord(
            vin=VIN,
            status="pending",
            fitments=(
                VinFitment(
                    position="Левая",
                    oem_numbers=(),
                    turbo_numbers=("KP39-015",),
                    articles=(),
                ),
            ),
        )

        verified = confirm_admin_vin_record(preliminary)

        self.assertTrue(is_admin_confirmation("Подтверждаю"))
        self.assertEqual(verified.status, "verified")
        self.assertTrue(verified.verified_at)


if __name__ == "__main__":
    unittest.main()

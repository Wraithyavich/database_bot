import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import script
from vin_search import VinStore


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


if __name__ == "__main__":
    unittest.main()

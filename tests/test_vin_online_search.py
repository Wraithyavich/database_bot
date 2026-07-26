import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from turbo_database import TurboDatabase
from vin_online_search import (
    GeminiVinSearcher,
    VinOnlineSearchError,
    attach_catalog_articles,
)
from vin_search import VinRecord


PROJECT_DIR = Path(__file__).resolve().parents[1]
TURBO_DATABASE_PATH = PROJECT_DIR / "turbo_search.sqlite"
KNOWN_VIN = "SALLSAAG4AA249280"


def gemini_response(*, grounded: bool = True) -> bytes:
    result = {
        "vehicle": {
            "make": "Land Rover",
            "model": "Range Rover Sport",
            "model_year": "2010",
            "engine": "3.0 TDV6",
            "power_kw": "180",
        },
        "fitments": [
            {
                "position": "Левая",
                "oem_numbers": ["LR013202"],
                "turbo_numbers": ["778400-0003"],
                "evidence": "Номер указан в каталоге для этой комплектации.",
            }
        ],
        "summary": "Требуется проверка по установленной турбине.",
    }
    candidate = {
        "content": {
            "parts": [
                {
                    "text": json.dumps(result, ensure_ascii=False),
                }
            ]
        },
    }
    if grounded:
        candidate["groundingMetadata"] = {
            "groundingChunks": [
                {
                    "web": {
                        "uri": "https://example.test/fitment",
                        "title": "Parts catalog",
                    }
                }
            ]
        }
    return json.dumps({"candidates": [candidate]}).encode("utf-8")


class GeminiVinSearcherTests(unittest.TestCase):
    def test_requires_api_key(self) -> None:
        searcher = GeminiVinSearcher("")

        with self.assertRaisesRegex(
            VinOnlineSearchError,
            "not configured",
        ):
            searcher.search(VinRecord(vin=KNOWN_VIN, status="pending"))

    def test_parses_grounded_candidates_without_exposing_key_in_url(self) -> None:
        captured = {}

        def fake_urlopen(request, *, timeout):
            captured["url"] = request.full_url
            captured["key"] = request.get_header("X-goog-api-key")
            captured["timeout"] = timeout
            return io.BytesIO(gemini_response())

        with patch(
            "vin_online_search.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            record = GeminiVinSearcher(
                "secret-test-key",
                timeout=12,
            ).search(VinRecord(vin=KNOWN_VIN, status="pending"))

        self.assertNotIn("secret-test-key", captured["url"])
        self.assertEqual(captured["key"], "secret-test-key")
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(record.make, "Land Rover")
        self.assertEqual(record.fitments[0].oem_numbers, ("LR013202",))
        self.assertEqual(record.fitments[0].turbo_numbers, ("778400-0003",))
        self.assertEqual(record.fitments[0].articles, ())
        self.assertEqual(record.sources[0].url, "https://example.test/fitment")
        self.assertTrue(record.online_search_at)

    def test_discards_part_numbers_without_grounding_sources(self) -> None:
        with patch(
            "vin_online_search.urllib.request.urlopen",
            return_value=io.BytesIO(gemini_response(grounded=False)),
        ):
            record = GeminiVinSearcher("test-key").search(
                VinRecord(vin=KNOWN_VIN, status="pending")
            )

        self.assertEqual(record.fitments, ())
        self.assertEqual(record.sources, ())
        self.assertTrue(record.online_search_at)

    def test_matches_online_numbers_to_local_catalog_exactly(self) -> None:
        with patch(
            "vin_online_search.urllib.request.urlopen",
            return_value=io.BytesIO(gemini_response()),
        ):
            online_record = GeminiVinSearcher("test-key").search(
                VinRecord(vin=KNOWN_VIN, status="pending")
            )

        enriched = attach_catalog_articles(
            online_record,
            TurboDatabase(TURBO_DATABASE_PATH),
        )

        self.assertEqual(enriched.fitments[0].articles, ("GT17-092-1",))


if __name__ == "__main__":
    unittest.main()

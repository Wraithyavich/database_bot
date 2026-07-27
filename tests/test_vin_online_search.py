import base64
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from turbo_database import TurboDatabase
from vin_online_search import (
    GeminiVinSearcher,
    VinOnlineSearchError,
    VinOnlineSearcherRouter,
    YandexVinSearcher,
    _filter_emex_hits,
    _select_yandex_result_sources,
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


def yandex_web_response(
    *,
    grounded: bool = True,
    identity: bool = True,
) -> bytes:
    if grounded:
        url = (
            "https://emex.ru/vin/SALLSAAG4AA249280"
            if identity
            else "https://emex.ru/generic-turbo"
        )
        title = (
            "SALLSAAG4AA249280 parts catalog"
            if identity
            else "Generic turbo catalog"
        )
        xml = f"""
        <yandexsearch>
          <response>
            <results>
              <grouping>
                <group>
                  <doc>
                    <url>{url}</url>
                    <title>{title}</title>
                    <passages>
                      <passage>
                        LAND ROVER Range Rover Sport 2010 3.0 TDV6,
                        OEM LR013202, Turbo P/N 778400-0003.
                      </passage>
                    </passages>
                  </doc>
                </group>
              </grouping>
            </results>
          </response>
        </yandexsearch>
        """
    else:
        xml = "<yandexsearch><response><results /></response></yandexsearch>"
    return json.dumps(
        {
            "rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")
        }
    ).encode("utf-8")


def yandex_chat_response(*, hallucinated: bool = False) -> bytes:
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
                "turbo_numbers": [
                    "999999-9999" if hallucinated else "778400-0003"
                ],
                "evidence": "Номер найден в каталоге.",
            }
        ],
        "source_urls": [
            "https://emex.ru/vin/SALLSAAG4AA249280"
        ],
        "summary": "Требуется проверка по установленной турбине.",
    }
    document = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(result, ensure_ascii=False),
                    "role": "assistant",
                }
            }
        ]
    }
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


class YandexVinSearcherTests(unittest.TestCase):
    def test_requires_api_key_and_folder_id(self) -> None:
        for searcher in (
            YandexVinSearcher("", "folder-id"),
            YandexVinSearcher("test-key", ""),
        ):
            with self.subTest(searcher=searcher):
                with self.assertRaisesRegex(
                    VinOnlineSearchError,
                    "not configured",
                ):
                    searcher.search(
                        VinRecord(vin=KNOWN_VIN, status="pending")
                    )

    def test_parses_grounded_response_without_exposing_key_in_url(self) -> None:
        captured = []

        def fake_urlopen(request, *, timeout):
            captured.append(
                {
                    "url": request.full_url,
                    "authorization": request.get_header("Authorization"),
                    "body": json.loads(request.data),
                    "timeout": timeout,
                }
            )
            if request.full_url.endswith("/v2/web/search"):
                return io.BytesIO(yandex_web_response())
            return io.BytesIO(yandex_chat_response())

        with patch(
            "vin_online_search.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            record = YandexVinSearcher(
                "secret-test-key",
                "folder-id",
                timeout=14,
            ).search(VinRecord(vin=KNOWN_VIN, status="pending"))

        self.assertEqual(len(captured), 4)
        self.assertTrue(
            all("secret-test-key" not in item["url"] for item in captured)
        )
        self.assertTrue(
            all(
                item["authorization"] == "Api-Key secret-test-key"
                for item in captured
            )
        )
        search_requests = [
            item
            for item in captured
            if item["url"].endswith("/v2/web/search")
        ]
        self.assertEqual(len(search_requests), 3)
        self.assertEqual(
            search_requests[0]["body"]["folderId"],
            "folder-id",
        )
        self.assertEqual(
            search_requests[0]["body"]["query"]["searchType"],
            "SEARCH_TYPE_RU",
        )
        self.assertIn(
            KNOWN_VIN,
            search_requests[0]["body"]["query"]["queryText"],
        )
        self.assertTrue(
            all(
                "site:emex.ru"
                in item["body"]["query"]["queryText"]
                for item in search_requests
            )
        )
        self.assertTrue(
            all(
                "exist.ru"
                not in item["body"]["query"]["queryText"].lower()
                for item in search_requests
            )
        )
        self.assertTrue(all(item["timeout"] == 14 for item in captured))
        self.assertEqual(record.make, "Land Rover")
        self.assertEqual(record.fitments[0].oem_numbers, ("LR013202",))
        self.assertEqual(record.fitments[0].turbo_numbers, ("778400-0003",))
        self.assertEqual(
            record.sources[0].url,
            "https://emex.ru/vin/SALLSAAG4AA249280",
        )
        self.assertEqual(
            record.online_search_provider,
            "Yandex Search API + Alice AI (Emex)",
        )

    def test_discards_part_numbers_without_sources(self) -> None:
        with patch(
            "vin_online_search.urllib.request.urlopen",
            side_effect=lambda request, timeout: io.BytesIO(
                yandex_web_response(grounded=False),
            ),
        ):
            record = YandexVinSearcher(
                "test-key",
                "folder-id",
            ).search(VinRecord(vin=KNOWN_VIN, status="pending"))

        self.assertEqual(record.fitments, ())
        self.assertEqual(record.sources, ())
        self.assertTrue(record.online_search_at)

    def test_does_not_analyze_results_without_vin_identity_source(self) -> None:
        requests = []

        def fake_urlopen(request, *, timeout):
            requests.append(request.full_url)
            if not request.full_url.endswith("/v2/web/search"):
                raise AssertionError("YandexGPT must not be called")
            return io.BytesIO(yandex_web_response(identity=False))

        with patch(
            "vin_online_search.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            record = YandexVinSearcher(
                "test-key",
                "folder-id",
            ).search(VinRecord(vin=KNOWN_VIN, status="pending"))

        self.assertEqual(len(requests), 3)
        self.assertEqual(record.fitments, ())
        self.assertEqual(record.sources, ())
        self.assertIn("модельный префикс", record.notes)

    def test_discards_numbers_missing_from_search_snippets(self) -> None:
        def fake_urlopen(request, *, timeout):
            if request.full_url.endswith("/v2/web/search"):
                return io.BytesIO(yandex_web_response())
            return io.BytesIO(yandex_chat_response(hallucinated=True))

        with patch(
            "vin_online_search.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            record = YandexVinSearcher(
                "test-key",
                "folder-id",
            ).search(VinRecord(vin=KNOWN_VIN, status="pending"))

        self.assertEqual(record.fitments[0].oem_numbers, ("LR013202",))
        self.assertEqual(record.fitments[0].turbo_numbers, ())

    def test_empty_result_keeps_only_vin_specific_sources(self) -> None:
        sources = _select_yandex_result_sources(
            {"fitments": []},
            vin=KNOWN_VIN,
            hits=[
                {
                    "title": f"Decode {KNOWN_VIN}",
                    "url": "https://emex.ru/exact-vin",
                    "text": "Vehicle details",
                },
                {
                    "title": "Generic turbo catalog",
                    "url": "https://emex.ru/generic",
                    "text": "Unrelated vehicle",
                },
                {
                    "title": f"Images for {KNOWN_VIN[:11]}",
                    "url": (
                        "https://yandex.ru/images/search?"
                        f"text={KNOWN_VIN[:11]}"
                    ),
                    "text": "",
                },
            ],
        )

        self.assertEqual(
            tuple(source.url for source in sources),
            ("https://emex.ru/exact-vin",),
        )

    def test_accepts_only_emex_domains(self) -> None:
        hits = [
            {"url": "https://emex.ru/search", "title": "", "text": ""},
            {
                "url": "https://kazan.emex.ru/search",
                "title": "",
                "text": "",
            },
            {
                "url": "https://exist.ru/search",
                "title": "",
                "text": "",
            },
            {
                "url": "https://notemex.ru/search",
                "title": "",
                "text": "",
            },
        ]

        self.assertEqual(
            [hit["url"] for hit in _filter_emex_hits(hits)],
            [
                "https://emex.ru/search",
                "https://kazan.emex.ru/search",
            ],
        )


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


class VinOnlineSearcherRouterTests(unittest.TestCase):
    def test_prefers_yandex_and_does_not_call_fallback_after_success(self) -> None:
        expected = VinRecord(vin=KNOWN_VIN, status="pending")
        yandex = SimpleNamespace(
            enabled=True,
            provider_name="Yandex",
            search=lambda record: expected,
        )

        def unexpected_search(record):
            raise AssertionError("fallback must not be called")

        gemini = SimpleNamespace(
            enabled=True,
            provider_name="Gemini",
            search=unexpected_search,
        )
        router = VinOnlineSearcherRouter(yandex, gemini)

        self.assertIs(
            router.search(VinRecord(vin=KNOWN_VIN, status="pending")),
            expected,
        )
        self.assertEqual(router.description, "Yandex → Gemini")

    def test_uses_fallback_after_provider_error(self) -> None:
        expected = VinRecord(vin=KNOWN_VIN, status="pending")

        def failed_search(record):
            raise VinOnlineSearchError("temporary error")

        yandex = SimpleNamespace(
            enabled=True,
            provider_name="Yandex",
            search=failed_search,
        )
        gemini = SimpleNamespace(
            enabled=True,
            provider_name="Gemini",
            search=lambda record: expected,
        )

        self.assertIs(
            VinOnlineSearcherRouter(yandex, gemini).search(
                VinRecord(vin=KNOWN_VIN, status="pending")
            ),
            expected,
        )


if __name__ == "__main__":
    unittest.main()

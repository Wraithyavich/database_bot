import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from image_search import (
    OcrCandidate,
    OcrLine,
    RapidOcrRecognizer,
    extract_number_candidates,
    ocr_candidate_variants,
    search_image_candidates,
)
from turbo_database import TurboDatabase, normalize_number


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = PROJECT_DIR / "turbo_search.sqlite"


class CandidateExtractionTests(unittest.TestCase):
    def test_reports_engine_initialization_error(self) -> None:
        def broken_engine():
            raise RuntimeError("backend is unavailable")

        fake_module = SimpleNamespace(RapidOCR=broken_engine)
        with patch.dict("sys.modules", {"rapidocr": fake_module}):
            with self.assertRaisesRegex(
                RuntimeError,
                "backend is unavailable",
            ):
                RapidOcrRecognizer().check_available()

    def test_recognizer_reads_current_rapidocr_output(self) -> None:
        class FakeEngine:
            def __call__(self, image_path: str):
                return SimpleNamespace(
                    txts=["Part No: AL-0095", "blurred"],
                    scores=[0.98, 0.10],
                )

        fake_module = SimpleNamespace(RapidOCR=lambda: FakeEngine())
        with patch.dict("sys.modules", {"rapidocr": fake_module}):
            candidates = RapidOcrRecognizer().recognize("test-image.jpg")

        normalized = {candidate.normalized for candidate in candidates}
        self.assertIn("AL0095", normalized)
        self.assertNotIn("BLURRED", normalized)

    def test_extracts_common_number_formats(self) -> None:
        candidates = extract_number_candidates(
            [
                OcrLine("Part No: AL-0095", 0.98),
                OcrLine("OEM 17201-52010", 0.96),
                OcrLine("JRONE 1000-010-006", 0.94),
            ]
        )
        normalized = {candidate.normalized for candidate in candidates}
        self.assertIn("AL0095", normalized)
        self.assertIn("1720152010", normalized)
        self.assertIn("1000010006", normalized)

    def test_reassembles_number_split_by_spaces(self) -> None:
        candidates = extract_number_candidates(
            [
                OcrLine("AL 0095", 0.90),
                OcrLine("17201 52010", 0.90),
                OcrLine("26H.145 702 R", 0.90),
                OcrLine("A 656 090 03 80", 0.90),
            ]
        )
        normalized = {candidate.normalized for candidate in candidates}
        self.assertIn("AL0095", normalized)
        self.assertIn("1720152010", normalized)
        self.assertIn("26H145702R", normalized)
        self.assertIn("A6560900380", normalized)

    def test_ignores_words_without_digits(self) -> None:
        candidates = extract_number_candidates(
            [OcrLine("GARRETT TURBOCHARGER", 0.99)]
        )
        self.assertEqual(candidates, ())

    def test_corrects_common_ocr_digit_confusions(self) -> None:
        self.assertEqual(
            ocr_candidate_variants("AL-OO95"),
            ("AL-OO95", "AL-0095"),
        )
        self.assertIn(
            "06H145702R",
            ocr_candidate_variants("26H-145-702-R"),
        )


class ImageDatabaseSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = TurboDatabase(DATABASE_PATH)

    def test_exact_image_candidate(self) -> None:
        candidate = OcrCandidate(
            value="AL-0095",
            normalized=normalize_number("AL-0095"),
            confidence=0.97,
        )
        matches = search_image_candidates(self.database, [candidate])
        articles = {
            article.article
            for image_match in matches
            for article in image_match.result.matches
        }
        self.assertIn("R2S-070-HB", articles)

    def test_corrected_image_candidate(self) -> None:
        candidate = OcrCandidate(
            value="AL-OO95",
            normalized=normalize_number("AL-OO95"),
            confidence=0.91,
        )
        matches = search_image_candidates(self.database, [candidate])
        self.assertTrue(matches)
        self.assertEqual(matches[0].searched_value, "AL-0095")

    def test_partial_image_candidate(self) -> None:
        candidate = OcrCandidate(
            value="787556",
            normalized="787556",
            confidence=0.95,
        )
        matches = search_image_candidates(self.database, [candidate])
        articles = {
            article.article
            for image_match in matches
            for article in image_match.result.matches
        }
        self.assertIn("AC-G150", articles)
        self.assertFalse(matches[0].result.exact)

    def test_rejects_noisy_mass_partial_match(self) -> None:
        candidate = OcrCandidate(
            value="145-702",
            normalized="145702",
            confidence=0.95,
        )
        self.assertEqual(
            search_image_candidates(self.database, [candidate]),
            (),
        )

    def test_vag_number_with_ocr_prefix_error(self) -> None:
        candidate = OcrCandidate(
            value="26H-145-702-R",
            normalized="26H145702R",
            confidence=0.90,
        )
        matches = search_image_candidates(self.database, [candidate])
        articles = {
            article.article
            for image_match in matches
            for article in image_match.result.matches
        }
        self.assertIn("Turbo-I161S", articles)
        self.assertEqual(matches[0].searched_value, "06H145702R")

    def test_mercedes_plate_candidates(self) -> None:
        candidates = extract_number_candidates(
            [
                OcrLine("A 656 090 03 80", 0.94),
                OcrLine("AL0083-Q02-10009700269", 0.99),
            ]
        )
        matches = search_image_candidates(self.database, candidates)
        matched_values = {match.searched_value for match in matches}
        articles = {
            article.article
            for image_match in matches
            for article in image_match.result.matches
        }
        self.assertIn("AL0083", matched_values)
        self.assertIn("A-656-090-03-80", matched_values)
        self.assertIn("R2S-070-HB", articles)
        self.assertIn("R2S-070-LBR", articles)


if __name__ == "__main__":
    unittest.main()

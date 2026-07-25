import tempfile
import unittest
from pathlib import Path

from PIL import Image

from request_limits import (
    ImageRejectedError,
    SlidingWindowRateLimiter,
    validate_image_file,
)


class SlidingWindowRateLimiterTests(unittest.TestCase):
    def test_rejects_requests_over_limit_and_reports_retry(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10)

        self.assertTrue(limiter.allow("user", now=100).allowed)
        self.assertTrue(limiter.allow("user", now=101).allowed)
        rejected = limiter.allow("user", now=102)

        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.retry_after, 8)

    def test_accepts_request_after_window(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=1, window_seconds=10)

        self.assertTrue(limiter.allow("user", now=100).allowed)
        self.assertTrue(limiter.allow("user", now=110).allowed)

    def test_limits_keys_in_memory(self) -> None:
        limiter = SlidingWindowRateLimiter(
            limit=1,
            window_seconds=10,
            max_keys=2,
        )

        limiter.allow("first", now=100)
        limiter.allow("second", now=101)
        limiter.allow("third", now=102)

        self.assertTrue(limiter.allow("first", now=103).allowed)


class ImageValidationTests(unittest.TestCase):
    def test_accepts_safe_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.png"
            Image.new("RGB", (100, 50), "white").save(path)

            dimensions = validate_image_file(
                path,
                max_bytes=1_000_000,
                max_pixels=10_000,
            )

            self.assertEqual(dimensions, (100, 50))

    def test_rejects_excessive_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.png"
            Image.new("RGB", (100, 100), "white").save(path)

            with self.assertRaisesRegex(ImageRejectedError, "resolution"):
                validate_image_file(
                    path,
                    max_bytes=1_000_000,
                    max_pixels=9_999,
                )

    def test_rejects_non_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.jpg"
            path.write_text("not an image", encoding="utf-8")

            with self.assertRaisesRegex(ImageRejectedError, "safe image"):
                validate_image_file(
                    path,
                    max_bytes=1_000_000,
                    max_pixels=10_000,
                )


if __name__ == "__main__":
    unittest.main()

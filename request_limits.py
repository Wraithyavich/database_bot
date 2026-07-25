from __future__ import annotations

import math
import threading
import time
import warnings
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Hashable

from PIL import Image, UnidentifiedImageError


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        max_keys: int = 10_000,
    ):
        if limit < 1:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_keys < 1:
            raise ValueError("max_keys must be positive")

        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._events: dict[Hashable, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(
        self,
        key: Hashable,
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        timestamp = time.monotonic() if now is None else now
        cutoff = timestamp - self.window_seconds

        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self.max_keys:
                    oldest_key = min(
                        self._events,
                        key=lambda existing_key: self._events[existing_key][-1],
                    )
                    del self._events[oldest_key]
                events = deque()
                self._events[key] = events

            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= self.limit:
                retry_after = max(
                    1,
                    math.ceil(events[0] + self.window_seconds - timestamp),
                )
                return RateLimitDecision(False, retry_after)

            events.append(timestamp)
            return RateLimitDecision(True)


class ImageRejectedError(ValueError):
    pass


def validate_image_file(
    path: str | Path,
    *,
    max_bytes: int,
    max_pixels: int,
) -> tuple[int, int]:
    image_path = Path(path)
    try:
        file_size = image_path.stat().st_size
    except OSError as error:
        raise ImageRejectedError(f"Cannot read image: {error}") from error

    if file_size > max_bytes:
        raise ImageRejectedError("Image file is too large")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(image_path) as image:
                width, height = image.size
                if width < 1 or height < 1:
                    raise ImageRejectedError("Invalid image dimensions")
                if width * height > max_pixels:
                    raise ImageRejectedError("Image resolution is too large")
                image.verify()
    except ImageRejectedError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
    ) as error:
        raise ImageRejectedError("File is not a safe image") from error

    return width, height

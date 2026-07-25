from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from turbo_database import SearchResult, TurboDatabase, normalize_number


MAX_OCR_CANDIDATES = 40
MAX_MATCHED_CANDIDATES = 5
MIN_PARTIAL_OCR_LENGTH = 6
MAX_PARTIAL_IMAGE_MATCHES = 25

_TOKEN_RE = re.compile(
    r"[A-ZА-Я0-9]+(?:[-./][A-ZА-Я0-9]+)+|[A-ZА-Я0-9]{4,}",
    re.IGNORECASE,
)
_COMPONENT_RE = re.compile(r"[A-ZА-Я0-9]+", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"([-./\s]+)")
_OCR_DIGIT_FIXES = str.maketrans(
    {
        "O": "0",
        "Q": "0",
        "I": "1",
        "L": "1",
        "Z": "2",
        "S": "5",
        "B": "8",
    }
)
_LABEL_WORDS = {
    "ART",
    "ARTICLE",
    "MODEL",
    "NO",
    "NUM",
    "NUMBER",
    "OEM",
    "PART",
    "PN",
    "SERIAL",
    "SN",
    "TYPE",
}


class OcrUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float


@dataclass(frozen=True)
class OcrCandidate:
    value: str
    normalized: str
    confidence: float


@dataclass(frozen=True)
class ImageSearchMatch:
    recognized_value: str
    searched_value: str
    confidence: float
    result: SearchResult


class RapidOcrRecognizer:
    """Локальный OCR с ленивой инициализацией ONNX-моделей."""

    def __init__(self, *, minimum_confidence: float = 0.30):
        self.minimum_confidence = minimum_confidence
        self._engine = None
        self._lock = threading.Lock()

    def _get_engine(self):
        if self._engine is not None:
            return self._engine

        with self._lock:
            if self._engine is None:
                try:
                    from rapidocr import RapidOCR

                    self._engine = RapidOCR()
                except Exception as error:
                    raise OcrUnavailableError(
                        "Не удалось загрузить RapidOCR "
                        f"({type(error).__name__}: {error})"
                    ) from error
        return self._engine

    def check_available(self) -> None:
        self._get_engine()

    def recognize(self, image_path: str | Path) -> tuple[OcrCandidate, ...]:
        engine = self._get_engine()
        try:
            with self._lock:
                outputs = [engine(str(image_path))]

                try:
                    import cv2

                    image = cv2.imread(str(image_path))
                    if image is not None:
                        grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                        enhanced = cv2.createCLAHE(
                            clipLimit=3.0,
                            tileGridSize=(8, 8),
                        ).apply(grayscale)
                        enhanced = cv2.cvtColor(
                            enhanced,
                            cv2.COLOR_GRAY2BGR,
                        )
                        outputs.append(engine(enhanced))
                except (ImportError, AttributeError):
                    pass
        except Exception as error:
            raise RuntimeError(f"Ошибка OCR: {error}") from error

        lines_by_text: dict[str, OcrLine] = {}
        for output in outputs:
            texts = getattr(output, "txts", None)
            scores = getattr(output, "scores", None)
            if texts is None:
                continue

            for index, raw_text in enumerate(texts):
                text = str(raw_text).strip()
                if not text:
                    continue
                confidence = 1.0
                if scores is not None and index < len(scores):
                    confidence = float(scores[index])
                if confidence < self.minimum_confidence:
                    continue
                current = lines_by_text.get(text)
                if current is None or confidence > current.confidence:
                    lines_by_text[text] = OcrLine(
                        text=text,
                        confidence=confidence,
                    )
        return extract_number_candidates(lines_by_text.values())


def extract_number_candidates(
    lines: Iterable[OcrLine],
    *,
    limit: int = MAX_OCR_CANDIDATES,
) -> tuple[OcrCandidate, ...]:
    candidates: dict[str, OcrCandidate] = {}

    def add_candidate(value: str, confidence: float) -> None:
        cleaned = value.strip(" \t\r\n-./")
        normalized = normalize_number(cleaned)
        if (
            len(normalized) < 4
            or len(normalized) > 32
            or not any(character.isdigit() for character in normalized)
        ):
            return
        current = candidates.get(normalized)
        candidate = OcrCandidate(
            value=cleaned,
            normalized=normalized,
            confidence=confidence,
        )
        if current is None or confidence > current.confidence:
            candidates[normalized] = candidate

    for line in lines:
        text = line.text.upper()
        for match in _TOKEN_RE.finditer(text):
            add_candidate(match.group(0), line.confidence)

        components = _COMPONENT_RE.findall(text)
        for component in components:
            add_candidate(component, line.confidence)

        for window_size in (2, 3, 4, 5):
            for start in range(0, len(components) - window_size + 1):
                window = components[start : start + window_size]
                alpha_only = [
                    component
                    for component in window
                    if not any(character.isdigit() for character in component)
                ]
                if len(alpha_only) > 1:
                    continue
                if alpha_only:
                    alpha_component = alpha_only[0]
                    is_prefix = window[0] == alpha_component
                    is_suffix = window[-1] == alpha_component
                    if is_prefix:
                        if alpha_component in _LABEL_WORDS or len(alpha_component) > 4:
                            continue
                        numeric_components = window[1:]
                    elif is_suffix:
                        if len(alpha_component) > 3:
                            continue
                        numeric_components = window[:-1]
                    else:
                        continue
                else:
                    numeric_components = window
                if not all(
                    any(character.isdigit() for character in component)
                    for component in numeric_components
                ):
                    continue
                add_candidate("-".join(window), line.confidence * 0.98)

    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            -item.confidence,
            -sum(character.isdigit() for character in item.normalized),
            -len(item.normalized),
            item.value,
        ),
    )
    return tuple(ranked[:limit])


def ocr_candidate_variants(value: str) -> tuple[str, ...]:
    parts = _SEPARATOR_RE.split(value.upper())
    corrected_parts: list[str] = []
    changed = False

    for part in parts:
        if not part or _SEPARATOR_RE.fullmatch(part):
            corrected_parts.append(part)
            continue
        if any(character.isdigit() for character in part):
            corrected = part.translate(_OCR_DIGIT_FIXES)
            corrected_parts.append(corrected)
            changed = changed or corrected != part
        else:
            corrected_parts.append(part)

    corrected_value = "".join(corrected_parts)
    variants = [value]
    if changed and normalize_number(corrected_value) != normalize_number(value):
        variants.append(corrected_value)

    for base_value in tuple(variants):
        normalized = normalize_number(base_value)
        if (
            len(normalized) >= 9
            and len(normalized) >= 3
            and normalized[0] in {"2", "8", "O", "Q"}
            and normalized[1].isdigit()
            and normalized[2].isalpha()
        ):
            variants.append(f"0{normalized[1:]}")

    return tuple(dict.fromkeys(variants))


def search_image_candidates(
    database: TurboDatabase,
    candidates: Iterable[OcrCandidate],
    *,
    result_limit: int = 100,
) -> tuple[ImageSearchMatch, ...]:
    candidate_list = tuple(candidates)
    exact_matches: list[ImageSearchMatch] = []
    searched_normalized: set[str] = set()

    for candidate in candidate_list:
        for variant in ocr_candidate_variants(candidate.value):
            normalized_variant = normalize_number(variant)
            if normalized_variant in searched_normalized:
                continue
            searched_normalized.add(normalized_variant)
            result = database.search(
                variant,
                limit=result_limit,
                allow_partial=False,
                allow_fallback=False,
            )
            if result.matches:
                exact_matches.append(
                    ImageSearchMatch(
                        recognized_value=candidate.value,
                        searched_value=variant,
                        confidence=candidate.confidence,
                        result=result,
                    )
                )
                break
        if len(exact_matches) >= MAX_MATCHED_CANDIDATES:
            break

    if exact_matches:
        return tuple(exact_matches)

    for candidate in candidate_list:
        if len(candidate.normalized) < MIN_PARTIAL_OCR_LENGTH:
            continue
        for variant in ocr_candidate_variants(candidate.value):
            result = database.search(variant, limit=result_limit)
            if (
                result.matches
                and not result.truncated
                and len(result.matches) <= MAX_PARTIAL_IMAGE_MATCHES
            ):
                return (
                    ImageSearchMatch(
                        recognized_value=candidate.value,
                        searched_value=variant,
                        confidence=candidate.confidence,
                        result=result,
                    ),
                )
    return ()

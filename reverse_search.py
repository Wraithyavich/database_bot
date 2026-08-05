from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


NUMBER_KINDS = frozenset(
    {
        "turbo_pn",
        "vehicle_oem",
        "component_pn",
        "external_cross",
        "unknown",
    }
)

EXPLICIT_ARTICLE_EQUIVALENCES = (
    ("B3-000-C1B", "B3-000TB"),
    ("GT42-019-C", "GT42-019B"),
    ("GT42-020-2B", "GT42-020T"),
    ("GT47-019-C", "GT47-019T"),
    ("HX-179-C", "HX-179B"),
    ("HX5-000-C", "HX5-000B"),
    ("K31-004-C", "K31-004B"),
    ("S3-063-1B", "S3-063TB"),
    ("S3-093-C", "S3-093TB"),
    ("S3-093-C1B", "S3-093TB"),
)

_DASH_TRANSLATION = str.maketrans(
    {
        "֊": "-",
        "־": "-",
        "᐀": "-",
        "᠆": "-",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "⸗": "-",
        "⸚": "-",
        "⸺": "-",
        "⸻": "-",
        "⹀": "-",
        "〜": "-",
        "〰": "-",
        "゠": "-",
        "︱": "-",
        "︲": "-",
        "﹘": "-",
        "﹣": "-",
        "－": "-",
    }
)
_AROUND_DASH = re.compile(r"\s*-\s*")
_WHITESPACE = re.compile(r"\s+")
_NON_ASCII_ALPHANUMERIC = re.compile(r"[^A-Z0-9]")
_NATURAL_PART = re.compile(r"(\d+)")


def normalize_article(value: str) -> str:
    """Normalize presentation differences without removing catalog suffixes."""
    normalized = value.strip().upper().translate(_DASH_TRANSLATION)
    normalized = _AROUND_DASH.sub("-", normalized)
    return _WHITESPACE.sub(" ", normalized)


def compact_article(value: str) -> str:
    """Compact key used only as a last, uniqueness-checked fallback."""
    return _NON_ASCII_ALPHANUMERIC.sub("", normalize_article(value))


def natural_sort_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PART.split(value)
        if part
    )


@dataclass(frozen=True)
class NumberEvidence:
    part_id: int
    article_norm: str
    source_id: int
    source_catalog: str
    number: str
    number_norm: str
    number_type: str
    match_role: str
    source_page: int | None
    raw_context: str
    catalog_order: int


@dataclass(frozen=True)
class ClassifiedEvidence:
    evidence: NumberEvidence
    number_kind: str
    source_column: str
    confidence: int
    priority: int


@dataclass(frozen=True)
class ClassificationContext:
    trusted_turbo_by_article: dict[str, frozenset[str]]
    trusted_vehicle_by_article: dict[str, frozenset[str]]
    catalog_has_trusted_turbo: frozenset[tuple[int, int]]
    first_catalog_number: frozenset[tuple[int, int, str]]
    bearing_pre_application: frozenset[tuple[int, str]]


def _is_article_row(evidence: NumberEvidence) -> bool:
    return evidence.match_role == "article" or evidence.number_type == "E&E P/N"


def classify_number_evidence(
    evidence: NumberEvidence,
    context: ClassificationContext,
) -> ClassifiedEvidence | None:
    """Classify by the meaning of a source/column, never by number shape."""
    if _is_article_row(evidence):
        return None

    source_id = evidence.source_id
    number_type = evidence.number_type
    key = (evidence.part_id, source_id, evidence.number_norm)
    trusted_turbo = context.trusted_turbo_by_article.get(
        evidence.article_norm, frozenset()
    )
    trusted_vehicle = context.trusted_vehicle_by_article.get(
        evidence.article_norm, frozenset()
    )

    kind = "unknown"
    column = "unclassified"
    confidence = 45
    priority = 10

    if source_id == 1:
        column = f"legacy:{number_type}"
        if number_type == "Turbo P/N":
            kind, confidence, priority = "turbo_pn", 70, 50
        elif number_type in {"Vehicle OE No / OEM", "OEM / Vehicle OE No"}:
            kind, confidence, priority = "vehicle_oem", 75, 65
        elif number_type == "JRONE":
            kind, confidence, priority = "external_cross", 85, 75
    elif source_id == 2:
        kind, column, confidence, priority = (
            "vehicle_oem",
            "OEM / Vehicle OE No",
            100,
            100,
        )
    elif source_id == 3:
        column = "JRONE" if number_type == "JRONE" else "Turbo P/N"
        if number_type == "JRONE":
            kind, confidence, priority = "external_cross", 100, 100
        else:
            kind, confidence, priority = "turbo_pn", 90, 85
    elif source_id == 4:
        kind, column, confidence, priority = "turbo_pn", "Turbo P/N", 95, 90
    elif source_id == 5:
        kind, column, confidence, priority = (
            "external_cross",
            "JRONE",
            100,
            100,
        )
    elif source_id == 7:
        if number_type == "Turbo P/N":
            kind, column, confidence, priority = (
                "turbo_pn",
                "OEM P/N (catalog meaning: Turbo P/N)",
                100,
                100,
            )
        elif number_type == "Number":
            kind, column, confidence, priority = (
                "vehicle_oem",
                "Vehicle OE No",
                95,
                95,
            )
        else:
            column = "model/application token"
    elif source_id == 8:
        if number_type == "Turbo P/N":
            if evidence.number_norm not in trusted_turbo:
                kind, column, confidence, priority = (
                    "component_pn",
                    "CHRA P/N",
                    100,
                    105,
                )
            else:
                kind, column, confidence, priority = (
                    "turbo_pn",
                    "Turbo P/N",
                    95,
                    95,
                )
        elif number_type == "Number":
            kind, column, confidence, priority = (
                "vehicle_oem",
                "Vehicle OE No",
                95,
                95,
            )
        else:
            column = "model/application token"
    elif source_id == 9:
        if number_type == "Turbo P/N":
            if evidence.number_norm not in trusted_turbo:
                kind, column, confidence, priority = (
                    "component_pn",
                    "Actuator OEM P/N",
                    100,
                    105,
                )
            else:
                kind, column, confidence, priority = (
                    "turbo_pn",
                    "Turbo P/N",
                    95,
                    95,
                )
        elif evidence.number_norm in trusted_vehicle:
            kind, column, confidence, priority = (
                "vehicle_oem",
                "Vehicle OE No (corroborated)",
                90,
                90,
            )
        else:
            column = "model/application token"
    elif source_id == 10:
        if number_type == "Turbo P/N":
            kind, column, confidence, priority = (
                "turbo_pn",
                "Turbo P/N",
                95,
                95,
            )
        elif evidence.number_norm in trusted_vehicle:
            kind, column, confidence, priority = (
                "vehicle_oem",
                "Vehicle OE No (corroborated)",
                90,
                90,
            )
        else:
            column = "unclassified geometry field"
    elif source_id in {11, 12, 13, 14}:
        if number_type == "Turbo P/N":
            if evidence.number_norm in trusted_turbo:
                kind, column, confidence, priority = (
                    "turbo_pn",
                    "Turbo P/N (corroborated)",
                    95,
                    95,
                )
            elif (evidence.part_id, source_id) in context.catalog_has_trusted_turbo:
                kind, column, confidence, priority = (
                    "component_pn",
                    "Part OEM P/N",
                    90,
                    90,
                )
            else:
                kind, column, confidence, priority = (
                    "turbo_pn",
                    "Turbo P/N (catalog field)",
                    75,
                    70,
                )
        elif evidence.number_norm in trusted_vehicle:
            kind, column, confidence, priority = (
                "vehicle_oem",
                "Vehicle OE No (corroborated)",
                90,
                90,
            )
        else:
            column = "model/dimension token"
    elif source_id == 15:
        if number_type == "Turbo P/N":
            kind, column, confidence, priority = (
                "turbo_pn",
                "Application / Turbo P/N",
                100,
                100,
            )
        else:
            column = "description/dimension token"
    elif source_id == 16:
        if number_type == "Turbo P/N":
            kind, column, confidence, priority = (
                "turbo_pn",
                "Turbo P/N",
                90,
                85,
            )
        elif number_type == "Number" and (
            evidence.part_id,
            evidence.number_norm,
        ) in context.bearing_pre_application:
            if key in context.first_catalog_number:
                kind, column, confidence, priority = (
                    "component_pn",
                    "Bearing housing OEM P/N",
                    95,
                    95,
                )
            else:
                kind, column, confidence, priority = (
                    "turbo_pn",
                    "Turbo P/N",
                    85,
                    80,
                )
        elif evidence.number_norm in trusted_vehicle:
            kind, column, confidence, priority = (
                "vehicle_oem",
                "Vehicle OE No (corroborated)",
                90,
                90,
            )
        else:
            column = "model/dimension token"

    return ClassifiedEvidence(
        evidence=evidence,
        number_kind=kind,
        source_column=column,
        confidence=confidence,
        priority=priority,
    )


def resolve_evidence(
    evidences: Iterable[ClassifiedEvidence],
) -> tuple[ClassifiedEvidence, tuple[str, ...]]:
    """Resolve one article/number relation and expose equal-priority conflicts."""
    values = tuple(evidences)
    if not values:
        raise ValueError("At least one evidence row is required")

    known = tuple(value for value in values if value.number_kind != "unknown")
    candidates = known or values
    best_priority = max(value.priority for value in candidates)
    best = tuple(value for value in candidates if value.priority == best_priority)
    best_kinds = tuple(sorted({value.number_kind for value in best}))

    if len(best_kinds) > 1:
        chosen = min(best, key=lambda value: value.evidence.catalog_order)
        return (
            ClassifiedEvidence(
                evidence=chosen.evidence,
                number_kind="unknown",
                source_column="conflicting semantic sources",
                confidence=0,
                priority=best_priority,
            ),
            best_kinds,
        )

    chosen_kind = best_kinds[0]
    chosen = min(
        (value for value in best if value.number_kind == chosen_kind),
        key=lambda value: value.evidence.catalog_order,
    )
    all_known_kinds = tuple(sorted({value.number_kind for value in known}))
    return chosen, all_known_kinds if len(all_known_kinds) > 1 else ()

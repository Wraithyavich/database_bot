from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from typing import Any

from turbo_database import normalize_number
from vin_search import (
    VinFitment,
    VinRecord,
    VinSource,
    extract_vin,
    utc_now,
)


EMEX_BASE_URL = "https://ru.emexdwc.ae/"
EMEX_HOST = "ru.emexdwc.ae"
MAX_RESPONSE_BYTES = 2_000_000
MAX_VEHICLE_CANDIDATES = 3
MAX_TURBO_UNITS = 3
PART_NUMBER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9 ._+/()-]{2,39}$", re.I)
TURBO_GROUP_PATTERN = re.compile(r"\b145-\d{3}\b")
VEHICLE_HEADING_PATTERN = re.compile(
    r"Автомобиль\s+(?P<make>.+?)\s+-\s+(?P<model>.+)",
    re.I,
)
TURBO_TERMS = (
    "турбонагнетател",
    "турбокомпресс",
    "turbocharger",
    "turbolader",
)


class EmexCatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmexLookupReport:
    record: VinRecord
    status: str
    summary: str
    checked_sources: tuple[str, ...]
    details: dict[str, Any]


@dataclass(frozen=True)
class _Anchor:
    href: str
    text: str


@dataclass(frozen=True)
class _Cell:
    name: str
    text: str
    hrefs: tuple[str, ...]


class _CatalogHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_Anchor] = []
        self.headings: list[str] = []
        self.rows: list[tuple[_Cell, ...]] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._heading_tag: str | None = None
        self._heading_text: list[str] = []
        self._row: list[_Cell] | None = None
        self._cell_name: str | None = None
        self._cell_text: list[str] = []
        self._cell_hrefs: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "a":
            self._anchor_href = attributes.get("href") or ""
            self._anchor_text = []
            if self._cell_name is not None and self._anchor_href:
                self._cell_hrefs.append(self._anchor_href)
        elif tag in {"h1", "h2"}:
            self._heading_tag = tag
            self._heading_text = []
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_name = attributes.get("name") or ""
            self._cell_text = []
            self._cell_hrefs = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href is not None:
            self.anchors.append(
                _Anchor(
                    href=self._anchor_href,
                    text=_clean_text(" ".join(self._anchor_text)),
                )
            )
            self._anchor_href = None
            self._anchor_text = []
        elif tag == self._heading_tag:
            heading = _clean_text(" ".join(self._heading_text))
            if heading:
                self.headings.append(heading)
            self._heading_tag = None
            self._heading_text = []
        elif tag in {"td", "th"} and self._cell_name is not None:
            if self._row is not None:
                self._row.append(
                    _Cell(
                        name=self._cell_name,
                        text=_clean_text(" ".join(self._cell_text)),
                        hrefs=tuple(self._cell_hrefs),
                    )
                )
            self._cell_name = None
            self._cell_text = []
            self._cell_hrefs = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(tuple(self._row))
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._heading_tag is not None:
            self._heading_text.append(data)
        if self._cell_name is not None:
            self._cell_text.append(data)


class EmexVinCatalog:
    """Read-only VIN lookup through the public Emex DWC catalogue."""

    def __init__(
        self,
        *,
        timeout: float = 20,
        opener: Any | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_response_bytes < 1024:
            raise ValueError("max_response_bytes is too small")
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener()
        self.max_response_bytes = max_response_bytes

    def search(self, base_record: VinRecord) -> EmexLookupReport:
        vin = extract_vin(base_record.vin)
        if vin is None or vin != base_record.vin:
            raise ValueError("Invalid VIN")

        vehicles_url = self._url(
            "Vehicles.aspx",
            {
                "ft": "findByVIN",
                "c": "",
                "ssd": "",
                "vin": vin,
            },
        )
        checked = [vehicles_url]
        vehicles_page = self._fetch(vehicles_url)
        vehicle_links = self._matching_links(
            vehicles_page,
            expected_page="Vehicle.aspx",
            vin=vin,
        )[:MAX_VEHICLE_CANDIDATES]
        if not vehicle_links:
            return self._not_found(
                base_record,
                checked,
                "Emex не вернул каталог автомобиля для этого VIN.",
                {"vehicle_candidates": 0},
            )

        fitments: list[VinFitment] = []
        sources: list[VinSource] = []
        vehicles: list[dict[str, Any]] = []
        resolved_make = base_record.make
        resolved_model = base_record.model

        for vehicle_link in vehicle_links:
            vehicle_url = self._absolute_catalog_url(vehicle_link.href)
            checked.append(_audit_url(vehicle_url))
            vehicle_page = self._fetch(vehicle_url)
            make, model = self._vehicle_identity(vehicle_page)
            resolved_make = resolved_make or make
            resolved_model = resolved_model or model or vehicle_link.text

            unit_links = [
                anchor
                for anchor in self._matching_links(
                    vehicle_page,
                    expected_page="Unit.aspx",
                    vin=vin,
                )
                if TURBO_GROUP_PATTERN.search(anchor.text)
                and _contains_turbo_term(anchor.text)
            ][:MAX_TURBO_UNITS]
            vehicle_detail: dict[str, Any] = {
                "vehicle": " ".join(
                    part for part in (make, model or vehicle_link.text) if part
                ),
                "turbo_units": [],
            }

            for unit_link in unit_links:
                unit_url = self._absolute_catalog_url(unit_link.href)
                checked.append(_audit_url(unit_url))
                unit_page = self._fetch(unit_url)
                group = (
                    TURBO_GROUP_PATTERN.search(unit_link.text).group(0)
                    if TURBO_GROUP_PATTERN.search(unit_link.text)
                    else "145"
                )
                numbers, number_links = self._turbo_numbers(unit_page, vin=vin)
                vehicle_detail["turbo_units"].append(
                    {
                        "group": group,
                        "numbers": list(numbers),
                    }
                )
                if not numbers:
                    continue

                fitments.append(
                    VinFitment(
                        position=f"Турбокомпрессор, группа {group}",
                        oem_numbers=numbers,
                        turbo_numbers=(),
                        articles=(),
                        evidence=(
                            "VIN-фильтрованный каталог Emex DWC: "
                            f"узел {group} «Турбонагнетатель»."
                        ),
                    )
                )
                for number, link in number_links.items():
                    sources.append(
                        VinSource(
                            label=f"Emex DWC — OEM {number} по VIN",
                            url=self._absolute_catalog_url(link),
                        )
                    )
            vehicles.append(vehicle_detail)

        fitments = _deduplicate_fitments(fitments)
        sources = _deduplicate_sources(sources)
        if not fitments:
            return self._not_found(
                replace(
                    base_record,
                    make=resolved_make,
                    model=resolved_model,
                ),
                checked,
                "Emex нашёл автомобиль, но не вернул OEM в узле турбины.",
                {
                    "vehicle_candidates": len(vehicle_links),
                    "vehicles": vehicles,
                },
            )

        record = replace(
            base_record,
            status="pending",
            make=resolved_make,
            model=resolved_model,
            fitments=tuple(fitments),
            sources=tuple(
                _deduplicate_sources([*base_record.sources, *sources])[:10]
            ),
            notes=(
                "Предварительный результат VIN-каталога Emex; "
                "перед заказом необходимо сверить номер на шильдике."
            ),
            online_search_at=utc_now(),
            online_search_provider="Emex DWC VIN-каталог",
        )
        numbers = sorted(
            {
                number
                for fitment in fitments
                for number in fitment.oem_numbers
            }
        )
        return EmexLookupReport(
            record=record,
            status="found",
            summary="Emex вернул OEM турбокомпрессора: " + ", ".join(numbers),
            checked_sources=tuple(dict.fromkeys(checked)),
            details={
                "vehicle_candidates": len(vehicle_links),
                "vehicles": vehicles,
                "oem_numbers": numbers,
            },
        )

    def _not_found(
        self,
        record: VinRecord,
        checked: list[str],
        summary: str,
        details: dict[str, Any],
    ) -> EmexLookupReport:
        return EmexLookupReport(
            record=replace(
                record,
                fitments=(),
                online_search_at=utc_now(),
                online_search_provider="Emex DWC VIN-каталог",
                notes=summary,
            ),
            status="not_found",
            summary=summary,
            checked_sources=tuple(dict.fromkeys(checked)),
            details=details,
        )

    def _fetch(self, url: str) -> _CatalogHtmlParser:
        self._assert_catalog_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru,en;q=0.8",
                "User-Agent": "database-bot-vin-observer/1.0",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                final_url = response.geturl()
                self._assert_catalog_url(final_url)
                payload = response.read(self.max_response_bytes + 1)
                charset = response.headers.get_content_charset() or "utf-8"
        except (OSError, urllib.error.URLError, ValueError) as error:
            raise EmexCatalogError(f"Emex request failed: {error}") from error
        if len(payload) > self.max_response_bytes:
            raise EmexCatalogError("Emex response is too large")
        try:
            html = payload.decode(charset, errors="replace")
        except LookupError as error:
            raise EmexCatalogError("Emex returned an unknown charset") from error
        parser = _CatalogHtmlParser()
        parser.feed(html)
        parser.close()
        return parser

    @staticmethod
    def _matching_links(
        page: _CatalogHtmlParser,
        *,
        expected_page: str,
        vin: str,
    ) -> list[_Anchor]:
        result: list[_Anchor] = []
        seen: set[str] = set()
        for anchor in page.anchors:
            parsed = urllib.parse.urlsplit(anchor.href)
            if (
                (parsed.scheme and parsed.scheme != "https")
                or (parsed.hostname and parsed.hostname != EMEX_HOST)
            ):
                continue
            if parsed.path.rsplit("/", 1)[-1].lower() != expected_page.lower():
                continue
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("vin", [""])[0].upper() != vin:
                continue
            if anchor.href in seen:
                continue
            seen.add(anchor.href)
            result.append(anchor)
        return result

    @staticmethod
    def _vehicle_identity(page: _CatalogHtmlParser) -> tuple[str, str]:
        for heading in page.headings:
            match = VEHICLE_HEADING_PATTERN.fullmatch(heading)
            if match is not None:
                return (
                    _clean_text(match.group("make")),
                    _clean_text(match.group("model")),
                )
        return "", ""

    @staticmethod
    def _turbo_numbers(
        page: _CatalogHtmlParser,
        *,
        vin: str,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        numbers: list[str] = []
        links: dict[str, str] = {}
        seen: set[str] = set()
        for row in page.rows:
            names = {
                cell.name: cell
                for cell in row
                if cell.name
            }
            description = names.get("c_name")
            if description is None or not _is_turbo_component(description.text):
                continue

            number_cell = names.get("c_oem")
            raw_number = number_cell.text if number_cell is not None else ""
            hrefs = [
                href
                for cell in row
                for href in cell.hrefs
            ]
            matching_href = ""
            for href in hrefs:
                parsed = urllib.parse.urlsplit(href)
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("vin", [""])[0].upper() != vin:
                    continue
                candidate = query.get("n", [""])[0].strip()
                if candidate and not raw_number:
                    raw_number = candidate
                matching_href = href
                break

            cleaned = _part_number(raw_number, vin=vin)
            normalized = normalize_number(cleaned)
            if not cleaned or normalized in seen:
                continue
            seen.add(normalized)
            numbers.append(cleaned)
            if matching_href:
                links[cleaned] = matching_href
        return tuple(numbers), links

    @staticmethod
    def _url(path: str, query: dict[str, str]) -> str:
        return urllib.parse.urljoin(
            EMEX_BASE_URL,
            path + "?" + urllib.parse.urlencode(query),
        )

    @staticmethod
    def _absolute_catalog_url(href: str) -> str:
        url = urllib.parse.urljoin(EMEX_BASE_URL, href)
        EmexVinCatalog._assert_catalog_url(url)
        return url

    @staticmethod
    def _assert_catalog_url(url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != EMEX_HOST
            or parsed.username
            or parsed.password
        ):
            raise EmexCatalogError("Refusing a non-Emex catalogue URL")


def _part_number(value: str, *, vin: str) -> str:
    cleaned = _clean_text(value).upper()
    normalized = normalize_number(cleaned)
    if (
        not PART_NUMBER_PATTERN.fullmatch(cleaned)
        or not any(character.isdigit() for character in cleaned)
        or not 4 <= len(normalized) <= 32
        or normalized == vin
    ):
        return ""
    return cleaned


def _contains_turbo_term(value: str) -> bool:
    lowered = value.casefold()
    return any(term in lowered for term in TURBO_TERMS)


def _is_turbo_component(value: str) -> bool:
    lowered = _clean_text(value).casefold()
    return any(lowered.startswith(term) for term in TURBO_TERMS)


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _audit_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if key.casefold() != "ssd"
    ]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            "",
        )
    )


def _deduplicate_fitments(
    fitments: list[VinFitment],
) -> list[VinFitment]:
    result: list[VinFitment] = []
    seen: set[tuple[str, ...]] = set()
    for fitment in fitments:
        key = tuple(sorted(normalize_number(number) for number in fitment.oem_numbers))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(fitment)
    return result


def _deduplicate_sources(sources: list[VinSource]) -> list[VinSource]:
    result: list[VinSource] = []
    seen: set[str] = set()
    for source in sources:
        if source.url in seen:
            continue
        seen.add(source.url)
        result.append(source)
    return result

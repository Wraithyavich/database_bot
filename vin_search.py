from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
VIN_IN_TEXT_PATTERN = re.compile(r"(?<![A-Z0-9])([A-HJ-NPR-Z0-9]{17})(?![A-Z0-9])")
NHTSA_DECODE_URL = (
    "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"
)
MAX_DECODER_RESPONSE_BYTES = 1_000_000


class VinDecoderError(RuntimeError):
    pass


@dataclass(frozen=True)
class VinFitment:
    position: str
    oem_numbers: tuple[str, ...]
    turbo_numbers: tuple[str, ...]
    articles: tuple[str, ...]
    evidence: str = ""


@dataclass(frozen=True)
class VinSource:
    label: str
    url: str


@dataclass(frozen=True)
class VinRecord:
    vin: str
    status: str
    make: str = ""
    model: str = ""
    model_year: str = ""
    engine: str = ""
    power_kw: str = ""
    fitments: tuple[VinFitment, ...] = ()
    sources: tuple[VinSource, ...] = ()
    notes: str = ""
    verified_at: str = ""
    online_search_at: str = ""
    online_search_provider: str = ""


@dataclass(frozen=True)
class VinStoreStats:
    verified: int
    pending: int
    requests: int


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def extract_vin(value: str) -> str | None:
    text = value.strip().upper()
    direct = re.sub(r"[^A-Z0-9]", "", text)
    if VIN_PATTERN.fullmatch(direct) and any(char.isalpha() for char in direct):
        return direct

    match = VIN_IN_TEXT_PATTERN.search(text)
    if match is None:
        return None
    candidate = match.group(1)
    return candidate if any(char.isalpha() for char in candidate) else None


def format_verified_vin(record: VinRecord) -> list[str]:
    vehicle_parts = [part for part in (record.make, record.model) if part]
    vehicle = " ".join(vehicle_parts) or "не определён"
    details = " / ".join(
        part
        for part in (
            record.model_year,
            record.engine,
            f"{record.power_kw} кВт" if record.power_kw else "",
        )
        if part
    )

    lines = [
        "✅ Проверенный результат по VIN",
        f"VIN: {record.vin}",
        f"Автомобиль: {vehicle}",
    ]
    if details:
        lines.append(f"Год / двигатель: {details}")

    lines.extend(["", "Турбокомпрессоры и картриджи:"])
    for fitment in record.fitments:
        lines.append(f"• {fitment.position}")
        if fitment.oem_numbers:
            lines.append(f"  OEM: {', '.join(fitment.oem_numbers)}")
        if fitment.turbo_numbers:
            lines.append(f"  Turbo P/N: {', '.join(fitment.turbo_numbers)}")
        if fitment.articles:
            lines.append(f"  Наши картриджи: {', '.join(fitment.articles)}")

    if record.sources:
        lines.extend(["", "Источники:"])
        for index, source in enumerate(record.sources, start=1):
            lines.append(f"{index}. {source.label}: {source.url}")

    lines.extend(
        [
            "",
            "⚠️ Перед заказом рекомендуется сверить номер на установленной турбине.",
        ]
    )
    return lines


def format_pending_vin(record: VinRecord, *, decoder_failed: bool = False) -> list[str]:
    lines = [
        "🔎 VIN пока отсутствует в проверенной базе.",
        f"VIN: {record.vin}",
    ]

    vehicle_parts = [part for part in (record.make, record.model) if part]
    if vehicle_parts:
        lines.append(f"Автомобиль: {' '.join(vehicle_parts)}")

    details = " / ".join(
        part
        for part in (
            record.model_year,
            record.engine,
            f"{record.power_kw} кВт" if record.power_kw else "",
        )
        if part
    )
    if details:
        lines.append(f"Год / двигатель: {details}")

    lines.extend(
        [
            "",
            "Запрос сохранён в очереди на проверку.",
            "Бот не выдаёт случайные номера турбин без подтверждённых источников.",
        ]
    )
    if decoder_failed:
        lines.append(
            "Базовый VIN-декодер временно недоступен, но заявка сохранена."
        )
    return lines


def format_online_vin(record: VinRecord) -> list[str]:
    lines = [
        "⚠️ ПРЕДВАРИТЕЛЬНЫЙ результат поиска в интернете",
        f"VIN: {record.vin}",
    ]

    vehicle_parts = [part for part in (record.make, record.model) if part]
    if vehicle_parts:
        lines.append(f"Автомобиль: {' '.join(vehicle_parts)}")

    details = " / ".join(
        part
        for part in (
            record.model_year,
            record.engine,
            f"{record.power_kw} кВт" if record.power_kw else "",
        )
        if part
    )
    if details:
        lines.append(f"Год / двигатель: {details}")
    if record.online_search_provider:
        lines.append(f"Поиск: {record.online_search_provider}")

    if record.fitments:
        lines.extend(["", "Возможные номера турбокомпрессоров:"])
        for fitment in record.fitments:
            lines.append(f"• {fitment.position}")
            if fitment.oem_numbers:
                lines.append(f"  OEM: {', '.join(fitment.oem_numbers)}")
            if fitment.turbo_numbers:
                lines.append(f"  Turbo P/N: {', '.join(fitment.turbo_numbers)}")
            if fitment.articles:
                lines.append(
                    "  Возможные картриджи из нашей базы: "
                    f"{', '.join(fitment.articles)}"
                )
            if fitment.evidence:
                lines.append(f"  Основание: {fitment.evidence}")

        if not any(fitment.articles for fitment in record.fitments):
            lines.extend(
                [
                    "",
                    "В нашей базе точных совпадений по найденным номерам нет.",
                ]
            )
    else:
        lines.extend(
            [
                "",
                "Онлайн-поиск выполнен, но достаточно обоснованные номера "
                "турбин не найдены.",
            ]
        )

    if record.notes:
        lines.extend(["", f"Комментарий поиска: {record.notes}"])

    if record.sources:
        lines.extend(["", "Источники:"])
        for index, source in enumerate(record.sources, start=1):
            lines.append(f"{index}. {source.label}: {source.url}")

    lines.extend(
        [
            "",
            "⚠️ ВАЖНО: результат сформирован автоматически по информации "
            "из интернета. Номера могут быть неточными или относиться к другой "
            "комплектации.",
            "Перед заказом обязательно перепроверьте номер на шильдике "
            "установленной турбины или в официальном каталоге по VIN.",
            "VIN сохранён в очереди на ручную проверку.",
        ]
    )
    return lines


class NhtsaVinDecoder:
    def __init__(self, *, timeout: float = 10):
        self.timeout = timeout

    def decode(self, vin: str) -> VinRecord:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")

        url = NHTSA_DECODE_URL.format(vin=urllib.parse.quote(normalized, safe=""))
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "database-bot/1.0",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(MAX_DECODER_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError) as error:
            raise VinDecoderError(f"VIN decoder request failed: {error}") from error

        if len(payload) > MAX_DECODER_RESPONSE_BYTES:
            raise VinDecoderError("VIN decoder response is too large")

        try:
            document = json.loads(payload)
            result = document["Results"][0]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise VinDecoderError("VIN decoder returned an invalid response") from error

        return VinRecord(
            vin=normalized,
            status="pending",
            make=_clean_api_value(result.get("Make")),
            model=_clean_api_value(result.get("Model")),
            model_year=_clean_api_value(result.get("ModelYear")),
            engine=_first_nonempty(
                result.get("EngineModel"),
                result.get("EngineConfiguration"),
            ),
            power_kw=_clean_api_value(result.get("EngineKW")),
            sources=(
                VinSource(
                    label="NHTSA vPIC — базовое декодирование VIN",
                    url="https://vpic.nhtsa.dot.gov/decoder/",
                ),
            ),
        )


class VinStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self, *, seed_path: str | Path | None = None) -> VinStoreStats:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS vin_records(
                    vin TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('verified', 'pending')),
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vin_requests(
                    vin TEXT PRIMARY KEY REFERENCES vin_records(vin)
                        ON DELETE CASCADE,
                    request_count INTEGER NOT NULL,
                    first_requested_at TEXT NOT NULL,
                    last_requested_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_vin_records_status
                    ON vin_records(status);
                """
            )
            connection.commit()

        if seed_path is not None:
            self.import_verified_seed(seed_path)
        return self.stats()

    def import_verified_seed(self, seed_path: str | Path) -> None:
        path = Path(seed_path)
        document = json.loads(path.read_text(encoding="utf-8"))
        records = document.get("records")
        if not isinstance(records, list):
            raise ValueError("VIN seed must contain a records list")

        now = utc_now()
        with closing(self._connect()) as connection:
            for raw_record in records:
                record = _record_from_dict(raw_record)
                if record.status != "verified":
                    raise ValueError("VIN seed may contain only verified records")
                payload = json.dumps(
                    _record_to_dict(record),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                connection.execute(
                    """
                    INSERT INTO vin_records(
                        vin, status, payload_json, created_at, updated_at
                    )
                    VALUES (?, 'verified', ?, ?, ?)
                    ON CONFLICT(vin) DO UPDATE SET
                        status = 'verified',
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (record.vin, payload, now, now),
                )
            connection.commit()

    def lookup(self, vin: str) -> VinRecord | None:
        normalized = extract_vin(vin)
        if normalized is None:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM vin_records WHERE vin = ?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return _record_from_dict(json.loads(row["payload_json"]))

    def record_request(
        self,
        vin: str,
        *,
        decoded: VinRecord | None = None,
    ) -> VinRecord:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        pending = decoded or VinRecord(vin=normalized, status="pending")
        if pending.vin != normalized:
            raise ValueError("Decoded VIN does not match request")

        now = utc_now()
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT status, payload_json FROM vin_records WHERE vin = ?",
                (normalized,),
            ).fetchone()

            if existing is None:
                payload = json.dumps(
                    _record_to_dict(pending),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                connection.execute(
                    """
                    INSERT INTO vin_records(
                        vin, status, payload_json, created_at, updated_at
                    )
                    VALUES (?, 'pending', ?, ?, ?)
                    """,
                    (normalized, payload, now, now),
                )
                record = pending
            elif existing["status"] == "verified":
                record = _record_from_dict(json.loads(existing["payload_json"]))
            else:
                payload = json.dumps(
                    _record_to_dict(pending),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                connection.execute(
                    """
                    UPDATE vin_records
                    SET payload_json = ?, updated_at = ?
                    WHERE vin = ? AND status = 'pending'
                    """,
                    (payload, now, normalized),
                )
                record = pending

            connection.execute(
                """
                INSERT INTO vin_requests(
                    vin, request_count, first_requested_at, last_requested_at
                )
                VALUES (?, 1, ?, ?)
                ON CONFLICT(vin) DO UPDATE SET
                    request_count = vin_requests.request_count + 1,
                    last_requested_at = excluded.last_requested_at
                """,
                (normalized, now, now),
            )
            connection.commit()
        return record

    def pending(self, *, limit: int = 100) -> tuple[VinRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT r.payload_json
                FROM vin_records AS r
                LEFT JOIN vin_requests AS q ON q.vin = r.vin
                WHERE r.status = 'pending'
                ORDER BY q.last_requested_at DESC, r.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            _record_from_dict(json.loads(row["payload_json"])) for row in rows
        )

    def stats(self) -> VinStoreStats:
        with closing(self._connect()) as connection:
            counts = {
                row["status"]: row["amount"]
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS amount
                    FROM vin_records
                    GROUP BY status
                    """
                )
            }
            requests = connection.execute(
                "SELECT COALESCE(SUM(request_count), 0) FROM vin_requests"
            ).fetchone()[0]
        return VinStoreStats(
            verified=counts.get("verified", 0),
            pending=counts.get("pending", 0),
            requests=requests,
        )


def _clean_api_value(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _first_nonempty(*values: Any) -> str:
    for value in values:
        cleaned = _clean_api_value(value)
        if cleaned:
            return cleaned
    return ""


def _record_to_dict(record: VinRecord) -> dict[str, Any]:
    return {
        "vin": record.vin,
        "status": record.status,
        "make": record.make,
        "model": record.model,
        "model_year": record.model_year,
        "engine": record.engine,
        "power_kw": record.power_kw,
        "fitments": [
            {
                "position": fitment.position,
                "oem_numbers": list(fitment.oem_numbers),
                "turbo_numbers": list(fitment.turbo_numbers),
                "articles": list(fitment.articles),
                "evidence": fitment.evidence,
            }
            for fitment in record.fitments
        ],
        "sources": [
            {"label": source.label, "url": source.url}
            for source in record.sources
        ],
        "notes": record.notes,
        "verified_at": record.verified_at,
        "online_search_at": record.online_search_at,
        "online_search_provider": record.online_search_provider,
    }


def _record_from_dict(raw: Any) -> VinRecord:
    if not isinstance(raw, dict):
        raise ValueError("VIN record must be an object")
    vin = extract_vin(str(raw.get("vin", "")))
    if vin is None:
        raise ValueError("VIN record contains an invalid VIN")
    status = str(raw.get("status", ""))
    if status not in {"verified", "pending"}:
        raise ValueError("VIN record contains an invalid status")

    fitments = tuple(
        VinFitment(
            position=str(item.get("position", "")).strip(),
            oem_numbers=_string_tuple(item.get("oem_numbers")),
            turbo_numbers=_string_tuple(item.get("turbo_numbers")),
            articles=_string_tuple(item.get("articles")),
            evidence=str(item.get("evidence", "")).strip(),
        )
        for item in raw.get("fitments", [])
        if isinstance(item, dict)
    )
    sources = tuple(
        VinSource(
            label=str(item.get("label", "")).strip(),
            url=str(item.get("url", "")).strip(),
        )
        for item in raw.get("sources", [])
        if isinstance(item, dict)
    )
    return VinRecord(
        vin=vin,
        status=status,
        make=str(raw.get("make", "")).strip(),
        model=str(raw.get("model", "")).strip(),
        model_year=str(raw.get("model_year", "")).strip(),
        engine=str(raw.get("engine", "")).strip(),
        power_kw=str(raw.get("power_kw", "")).strip(),
        fitments=fitments,
        sources=sources,
        notes=str(raw.get("notes", "")).strip(),
        verified_at=str(raw.get("verified_at", "")).strip(),
        online_search_at=str(raw.get("online_search_at", "")).strip(),
        online_search_provider=str(
            raw.get("online_search_provider", "")
        ).strip(),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        cleaned
        for item in value
        if (cleaned := str(item).strip())
    )

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from vin_search import VinRecord, extract_vin, utc_now


@dataclass(frozen=True)
class UnresolvedVin:
    vin: str
    failure_code: str
    failure_detail: str
    make: str
    model: str
    model_year: str
    engine: str
    power_kw: str
    online_search_provider: str
    online_search_at: str
    request_count: int
    first_requested_at: str
    last_requested_at: str


@dataclass(frozen=True)
class UnresolvedVinStats:
    unique_vins: int
    requests: int


class UnresolvedVinStore:
    """Persistent queue containing only VIN requests without a usable result."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> UnresolvedVinStats:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS unresolved_vins(
                    vin TEXT PRIMARY KEY,
                    failure_code TEXT NOT NULL,
                    failure_detail TEXT NOT NULL,
                    make TEXT NOT NULL,
                    model TEXT NOT NULL,
                    model_year TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    power_kw TEXT NOT NULL,
                    online_search_provider TEXT NOT NULL,
                    online_search_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL CHECK(request_count > 0),
                    first_requested_at TEXT NOT NULL,
                    last_requested_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_unresolved_vins_priority
                    ON unresolved_vins(request_count DESC, last_requested_at DESC);

                CREATE TABLE IF NOT EXISTS unresolved_notifications(
                    vin TEXT NOT NULL REFERENCES unresolved_vins(vin)
                        ON DELETE CASCADE,
                    admin_chat_id INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT '',
                    message_id INTEGER,
                    PRIMARY KEY(vin, admin_chat_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_unresolved_notifications_message
                    ON unresolved_notifications(admin_chat_id, message_id)
                    WHERE message_id IS NOT NULL;
                """
            )
            connection.commit()
        return self.stats()

    def record_failure(
        self,
        vin: str,
        *,
        failure_code: str,
        failure_detail: str = "",
        record: VinRecord | None = None,
    ) -> UnresolvedVin:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        code = " ".join(failure_code.split())
        if not code:
            raise ValueError("failure_code must not be empty")
        if record is not None and record.vin != normalized:
            raise ValueError("VIN record does not match request")

        now = utc_now()
        values = {
            "make": record.make if record is not None else "",
            "model": record.model if record is not None else "",
            "model_year": record.model_year if record is not None else "",
            "engine": record.engine if record is not None else "",
            "power_kw": record.power_kw if record is not None else "",
            "online_search_provider": (
                record.online_search_provider if record is not None else ""
            ),
            "online_search_at": (
                record.online_search_at if record is not None else ""
            ),
        }

        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO unresolved_vins(
                    vin,
                    failure_code,
                    failure_detail,
                    make,
                    model,
                    model_year,
                    engine,
                    power_kw,
                    online_search_provider,
                    online_search_at,
                    request_count,
                    first_requested_at,
                    last_requested_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(vin) DO UPDATE SET
                    failure_code = excluded.failure_code,
                    failure_detail = excluded.failure_detail,
                    make = CASE
                        WHEN excluded.make <> '' THEN excluded.make
                        ELSE unresolved_vins.make
                    END,
                    model = CASE
                        WHEN excluded.model <> '' THEN excluded.model
                        ELSE unresolved_vins.model
                    END,
                    model_year = CASE
                        WHEN excluded.model_year <> '' THEN excluded.model_year
                        ELSE unresolved_vins.model_year
                    END,
                    engine = CASE
                        WHEN excluded.engine <> '' THEN excluded.engine
                        ELSE unresolved_vins.engine
                    END,
                    power_kw = CASE
                        WHEN excluded.power_kw <> '' THEN excluded.power_kw
                        ELSE unresolved_vins.power_kw
                    END,
                    online_search_provider = CASE
                        WHEN excluded.online_search_provider <> ''
                            THEN excluded.online_search_provider
                        ELSE unresolved_vins.online_search_provider
                    END,
                    online_search_at = CASE
                        WHEN excluded.online_search_at <> ''
                            THEN excluded.online_search_at
                        ELSE unresolved_vins.online_search_at
                    END,
                    request_count = unresolved_vins.request_count + 1,
                    last_requested_at = excluded.last_requested_at
                """,
                (
                    normalized,
                    code,
                    " ".join(failure_detail.split()),
                    values["make"],
                    values["model"],
                    values["model_year"],
                    values["engine"],
                    values["power_kw"],
                    values["online_search_provider"],
                    values["online_search_at"],
                    now,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM unresolved_vins WHERE vin = ?",
                (normalized,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to persist unresolved VIN")
        return _unresolved_from_row(row)

    def remove(self, vin: str) -> bool:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM unresolved_vins WHERE vin = ?",
                (normalized,),
            )
            connection.commit()
        return cursor.rowcount > 0

    def claim_notification(self, vin: str, admin_chat_id: int) -> bool:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        if admin_chat_id <= 0:
            raise ValueError("admin_chat_id must be positive")

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO unresolved_notifications(
                    vin, admin_chat_id, claimed_at
                )
                SELECT vin, ?, ?
                FROM unresolved_vins
                WHERE vin = ?
                """,
                (admin_chat_id, utc_now(), normalized),
            )
            connection.commit()
        return cursor.rowcount > 0

    def mark_notification_sent(
        self,
        vin: str,
        admin_chat_id: int,
        message_id: int,
    ) -> None:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        if admin_chat_id <= 0 or message_id <= 0:
            raise ValueError("Telegram IDs must be positive")

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE unresolved_notifications
                SET sent_at = ?, message_id = ?
                WHERE vin = ? AND admin_chat_id = ?
                """,
                (utc_now(), message_id, normalized, admin_chat_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("VIN notification was not claimed")

    def release_notification(self, vin: str, admin_chat_id: int) -> bool:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        if admin_chat_id <= 0:
            raise ValueError("admin_chat_id must be positive")

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                DELETE FROM unresolved_notifications
                WHERE vin = ? AND admin_chat_id = ? AND sent_at = ''
                """,
                (normalized, admin_chat_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def find_notification_vin(
        self,
        admin_chat_id: int,
        message_id: int,
    ) -> str | None:
        if admin_chat_id <= 0 or message_id <= 0:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT vin
                FROM unresolved_notifications
                WHERE admin_chat_id = ? AND message_id = ?
                """,
                (admin_chat_id, message_id),
            ).fetchone()
        return str(row["vin"]) if row is not None else None

    def list(self, *, limit: int = 100) -> tuple[UnresolvedVin, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM unresolved_vins
                ORDER BY request_count DESC, last_requested_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_unresolved_from_row(row) for row in rows)

    def stats(self) -> UnresolvedVinStats:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS unique_vins,
                    COALESCE(SUM(request_count), 0) AS requests
                FROM unresolved_vins
                """
            ).fetchone()
        return UnresolvedVinStats(
            unique_vins=int(row["unique_vins"]),
            requests=int(row["requests"]),
        )


def _unresolved_from_row(row: sqlite3.Row) -> UnresolvedVin:
    return UnresolvedVin(
        vin=row["vin"],
        failure_code=row["failure_code"],
        failure_detail=row["failure_detail"],
        make=row["make"],
        model=row["model"],
        model_year=row["model_year"],
        engine=row["engine"],
        power_kw=row["power_kw"],
        online_search_provider=row["online_search_provider"],
        online_search_at=row["online_search_at"],
        request_count=int(row["request_count"]),
        first_requested_at=row["first_requested_at"],
        last_requested_at=row["last_requested_at"],
    )

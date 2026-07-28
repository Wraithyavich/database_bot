from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class VinObserverJob:
    vin: str
    attempt_count: int
    next_attempt_at: str
    last_attempt_at: str
    last_result: str


@dataclass(frozen=True)
class VinObserverAttempt:
    id: int
    vin: str
    attempted_at: str
    stage: str
    status: str
    summary: str
    checked_sources: tuple[str, ...]
    report: Any


@dataclass(frozen=True)
class VinResultSubscription:
    id: int
    vin: str
    user_id: int
    chat_id: int
    username: str
    status_message_id: int
    requested_at: str
    delivered_at: str


@dataclass(frozen=True)
class VinManualRequest:
    id: int
    vin: str
    user_id: int
    chat_id: int
    username: str
    requested_at: str
    completed_at: str


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

                CREATE TABLE IF NOT EXISTS vin_observer_jobs(
                    vin TEXT PRIMARY KEY REFERENCES unresolved_vins(vin)
                        ON DELETE CASCADE,
                    attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK(attempt_count >= 0),
                    next_attempt_at TEXT NOT NULL,
                    lease_until TEXT NOT NULL DEFAULT '',
                    last_attempt_at TEXT NOT NULL DEFAULT '',
                    last_result TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_vin_observer_jobs_due
                    ON vin_observer_jobs(next_attempt_at, lease_until);

                CREATE TABLE IF NOT EXISTS vin_observer_daily_usage(
                    usage_date TEXT PRIMARY KEY,
                    attempt_count INTEGER NOT NULL
                        CHECK(attempt_count >= 0)
                );

                CREATE TABLE IF NOT EXISTS vin_observer_attempts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vin TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    checked_sources_json TEXT NOT NULL,
                    report_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_vin_observer_attempts_vin
                    ON vin_observer_attempts(vin, id DESC);

                CREATE TABLE IF NOT EXISTS vin_result_subscriptions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vin TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    status_message_id INTEGER NOT NULL,
                    requested_at TEXT NOT NULL,
                    delivered_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(chat_id, status_message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_vin_result_subscriptions_pending
                    ON vin_result_subscriptions(vin, delivered_at, id);

                CREATE TABLE IF NOT EXISTS vin_manual_requests(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vin TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_vin_manual_requests_pending
                    ON vin_manual_requests(vin, user_id, chat_id)
                    WHERE completed_at = '';

                INSERT OR IGNORE INTO vin_observer_jobs(
                    vin, next_attempt_at
                )
                SELECT vin, CURRENT_TIMESTAMP
                FROM unresolved_vins;
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
        observer_delay_seconds: int = 86_400,
    ) -> UnresolvedVin:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        code = " ".join(failure_code.split())
        if not code:
            raise ValueError("failure_code must not be empty")
        if record is not None and record.vin != normalized:
            raise ValueError("VIN record does not match request")
        if observer_delay_seconds < 0:
            raise ValueError("observer_delay_seconds must not be negative")

        now = utc_now()
        next_attempt_at = _utc_after(
            observer_delay_seconds,
            base=now,
        )
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
            connection.execute(
                """
                INSERT OR IGNORE INTO vin_observer_jobs(
                    vin, next_attempt_at
                )
                VALUES (?, ?)
                """,
                (normalized, next_attempt_at),
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

    def subscribe_result(
        self,
        vin: str,
        *,
        user_id: int,
        chat_id: int,
        username: str = "",
        status_message_id: int,
    ) -> VinResultSubscription:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        _validate_telegram_target(
            user_id=user_id,
            chat_id=chat_id,
            message_id=status_message_id,
        )
        cleaned_username = username.strip().lstrip("@")[:64]
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO vin_result_subscriptions(
                    vin,
                    user_id,
                    chat_id,
                    username,
                    status_message_id,
                    requested_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized,
                    user_id,
                    chat_id,
                    cleaned_username,
                    status_message_id,
                    utc_now(),
                ),
            )
            row = connection.execute(
                """
                SELECT *
                FROM vin_result_subscriptions
                WHERE chat_id = ? AND status_message_id = ?
                """,
                (chat_id, status_message_id),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("Failed to persist VIN result subscription")
        return _result_subscription_from_row(row)

    def pending_result_subscriptions(
        self,
        vin: str,
        *,
        limit: int = 100,
    ) -> tuple[VinResultSubscription, ...]:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM vin_result_subscriptions
                WHERE vin = ? AND delivered_at = ''
                ORDER BY id
                LIMIT ?
                """,
                (normalized, limit),
            ).fetchall()
        return tuple(_result_subscription_from_row(row) for row in rows)

    def mark_result_delivered(self, subscription_id: int) -> None:
        if subscription_id <= 0:
            raise ValueError("subscription_id must be positive")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE vin_result_subscriptions
                SET delivered_at = ?
                WHERE id = ? AND delivered_at = ''
                """,
                (utc_now(), subscription_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("VIN result subscription is not pending")

    def claim_manual_request(
        self,
        vin: str,
        *,
        user_id: int,
        chat_id: int,
        username: str = "",
    ) -> VinManualRequest | None:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        _validate_telegram_target(
            user_id=user_id,
            chat_id=chat_id,
        )
        cleaned_username = username.strip().lstrip("@")[:64]
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO vin_manual_requests(
                    vin,
                    user_id,
                    chat_id,
                    username,
                    requested_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized,
                    user_id,
                    chat_id,
                    cleaned_username,
                    utc_now(),
                ),
            )
            row = (
                connection.execute(
                    "SELECT * FROM vin_manual_requests WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                if cursor.rowcount == 1
                else None
            )
            connection.commit()
        return _manual_request_from_row(row) if row is not None else None

    def release_manual_request(self, request_id: int) -> bool:
        if request_id <= 0:
            raise ValueError("request_id must be positive")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                DELETE FROM vin_manual_requests
                WHERE id = ? AND completed_at = ''
                """,
                (request_id,),
            )
            connection.commit()
        return cursor.rowcount > 0

    def pending_manual_requests(
        self,
        vin: str,
        *,
        limit: int = 100,
    ) -> tuple[VinManualRequest, ...]:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM vin_manual_requests
                WHERE vin = ? AND completed_at = ''
                ORDER BY id
                LIMIT ?
                """,
                (normalized, limit),
            ).fetchall()
        return tuple(_manual_request_from_row(row) for row in rows)

    def mark_manual_request_completed(self, request_id: int) -> None:
        if request_id <= 0:
            raise ValueError("request_id must be positive")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE vin_manual_requests
                SET completed_at = ?
                WHERE id = ? AND completed_at = ''
                """,
                (utc_now(), request_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("VIN manual request is not pending")

    def claim_due_observer_job(
        self,
        *,
        daily_limit: int,
        lease_seconds: int = 600,
        now: str | None = None,
    ) -> VinObserverJob | None:
        if daily_limit < 1:
            raise ValueError("daily_limit must be positive")
        if lease_seconds < 60:
            raise ValueError("lease_seconds must be at least 60")
        current = _normalize_utc(now or utc_now())
        usage_date = current[:10]
        lease_until = _utc_after(lease_seconds, base=current)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO vin_observer_daily_usage(
                    usage_date, attempt_count
                )
                VALUES (?, 0)
                """,
                (usage_date,),
            )
            used = int(
                connection.execute(
                    """
                    SELECT attempt_count
                    FROM vin_observer_daily_usage
                    WHERE usage_date = ?
                    """,
                    (usage_date,),
                ).fetchone()["attempt_count"]
            )
            if used >= daily_limit:
                connection.commit()
                return None

            row = connection.execute(
                """
                SELECT *
                FROM vin_observer_jobs
                WHERE next_attempt_at <= ?
                  AND (lease_until = '' OR lease_until <= ?)
                ORDER BY next_attempt_at, attempt_count, vin
                LIMIT 1
                """,
                (current, current),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            connection.execute(
                """
                UPDATE vin_observer_jobs
                SET lease_until = ?
                WHERE vin = ?
                """,
                (lease_until, row["vin"]),
            )
            connection.execute(
                """
                UPDATE vin_observer_daily_usage
                SET attempt_count = attempt_count + 1
                WHERE usage_date = ?
                """,
                (usage_date,),
            )
            connection.commit()
        return _observer_job_from_row(row)

    def complete_observer_attempt(
        self,
        vin: str,
        *,
        next_delay_seconds: int,
        result: str,
        now: str | None = None,
    ) -> VinObserverJob:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        if next_delay_seconds < 60:
            raise ValueError("next_delay_seconds must be at least 60")
        current = _normalize_utc(now or utc_now())
        next_attempt_at = _utc_after(
            next_delay_seconds,
            base=current,
        )
        cleaned_result = " ".join(result.split())[:500]

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE vin_observer_jobs
                SET attempt_count = attempt_count + 1,
                    next_attempt_at = ?,
                    lease_until = '',
                    last_attempt_at = ?,
                    last_result = ?
                WHERE vin = ?
                """,
                (
                    next_attempt_at,
                    current,
                    cleaned_result,
                    normalized,
                ),
            )
            row = connection.execute(
                "SELECT * FROM vin_observer_jobs WHERE vin = ?",
                (normalized,),
            ).fetchone()
            connection.commit()
        if cursor.rowcount != 1 or row is None:
            raise RuntimeError("VIN observer job does not exist")
        return _observer_job_from_row(row)

    def record_observer_attempt(
        self,
        vin: str,
        *,
        stage: str,
        status: str,
        summary: str = "",
        checked_sources: tuple[str, ...] = (),
        report: Any = None,
        now: str | None = None,
    ) -> VinObserverAttempt:
        normalized = extract_vin(vin)
        if normalized is None:
            raise ValueError("Invalid VIN")
        cleaned_stage = stage.strip().lower()
        cleaned_status = status.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", cleaned_stage):
            raise ValueError("Invalid observer attempt stage")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", cleaned_status):
            raise ValueError("Invalid observer attempt status")

        attempted_at = _normalize_utc(now or utc_now())
        cleaned_summary = " ".join(summary.split())[:2000]
        cleaned_sources = tuple(
            dict.fromkeys(
                " ".join(str(source).split())[:1000]
                for source in checked_sources[:20]
                if " ".join(str(source).split())
            )
        )
        sources_json = json.dumps(
            cleaned_sources,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        report_json = _bounded_json(report, max_length=20_000)

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO vin_observer_attempts(
                    vin,
                    attempted_at,
                    stage,
                    status,
                    summary,
                    checked_sources_json,
                    report_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized,
                    attempted_at,
                    cleaned_stage,
                    cleaned_status,
                    cleaned_summary,
                    sources_json,
                    report_json,
                ),
            )
            attempt_id = int(cursor.lastrowid)
            connection.execute(
                """
                DELETE FROM vin_observer_attempts
                WHERE id NOT IN (
                    SELECT id
                    FROM vin_observer_attempts
                    ORDER BY id DESC
                    LIMIT 1000
                )
                """
            )
            row = connection.execute(
                "SELECT * FROM vin_observer_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("Failed to persist VIN observer attempt")
        return _observer_attempt_from_row(row)

    def list_observer_attempts(
        self,
        *,
        vin: str | None = None,
        limit: int = 100,
    ) -> tuple[VinObserverAttempt, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        parameters: tuple[Any, ...]
        where = ""
        if vin is None:
            parameters = (limit,)
        else:
            normalized = extract_vin(vin)
            if normalized is None:
                raise ValueError("Invalid VIN")
            where = "WHERE vin = ?"
            parameters = (normalized, limit)

        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM vin_observer_attempts
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(_observer_attempt_from_row(row) for row in rows)

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


def _observer_job_from_row(row: sqlite3.Row) -> VinObserverJob:
    return VinObserverJob(
        vin=row["vin"],
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=row["next_attempt_at"],
        last_attempt_at=row["last_attempt_at"],
        last_result=row["last_result"],
    )


def _observer_attempt_from_row(row: sqlite3.Row) -> VinObserverAttempt:
    try:
        checked_sources = tuple(json.loads(row["checked_sources_json"]))
    except (TypeError, json.JSONDecodeError):
        checked_sources = ()
    try:
        report = json.loads(row["report_json"])
    except (TypeError, json.JSONDecodeError):
        report = None
    return VinObserverAttempt(
        id=int(row["id"]),
        vin=row["vin"],
        attempted_at=row["attempted_at"],
        stage=row["stage"],
        status=row["status"],
        summary=row["summary"],
        checked_sources=checked_sources,
        report=report,
    )


def _result_subscription_from_row(
    row: sqlite3.Row,
) -> VinResultSubscription:
    return VinResultSubscription(
        id=int(row["id"]),
        vin=row["vin"],
        user_id=int(row["user_id"]),
        chat_id=int(row["chat_id"]),
        username=row["username"],
        status_message_id=int(row["status_message_id"]),
        requested_at=row["requested_at"],
        delivered_at=row["delivered_at"],
    )


def _manual_request_from_row(row: sqlite3.Row) -> VinManualRequest:
    return VinManualRequest(
        id=int(row["id"]),
        vin=row["vin"],
        user_id=int(row["user_id"]),
        chat_id=int(row["chat_id"]),
        username=row["username"],
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
    )


def _validate_telegram_target(
    *,
    user_id: int,
    chat_id: int,
    message_id: int | None = None,
) -> None:
    if user_id <= 0 or chat_id == 0:
        raise ValueError("Invalid Telegram target")
    if message_id is not None and message_id <= 0:
        raise ValueError("Invalid Telegram message ID")


def _bounded_json(value: Any, *, max_length: int) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        encoded = json.dumps(str(value), ensure_ascii=False)
    if len(encoded) <= max_length:
        return encoded
    truncated = json.dumps(
        {
            "truncated": True,
            "preview": encoded[: max_length // 8],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return truncated


def _normalize_utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat()


def _utc_after(seconds: int, *, base: str) -> str:
    parsed = datetime.fromisoformat(base.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (
        parsed.astimezone(UTC) + timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat()

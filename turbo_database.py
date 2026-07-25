from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


MIN_PARTIAL_SEARCH_LENGTH = 4
DEFAULT_RESULT_LIMIT = 100

_CYRILLIC_LOOKALIKES = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "У": "Y",
        "Х": "X",
    }
)
_NON_ALPHANUMERIC = re.compile(r"[^A-Z0-9]")
_REQUIRED_TABLES = {"parts", "numbers", "sources"}


def normalize_number(value: str) -> str:
    """Нормализация должна совпадать с нормализацией в turbo_parts.sqlite."""
    normalized = value.strip().upper().translate(_CYRILLIC_LOOKALIKES)
    return _NON_ALPHANUMERIC.sub("", normalized)


@dataclass(frozen=True)
class ArticleMatch:
    article: str
    categories: tuple[str, ...]
    matched_types: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class SearchResult:
    original_query: str
    normalized_query: str
    matched_query: str
    matches: tuple[ArticleMatch, ...]
    exact: bool
    truncated: bool
    fallback_used: bool = False


@dataclass(frozen=True)
class DatabaseStats:
    parts: int
    numbers: int
    crossrefs: int
    sources: int


class TurboDatabase:
    """Потокобезопасный read-only доступ к каталогу через короткие соединения."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def validate(self) -> DatabaseStats:
        if not self.path.is_file():
            raise FileNotFoundError(f"База SQLite не найдена: {self.path}")

        with closing(self._connect()) as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = _REQUIRED_TABLES - tables
            if missing:
                missing_names = ", ".join(sorted(missing))
                raise RuntimeError(
                    f"В базе отсутствуют обязательные таблицы: {missing_names}"
                )

            crossrefs = (
                connection.execute("SELECT COUNT(*) FROM crossrefs").fetchone()[0]
                if "crossrefs" in tables
                else 0
            )
            return DatabaseStats(
                parts=connection.execute("SELECT COUNT(*) FROM parts").fetchone()[0],
                numbers=connection.execute("SELECT COUNT(*) FROM numbers").fetchone()[0],
                crossrefs=crossrefs,
                sources=connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            )

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_RESULT_LIMIT,
        min_partial_length: int = MIN_PARTIAL_SEARCH_LENGTH,
        allow_partial: bool = True,
        allow_fallback: bool = True,
    ) -> SearchResult:
        if limit < 1:
            raise ValueError("limit должен быть больше нуля")

        normalized = normalize_number(query)
        if not normalized:
            return SearchResult(
                original_query=query,
                normalized_query="",
                matched_query="",
                matches=(),
                exact=True,
                truncated=False,
            )

        with closing(self._connect()) as connection:
            matches, truncated = self._find_articles(
                connection, normalized, exact=True, limit=limit
            )
            if matches:
                return SearchResult(
                    original_query=query,
                    normalized_query=normalized,
                    matched_query=normalized,
                    matches=matches,
                    exact=True,
                    truncated=truncated,
                )

            if allow_partial and len(normalized) >= min_partial_length:
                matches, truncated = self._find_articles(
                    connection, normalized, exact=False, limit=limit
                )
                if matches:
                    return SearchResult(
                        original_query=query,
                        normalized_query=normalized,
                        matched_query=normalized,
                        matches=matches,
                        exact=False,
                        truncated=truncated,
                    )

            fallback = self._fallback_970(normalized) if allow_fallback else None
            if fallback is not None:
                fallback_exact = True
                matches, truncated = self._find_articles(
                    connection, fallback, exact=True, limit=limit
                )
                if (
                    not matches
                    and allow_partial
                    and len(fallback) >= min_partial_length
                ):
                    fallback_exact = False
                    matches, truncated = self._find_articles(
                        connection, fallback, exact=False, limit=limit
                    )
                if matches:
                    return SearchResult(
                        original_query=query,
                        normalized_query=normalized,
                        matched_query=fallback,
                        matches=matches,
                        exact=fallback_exact,
                        truncated=truncated,
                        fallback_used=True,
                    )

        return SearchResult(
            original_query=query,
            normalized_query=normalized,
            matched_query=normalized,
            matches=(),
            exact=True,
            truncated=False,
        )

    @staticmethod
    def _fallback_970(normalized: str) -> str | None:
        if len(normalized) != 11 or not normalized.isdigit():
            return None
        if normalized[4:7] == "970":
            return None
        return f"{normalized[:4]}970{normalized[7:]}"

    @staticmethod
    def _find_articles(
        connection: sqlite3.Connection,
        normalized: str,
        *,
        exact: bool,
        limit: int,
    ) -> tuple[tuple[ArticleMatch, ...], bool]:
        operator = "=" if exact else "LIKE"
        parameter = normalized if exact else f"%{normalized}%"
        rows = connection.execute(
            f"""
            SELECT
                p.article,
                GROUP_CONCAT(DISTINCT p.category) AS categories,
                GROUP_CONCAT(DISTINCT n.number_type) AS matched_types,
                GROUP_CONCAT(DISTINCT s.name) AS sources
            FROM numbers AS n
            JOIN parts AS p ON p.id = n.part_id
            LEFT JOIN sources AS s ON s.id = n.source_id
            WHERE n.number_norm {operator} ?
            GROUP BY p.article
            ORDER BY p.article COLLATE NOCASE
            LIMIT ?
            """,
            (parameter, limit + 1),
        ).fetchall()

        truncated = len(rows) > limit
        matches = tuple(
            ArticleMatch(
                article=row["article"],
                categories=TurboDatabase._split_group(row["categories"]),
                matched_types=TurboDatabase._split_group(row["matched_types"]),
                sources=TurboDatabase._split_group(row["sources"]),
            )
            for row in rows[:limit]
        )
        return matches, truncated

    @staticmethod
    def _split_group(value: str | None) -> tuple[str, ...]:
        if not value:
            return ()
        return tuple(sorted(set(value.split(","))))

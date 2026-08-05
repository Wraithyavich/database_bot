from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from reverse_search import compact_article, natural_sort_key, normalize_article


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
_REVERSE_TABLES = {"part_numbers", "article_aliases"}
_SQLITE_HEADER = b"SQLite format 3\x00"
_LFS_POINTER_HEADER = "version https://git-lfs.github.com/spec/v1"
_LFS_OID_PATTERN = re.compile(r"^oid sha256:([0-9a-f]{64})$", re.MULTILINE)
_LFS_SIZE_PATTERN = re.compile(r"^size ([0-9]+)$", re.MULTILINE)
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


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
class ReverseNumber:
    number: str
    normalized: str
    kind: str
    source_catalog: str
    source_page: int | None


@dataclass(frozen=True)
class ReverseSearchResult:
    original_query: str
    normalized_query: str
    matched_article: str | None
    matched_article_norm: str | None
    categories: tuple[str, ...]
    numbers: tuple[ReverseNumber, ...]
    resolution: str
    candidates: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.matched_article is not None

    def numbers_of_kind(self, kind: str) -> tuple[ReverseNumber, ...]:
        return tuple(number for number in self.numbers if number.kind == kind)


@dataclass(frozen=True)
class DatabaseStats:
    parts: int
    numbers: int
    crossrefs: int
    sources: int
    reverse_numbers: int = 0


@dataclass(frozen=True)
class GitLfsPointer:
    oid: str
    size: int


def is_sqlite_database(path: str | Path) -> bool:
    database_path = Path(path)
    try:
        with database_path.open("rb") as database_file:
            return database_file.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def read_git_lfs_pointer(path: str | Path) -> GitLfsPointer | None:
    pointer_path = Path(path)
    try:
        if pointer_path.stat().st_size > 4096:
            return None
        content = pointer_path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None

    if not content.startswith(_LFS_POINTER_HEADER):
        return None

    oid_match = _LFS_OID_PATTERN.search(content)
    size_match = _LFS_SIZE_PATTERN.search(content)
    if oid_match is None or size_match is None:
        return None
    return GitLfsPointer(oid=oid_match.group(1), size=int(size_match.group(1)))


def ensure_sqlite_database(
    path: str | Path,
    *,
    download_url: str | None,
    cache_dir: str | Path | None = None,
    timeout: float = 300,
) -> Path:
    """Return a usable SQLite file, downloading an unresolved Git LFS object when needed."""
    database_path = Path(path).expanduser().resolve()
    if is_sqlite_database(database_path):
        return database_path

    pointer = read_git_lfs_pointer(database_path)
    if not download_url:
        if pointer is not None:
            raise sqlite3.DatabaseError(
                f"{database_path} contains a Git LFS pointer instead of the SQLite database"
            )
        raise sqlite3.DatabaseError(
            f"{database_path} is missing or is not a SQLite database"
        )

    destination_dir = (
        Path(cache_dir).expanduser().resolve()
        if cache_dir is not None
        else Path(tempfile.gettempdir()).resolve() / "turbo-database"
    )
    destination_dir.mkdir(parents=True, exist_ok=True)

    cache_key = pointer.oid[:16] if pointer is not None else "downloaded"
    destination_path = destination_dir / f"{database_path.stem}-{cache_key}.sqlite"
    if is_sqlite_database(destination_path):
        if pointer is None or destination_path.stat().st_size == pointer.size:
            return destination_path

    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": "database-bot/1.0"},
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f"{database_path.stem}-",
            suffix=".sqlite.tmp",
            dir=destination_dir,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            digest = hashlib.sha256()
            downloaded_size = 0
            with urllib.request.urlopen(request, timeout=timeout) as response:
                while chunk := response.read(_DOWNLOAD_CHUNK_SIZE):
                    temporary_file.write(chunk)
                    digest.update(chunk)
                    downloaded_size += len(chunk)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        if pointer is not None and downloaded_size != pointer.size:
            raise sqlite3.DatabaseError(
                "Downloaded SQLite size does not match the Git LFS pointer "
                f"({downloaded_size} != {pointer.size})"
            )
        if pointer is not None and digest.hexdigest() != pointer.oid:
            raise sqlite3.DatabaseError(
                "Downloaded SQLite checksum does not match the Git LFS pointer"
            )
        if not is_sqlite_database(temporary_path):
            raise sqlite3.DatabaseError(
                "Downloaded file is not a valid SQLite database"
            )

        os.replace(temporary_path, destination_path)
        temporary_path = None
        return destination_path
    except (OSError, urllib.error.URLError) as error:
        raise sqlite3.DatabaseError(
            f"Unable to download SQLite database from {download_url}: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


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

            reverse_numbers = (
                connection.execute("SELECT COUNT(*) FROM part_numbers").fetchone()[0]
                if "part_numbers" in tables
                else 0
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
                reverse_numbers=reverse_numbers,
            )

    def reverse_search(self, query: str) -> ReverseSearchResult | None:
        """Resolve an E&E article before the legacy number-to-article search."""
        normalized = normalize_article(query)
        if not normalized:
            return None

        with closing(self._connect()) as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not _REVERSE_TABLES.issubset(tables):
                return None

            exact = self._article_norms(connection, "article_norm", normalized)
            if exact:
                return self._load_reverse_result(
                    connection,
                    query,
                    normalized,
                    exact[0][0],
                    resolution="exact",
                )

            alias_targets = connection.execute(
                """
                SELECT DISTINCT target_article_norm
                FROM article_aliases
                WHERE alias_norm = ?
                ORDER BY target_article_norm
                """,
                (normalized,),
            ).fetchall()
            alias_norms = tuple(row[0] for row in alias_targets)
            if len(alias_norms) == 1:
                return self._load_reverse_result(
                    connection,
                    query,
                    normalized,
                    alias_norms[0],
                    resolution="alias",
                )
            if len(alias_norms) > 1:
                return self._ambiguous_result(
                    connection, query, normalized, alias_norms, "alias_ambiguous"
                )

            tn_candidates = self._tn_alias_candidates(normalized)
            for candidate in tn_candidates:
                rows = self._article_norms(connection, "article_norm", candidate)
                if rows:
                    return self._load_reverse_result(
                        connection,
                        query,
                        normalized,
                        rows[0][0],
                        resolution="tn_alias",
                    )

            compact = compact_article(normalized)
            if not compact:
                return None
            compact_matches = self._article_norms(
                connection, "article_compact", compact
            )
            distinct_norms = tuple(dict.fromkeys(row[0] for row in compact_matches))
            if len(distinct_norms) == 1:
                return self._load_reverse_result(
                    connection,
                    query,
                    normalized,
                    distinct_norms[0],
                    resolution="compact",
                )
            if len(distinct_norms) > 1:
                return self._ambiguous_result(
                    connection,
                    query,
                    normalized,
                    distinct_norms,
                    "compact_ambiguous",
                )
        return None

    @staticmethod
    def _tn_alias_candidates(normalized: str) -> tuple[str, ...]:
        if not normalized.startswith("TN-"):
            return ()
        without_prefix = normalized[3:]
        candidates = [without_prefix]
        if without_prefix.endswith("-OE") and len(without_prefix) > 3:
            candidates.append(without_prefix[:-3])
        return tuple(dict.fromkeys(candidates))

    @staticmethod
    def _article_norms(
        connection: sqlite3.Connection,
        column: str,
        value: str,
    ) -> tuple[tuple[str, str], ...]:
        if column not in {"article_norm", "article_compact"}:
            raise ValueError("Unsupported article lookup column")
        rows = connection.execute(
            f"""
            SELECT DISTINCT article_norm, article
            FROM parts
            WHERE {column} = ?
            ORDER BY article COLLATE NOCASE, article_norm
            """,
            (value,),
        ).fetchall()
        return tuple((row["article_norm"], row["article"]) for row in rows)

    @staticmethod
    def _ambiguous_result(
        connection: sqlite3.Connection,
        original_query: str,
        normalized_query: str,
        article_norms: tuple[str, ...],
        resolution: str,
    ) -> ReverseSearchResult:
        placeholders = ",".join("?" for _ in article_norms)
        rows = connection.execute(
            f"""
            SELECT DISTINCT article
            FROM parts
            WHERE article_norm IN ({placeholders})
            """,
            article_norms,
        ).fetchall()
        candidates = tuple(
            sorted({row["article"] for row in rows}, key=natural_sort_key)
        )
        return ReverseSearchResult(
            original_query=original_query,
            normalized_query=normalized_query,
            matched_article=None,
            matched_article_norm=None,
            categories=(),
            numbers=(),
            resolution=resolution,
            candidates=candidates,
        )

    @staticmethod
    def _load_reverse_result(
        connection: sqlite3.Connection,
        original_query: str,
        normalized_query: str,
        article_norm: str,
        *,
        resolution: str,
    ) -> ReverseSearchResult:
        part_rows = connection.execute(
            """
            SELECT article, category
            FROM parts
            WHERE article_norm = ?
            ORDER BY id
            """,
            (article_norm,),
        ).fetchall()
        if not part_rows:
            raise RuntimeError(f"Alias target is absent: {article_norm}")

        number_rows = connection.execute(
            """
            SELECT
                pn.number,
                pn.number_norm,
                pn.number_kind,
                pn.source_catalog,
                pn.source_page,
                pn.catalog_order
            FROM part_numbers AS pn
            JOIN parts AS p ON p.id = pn.part_id
            WHERE p.article_norm = ?
            ORDER BY pn.catalog_order, pn.id
            """,
            (article_norm,),
        ).fetchall()
        numbers = tuple(
            sorted(
                (
                    ReverseNumber(
                        number=row["number"],
                        normalized=row["number_norm"],
                        kind=row["number_kind"],
                        source_catalog=row["source_catalog"],
                        source_page=row["source_page"],
                    )
                    for row in number_rows
                ),
                key=lambda number: (
                    {
                        "turbo_pn": 0,
                        "vehicle_oem": 1,
                        "component_pn": 2,
                        "external_cross": 3,
                        "unknown": 4,
                    }.get(number.kind, 5),
                    natural_sort_key(number.number),
                ),
            )
        )
        categories = tuple(
            sorted({row["category"] for row in part_rows}, key=natural_sort_key)
        )
        return ReverseSearchResult(
            original_query=original_query,
            normalized_query=normalized_query,
            matched_article=part_rows[0]["article"],
            matched_article_norm=article_norm,
            categories=categories,
            numbers=numbers,
            resolution=resolution,
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

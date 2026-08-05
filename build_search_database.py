from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

from reverse_search import (
    EXPLICIT_ARTICLE_EQUIVALENCES,
    ClassificationContext,
    NumberEvidence,
    classify_number_evidence,
    compact_article,
    natural_sort_key,
    normalize_article,
    resolve_evidence,
)


SCHEMA_VERSION = 2
_NON_ALPHANUMERIC = re.compile(r"[^A-Z0-9]")
_JRONE_SEPARATOR = re.compile(r"[,/\s]+")


def _number_norm(value: str) -> str:
    return _NON_ALPHANUMERIC.sub("", value.strip().upper())


def _split_jrone_turbo_field(value: str) -> tuple[str, ...]:
    """Expand the delimiter/shorthand syntax used by jronecross.csv."""
    result: list[str] = []
    previous = ""
    for raw_token in _JRONE_SEPARATOR.split(value.strip()):
        token = raw_token.strip(".;")
        if not token:
            continue
        if token.isdigit() and len(token) <= 4 and "-" in previous:
            prefix, suffix = previous.rsplit("-", 1)
            if suffix.isdigit() and len(token) <= len(suffix):
                token = f"{prefix}-{suffix[:-len(token)]}{token}"
        result.append(token)
        previous = token
    return tuple(dict.fromkeys(result))


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        PRAGMA foreign_keys = ON;

        CREATE TABLE sources(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE parts(
            id INTEGER PRIMARY KEY,
            article TEXT NOT NULL,
            article_norm TEXT NOT NULL,
            article_compact TEXT NOT NULL,
            category TEXT NOT NULL
        );

        -- Compatibility table used by the original direct search.
        CREATE TABLE numbers(
            part_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            number_norm TEXT NOT NULL,
            number_type TEXT NOT NULL
        );

        CREATE TABLE part_numbers(
            id INTEGER PRIMARY KEY,
            part_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            number TEXT NOT NULL,
            number_norm TEXT NOT NULL,
            number_kind TEXT NOT NULL CHECK(number_kind IN (
                'turbo_pn', 'vehicle_oem', 'component_pn',
                'external_cross', 'unknown'
            )),
            source_catalog TEXT NOT NULL,
            source_page INTEGER,
            source_column TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            catalog_order INTEGER NOT NULL,
            FOREIGN KEY(part_id) REFERENCES parts(id),
            FOREIGN KEY(source_id) REFERENCES sources(id)
        );

        CREATE TABLE article_aliases(
            alias_norm TEXT NOT NULL,
            target_article_norm TEXT NOT NULL,
            alias_kind TEXT NOT NULL,
            source_note TEXT NOT NULL,
            PRIMARY KEY(alias_norm, target_article_norm)
        );

        CREATE TABLE number_kind_conflicts(
            article_norm TEXT NOT NULL,
            number TEXT NOT NULL,
            number_norm TEXT NOT NULL,
            observed_kinds TEXT NOT NULL,
            resolution TEXT NOT NULL,
            PRIMARY KEY(article_norm, number_norm)
        );

        CREATE TABLE build_metadata(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def _load_evidence(source: sqlite3.Connection) -> tuple[NumberEvidence, ...]:
    rows = source.execute(
        """
        SELECT
            n.id,
            n.part_id,
            n.source_id,
            s.name AS source_catalog,
            p.article,
            n.number,
            n.number_norm,
            n.number_type,
            n.match_role,
            n.page,
            COALESCE(n.raw_context, p.raw_text, '') AS raw_context
        FROM numbers AS n
        JOIN parts AS p ON p.id = n.part_id
        JOIN sources AS s ON s.id = n.source_id
        ORDER BY n.id
        """
    )
    loaded = tuple(
        NumberEvidence(
            part_id=row[1],
            article_norm=normalize_article(row[4]),
            source_id=row[2],
            source_catalog=row[3],
            number=row[5],
            number_norm=row[6],
            number_type=row[7],
            match_role=row[8],
            source_page=row[9],
            raw_context=row[10],
            catalog_order=row[0],
        )
        for row in rows
    )
    # The second jronecross.csv field is a delimited Turbo P/N list. The legacy
    # importer retained a few combined values, so rebuild those rows from their
    # preserved raw source line instead of exposing a list as one "number".
    result = [
        evidence
        for evidence in loaded
        if evidence.source_id != 3 or evidence.number_type == "JRONE"
    ]
    seen_jrone_rows: set[tuple[int, str]] = set()
    for evidence in loaded:
        if evidence.source_id != 3:
            continue
        row_key = (evidence.part_id, evidence.raw_context)
        if row_key in seen_jrone_rows:
            continue
        seen_jrone_rows.add(row_key)
        fields = evidence.raw_context.split(";")
        if len(fields) < 3:
            continue
        for offset, number in enumerate(_split_jrone_turbo_field(fields[1]), 1):
            result.append(
                NumberEvidence(
                    part_id=evidence.part_id,
                    article_norm=evidence.article_norm,
                    source_id=3,
                    source_catalog=evidence.source_catalog,
                    number=number,
                    number_norm=_number_norm(number),
                    number_type="Turbo P/N",
                    match_role="normalized_crossref_input",
                    source_page=None,
                    raw_context=evidence.raw_context,
                    catalog_order=evidence.catalog_order + offset,
                )
            )
    return tuple(result)


def _classification_context(
    evidences: tuple[NumberEvidence, ...],
) -> ClassificationContext:
    trusted_turbo: dict[str, set[str]] = defaultdict(set)
    trusted_vehicle: dict[str, set[str]] = defaultdict(set)
    grouped: dict[tuple[int, int, str], list[NumberEvidence]] = defaultdict(list)

    for evidence in evidences:
        grouped[(evidence.part_id, evidence.source_id, evidence.number_type)].append(
            evidence
        )
        if evidence.match_role == "article" or evidence.number_type == "E&E P/N":
            continue
        if evidence.source_id in {3, 4, 7, 15}:
            if evidence.source_id not in {3, 4} or evidence.number_type != "JRONE":
                if evidence.source_id != 7 or evidence.number_type == "Turbo P/N":
                    trusted_turbo[evidence.article_norm].add(evidence.number_norm)
        if evidence.source_id == 1 and evidence.number_type == "Turbo P/N":
            trusted_turbo[evidence.article_norm].add(evidence.number_norm)
        if evidence.source_id == 2:
            trusted_vehicle[evidence.article_norm].add(evidence.number_norm)
        if evidence.source_id in {7, 8} and evidence.number_type == "Number":
            trusted_vehicle[evidence.article_norm].add(evidence.number_norm)

    first_catalog_number: set[tuple[int, int, str]] = set()
    for (part_id, source_id, number_type), values in grouped.items():
        if source_id in {8, 9} and number_type == "Turbo P/N":
            first = min(values, key=lambda value: value.catalog_order)
            first_catalog_number.add((part_id, source_id, first.number_norm))

    catalog_has_trusted_turbo: set[tuple[int, int]] = set()
    for evidence in evidences:
        if evidence.source_id not in {11, 12, 13, 14}:
            continue
        if evidence.number_type != "Turbo P/N":
            continue
        if evidence.number_norm in trusted_turbo.get(evidence.article_norm, set()):
            catalog_has_trusted_turbo.add((evidence.part_id, evidence.source_id))

    bearing_pre_application: set[tuple[int, str]] = set()
    bearing_candidates: dict[int, list[tuple[int, NumberEvidence]]] = defaultdict(list)
    for evidence in evidences:
        if evidence.source_id != 16 or evidence.number_type != "Number":
            continue
        raw = evidence.raw_context
        position = raw.find(evidence.number)
        application_start = raw.find("(")
        if position >= 0 and (application_start < 0 or position < application_start):
            bearing_pre_application.add((evidence.part_id, evidence.number_norm))
            bearing_candidates[evidence.part_id].append((position, evidence))
    for part_id, values in bearing_candidates.items():
        first = min(values, key=lambda value: (value[0], value[1].catalog_order))[1]
        first_catalog_number.add((part_id, 16, first.number_norm))

    return ClassificationContext(
        trusted_turbo_by_article={
            key: frozenset(value) for key, value in trusted_turbo.items()
        },
        trusted_vehicle_by_article={
            key: frozenset(value) for key, value in trusted_vehicle.items()
        },
        catalog_has_trusted_turbo=frozenset(catalog_has_trusted_turbo),
        first_catalog_number=frozenset(first_catalog_number),
        bearing_pre_application=frozenset(bearing_pre_application),
    )


def _insert_reverse_relations(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
) -> dict[str, int]:
    evidences = _load_evidence(source)
    context = _classification_context(evidences)
    classified_by_relation: dict[
        tuple[str, str], list
    ] = defaultdict(list)
    candidate_rows = 0

    for evidence in evidences:
        classified = classify_number_evidence(evidence, context)
        if classified is None:
            continue
        candidate_rows += 1
        classified_by_relation[
            (evidence.article_norm, evidence.number_norm)
        ].append(classified)

    conflicts = 0
    for (article_norm, number_norm), values in sorted(
        classified_by_relation.items(),
        key=lambda item: (
            natural_sort_key(item[0][0]),
            min(value.evidence.catalog_order for value in item[1]),
        ),
    ):
        resolved, observed_conflict = resolve_evidence(values)
        evidence = resolved.evidence
        destination.execute(
            """
            INSERT INTO part_numbers(
                part_id, source_id, number, number_norm, number_kind,
                source_catalog, source_page, source_column, confidence,
                catalog_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.part_id,
                evidence.source_id,
                evidence.number,
                evidence.number_norm,
                resolved.number_kind,
                evidence.source_catalog,
                evidence.source_page,
                resolved.source_column,
                resolved.confidence,
                evidence.catalog_order,
            ),
        )
        if observed_conflict:
            conflicts += 1
            destination.execute(
                """
                INSERT INTO number_kind_conflicts(
                    article_norm, number, number_norm, observed_kinds, resolution
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    article_norm,
                    evidence.number,
                    number_norm,
                    ", ".join(observed_conflict),
                    resolved.number_kind,
                ),
            )

    final_rows = len(classified_by_relation)
    return {
        "classification_candidates": candidate_rows,
        "deduplicated_relations": candidate_rows - final_rows,
        "classification_conflicts": conflicts,
    }


def _insert_aliases(destination: sqlite3.Connection) -> None:
    available = {
        row[0]
        for row in destination.execute("SELECT DISTINCT article_norm FROM parts")
    }
    for alias, target in EXPLICIT_ARTICLE_EQUIVALENCES:
        alias_norm = normalize_article(alias)
        target_norm = normalize_article(target)
        if target_norm not in available:
            continue
        destination.execute(
            """
            INSERT OR IGNORE INTO article_aliases(
                alias_norm, target_article_norm, alias_kind, source_note
            ) VALUES (?, ?, 'explicit_equivalence', 'approved casting/billet rule')
            """,
            (alias_norm, target_norm),
        )


def _create_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX idx_numbers_norm ON numbers(number_norm);
        CREATE INDEX idx_parts_article_norm ON parts(article_norm);
        CREATE INDEX idx_parts_article_compact ON parts(article_compact);
        CREATE INDEX idx_part_numbers_part_id ON part_numbers(part_id);
        CREATE INDEX idx_part_numbers_part_kind
            ON part_numbers(part_id, number_kind);
        CREATE INDEX idx_part_numbers_number_norm ON part_numbers(number_norm);
        CREATE INDEX idx_aliases_alias_norm ON article_aliases(alias_norm);
        """
    )


def _metadata(
    destination: sqlite3.Connection,
    build_stats: dict[str, int],
) -> None:
    values = {
        "schema_version": str(SCHEMA_VERSION),
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **{key: str(value) for key, value in build_stats.items()},
    }
    destination.executemany(
        "INSERT INTO build_metadata(key, value) VALUES (?, ?)",
        sorted(values.items()),
    )


def backup_database(database_path: Path, backup_dir: Path) -> Path | None:
    database_path = database_path.expanduser().resolve()
    backup_dir = backup_dir.expanduser().resolve()
    if not database_path.is_file():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{database_path.name}.{stamp}.bak"
    shutil.copy2(database_path, backup_path)
    return backup_path


def build_search_database(
    source_path: Path,
    destination_path: Path,
    *,
    backup_dir: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    source_path = source_path.expanduser().resolve()
    destination_path = destination_path.expanduser().resolve()
    temporary_path = destination_path.with_suffix(f"{destination_path.suffix}.tmp")

    if not source_path.is_file():
        raise FileNotFoundError(f"Source database not found: {source_path}")
    if source_path == destination_path:
        raise ValueError("Source and destination databases must be different files")

    temporary_path.unlink(missing_ok=True)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    source_uri = f"{source_path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(temporary_path)) as destination:
                _create_schema(destination)
                destination.executemany(
                    "INSERT INTO sources(id, name) VALUES (?, ?)",
                    source.execute("SELECT id, name FROM sources ORDER BY id"),
                )

                part_rows = tuple(
                    source.execute(
                        "SELECT id, article, category FROM parts ORDER BY id"
                    )
                )
                destination.executemany(
                    """
                    INSERT INTO parts(
                        id, article, article_norm, article_compact, category
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            part_id,
                            article,
                            normalize_article(article),
                            compact_article(article),
                            category,
                        )
                        for part_id, article, category in part_rows
                    ),
                )
                destination.executemany(
                    """
                    INSERT INTO numbers(part_id, source_id, number_norm, number_type)
                    VALUES (?, ?, ?, ?)
                    """,
                    source.execute(
                        """
                        SELECT DISTINCT part_id, source_id, number_norm, number_type
                        FROM numbers
                        ORDER BY part_id, source_id, number_norm, number_type
                        """
                    ),
                )

                build_stats = _insert_reverse_relations(source, destination)
                _insert_aliases(destination)
                _create_indexes(destination)
                _metadata(destination, build_stats)
                destination.execute("ANALYZE")
                destination.commit()

                integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise sqlite3.DatabaseError(
                        f"Generated database failed integrity check: {integrity}"
                    )

        if backup_dir is not None:
            backup_database(destination_path, backup_dir)
        os.replace(temporary_path, destination_path)
        if audit_path is not None:
            write_audit_report(destination_path, audit_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def collect_audit_stats(database_path: Path) -> dict[str, int]:
    database_uri = f"{database_path.expanduser().resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM build_metadata"))
        stats = {
            "unique_articles": connection.execute(
                "SELECT COUNT(DISTINCT article_norm) FROM parts"
            ).fetchone()[0],
            "with_turbo_pn": connection.execute(
                """SELECT COUNT(DISTINCT p.article_norm)
                   FROM part_numbers pn JOIN parts p ON p.id=pn.part_id
                   WHERE pn.number_kind='turbo_pn'"""
            ).fetchone()[0],
            "with_vehicle_oem": connection.execute(
                """SELECT COUNT(DISTINCT p.article_norm)
                   FROM part_numbers pn JOIN parts p ON p.id=pn.part_id
                   WHERE pn.number_kind='vehicle_oem'"""
            ).fetchone()[0],
            "with_component_pn": connection.execute(
                """SELECT COUNT(DISTINCT p.article_norm)
                   FROM part_numbers pn JOIN parts p ON p.id=pn.part_id
                   WHERE pn.number_kind='component_pn'"""
            ).fetchone()[0],
            "with_turbo_and_oem": connection.execute(
                """SELECT COUNT(*) FROM (
                   SELECT p.article_norm
                   FROM part_numbers pn JOIN parts p ON p.id=pn.part_id
                   GROUP BY p.article_norm
                   HAVING SUM(pn.number_kind='turbo_pn')>0
                      AND SUM(pn.number_kind='vehicle_oem')>0)"""
            ).fetchone()[0],
            "without_reverse_links": connection.execute(
                """SELECT COUNT(*) FROM (
                   SELECT article_norm FROM parts GROUP BY article_norm
                   EXCEPT
                   SELECT p.article_norm FROM part_numbers pn
                   JOIN parts p ON p.id=pn.part_id
                   WHERE pn.number_kind IN (
                     'turbo_pn','vehicle_oem','component_pn'))"""
            ).fetchone()[0],
            "unknown_rows": connection.execute(
                "SELECT COUNT(*) FROM part_numbers WHERE number_kind='unknown'"
            ).fetchone()[0],
            "duplicates_removed": int(
                metadata.get("deduplicated_relations", "0")
            ),
            "orphan_relations": connection.execute(
                """SELECT COUNT(*) FROM part_numbers pn
                   LEFT JOIN parts p ON p.id=pn.part_id WHERE p.id IS NULL"""
            ).fetchone()[0],
            "conflicting_relations": connection.execute(
                "SELECT COUNT(*) FROM number_kind_conflicts"
            ).fetchone()[0],
            "alias_rows": connection.execute(
                "SELECT COUNT(*) FROM article_aliases"
            ).fetchone()[0],
            "alias_only_articles": connection.execute(
                """SELECT COUNT(DISTINCT aa.target_article_norm)
                   FROM article_aliases aa
                   WHERE NOT EXISTS (
                     SELECT 1 FROM parts p WHERE p.article_norm=aa.alias_norm)"""
            ).fetchone()[0],
            "ambiguous_compact_keys": connection.execute(
                """SELECT COUNT(*) FROM (
                   SELECT article_compact FROM parts
                   GROUP BY article_compact
                   HAVING COUNT(DISTINCT article_norm)>1)"""
            ).fetchone()[0],
        }
        return stats


def write_audit_report(database_path: Path, audit_path: Path) -> None:
    database_path = database_path.expanduser().resolve()
    audit_path = audit_path.expanduser().resolve()
    stats = collect_audit_stats(database_path)
    uri = f"{database_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        conflicts = connection.execute(
            """SELECT article_norm, number, observed_kinds, resolution
               FROM number_kind_conflicts
               ORDER BY article_norm, number LIMIT 100"""
        ).fetchall()
        ambiguous = connection.execute(
            """SELECT article_compact, GROUP_CONCAT(DISTINCT article_norm)
               FROM parts GROUP BY article_compact
               HAVING COUNT(DISTINCT article_norm)>1
               ORDER BY article_compact LIMIT 100"""
        ).fetchall()

    lines = [
        "# Аудит данных обратного поиска",
        "",
        f"Сформирован: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "## Сводка",
        "",
        f"- Уникальных нормализованных артикулов: {stats['unique_articles']}",
        f"- Артикулов с Turbo P/N: {stats['with_turbo_pn']}",
        f"- Артикулов с Vehicle OE/OEM: {stats['with_vehicle_oem']}",
        f"- Артикулов с OEM/P/N детали: {stats['with_component_pn']}",
        f"- Артикулов одновременно с Turbo P/N и OEM: {stats['with_turbo_and_oem']}",
        f"- Артикулов без достоверных обратных связей: {stats['without_reverse_links']}",
        f"- Связей `unknown`: {stats['unknown_rows']}",
        f"- Повторных свидетельств, удалённых дедупликацией: {stats['duplicates_removed']}",
        f"- Осиротевших связей: {stats['orphan_relations']}",
        f"- Связей с конфликтующими типами: {stats['conflicting_relations']}",
        f"- Явных alias/equivalence: {stats['alias_rows']}",
        f"- Артикулов, доступных только через alias: {stats['alias_only_articles']}",
        f"- Неоднозначных compact-ключей: {stats['ambiguous_compact_keys']}",
        "",
        "## Семантические правила",
        "",
        "Классификация выполняется по источнику и смыслу каталожного поля. "
        "Старые `numbers` сохранены для прямого поиска; обратный поиск использует "
        "отдельную `part_numbers`. Токены модели, двигателя и размеров не "
        "показываются как Turbo P/N или Vehicle OEM.",
        "",
        "## Известное ограничение локальных данных",
        "",
        "В имеющейся исходной SQLite нет каталога Gasket Kits и нет полной строки "
        "`GK-0552`: присутствуют только Turbo P/N `454163-0001` и внешний JRONE "
        "`2090-505-034`. Номера `454163-0002` и `2 505 034` не добавлены в "
        "рабочую базу догадкой. Полная ожидаемая семантика проверяется отдельной "
        "тестовой fixture до появления локального исходного каталога.",
        "",
        "## Конфликтующие типы (первые 100)",
        "",
    ]
    if conflicts:
        lines.extend(
            f"- `{article}` / `{number}`: {kinds}; итог `{resolution}`"
            for article, number, kinds, resolution in conflicts
        )
    else:
        lines.append("Конфликтов равного приоритета нет.")
    lines.extend(["", "## Неоднозначные compact-ключи (первые 100)", ""])
    if ambiguous:
        lines.extend(
            f"- `{key}`: {articles}" for key, articles in ambiguous
        )
    else:
        lines.append("Неоднозначных compact-ключей нет.")
    lines.extend(
        [
            "",
            "## Принцип безопасности",
            "",
            "Сомнительные записи остаются `unknown` и не попадают в стандартную "
            "выдачу. Исходные PDF не изменялись, интернет для дополнения номеров "
            "не использовался.",
            "",
        ]
    )
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = audit_path.with_suffix(f"{audit_path.suffix}.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, audit_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the compact SQLite database used by the Telegram bot."
    )
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent / "turbo_parts.sqlite",
    )
    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent / "turbo_search.sqlite",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Copy an existing destination here before atomic replacement.",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=Path(__file__).resolve().parent / "docs/reverse_search_audit.md",
    )
    args = parser.parse_args()

    build_search_database(
        args.source,
        args.destination,
        backup_dir=args.backup_dir,
        audit_path=args.audit,
    )
    print(f"Created {args.destination.resolve()} ({args.destination.stat().st_size} bytes)")
    print(f"Audit: {args.audit.resolve()}")


if __name__ == "__main__":
    main()

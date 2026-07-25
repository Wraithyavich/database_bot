from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def build_search_database(source_path: Path, destination_path: Path) -> None:
    source_path = source_path.expanduser().resolve()
    destination_path = destination_path.expanduser().resolve()
    temporary_path = destination_path.with_suffix(f"{destination_path.suffix}.tmp")

    if not source_path.is_file():
        raise FileNotFoundError(f"Source database not found: {source_path}")

    temporary_path.unlink(missing_ok=True)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    source_uri = f"{source_path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(temporary_path)) as destination:
                destination.executescript(
                    """
                    PRAGMA journal_mode = OFF;
                    PRAGMA synchronous = OFF;
                    PRAGMA temp_store = MEMORY;

                    CREATE TABLE sources(
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL
                    );

                    CREATE TABLE parts(
                        id INTEGER PRIMARY KEY,
                        article TEXT NOT NULL,
                        category TEXT NOT NULL
                    );

                    CREATE TABLE numbers(
                        part_id INTEGER NOT NULL,
                        source_id INTEGER NOT NULL,
                        number_norm TEXT NOT NULL,
                        number_type TEXT NOT NULL
                    );
                    """
                )

                destination.executemany(
                    "INSERT INTO sources(id, name) VALUES (?, ?)",
                    source.execute("SELECT id, name FROM sources ORDER BY id"),
                )
                destination.executemany(
                    "INSERT INTO parts(id, article, category) VALUES (?, ?, ?)",
                    source.execute(
                        "SELECT id, article, category FROM parts ORDER BY id"
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
                destination.execute(
                    "CREATE INDEX idx_numbers_norm ON numbers(number_norm)"
                )
                destination.execute("ANALYZE")
                destination.commit()

                integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise sqlite3.DatabaseError(
                        f"Generated database failed integrity check: {integrity}"
                    )

        os.replace(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)


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
    args = parser.parse_args()

    build_search_database(args.source, args.destination)
    print(f"Created {args.destination.resolve()} ({args.destination.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

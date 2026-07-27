from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

from vin_search import VinStore
from vin_unresolved import UnresolvedVin, UnresolvedVinStore


BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the VIN verification queue")
    parser.add_argument(
        "command",
        choices=(
            "pending",
            "stats",
            "unresolved",
            "unresolved-stats",
            "export-unresolved",
            "observer-attempts",
        ),
        help="Queue operation to run",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("VIN_DATABASE_PATH", BASE_DIR / "vin_cache.sqlite"),
        help="Path to the writable VIN SQLite database",
    )
    parser.add_argument(
        "--unresolved-database",
        default=os.environ.get(
            "VIN_UNRESOLVED_DATABASE_PATH",
            BASE_DIR / "vin_unresolved.sqlite",
        ),
        help="Path to the separate unresolved VIN SQLite database",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--vin",
        help="Optional VIN filter for observer-attempts",
    )
    parser.add_argument(
        "--output",
        help="CSV path for export-unresolved (defaults next to its database)",
    )
    args = parser.parse_args()

    if (
        args.command.startswith("unresolved")
        or args.command == "export-unresolved"
        or args.command == "observer-attempts"
    ):
        unresolved_store = UnresolvedVinStore(args.unresolved_database)
        unresolved_store.initialize()
        if args.command == "observer-attempts":
            for attempt in unresolved_store.list_observer_attempts(
                vin=args.vin,
                limit=args.limit,
            ):
                print(
                    json.dumps(
                        {
                            "id": attempt.id,
                            "vin": attempt.vin,
                            "attempted_at": attempt.attempted_at,
                            "stage": attempt.stage,
                            "status": attempt.status,
                            "summary": attempt.summary,
                            "checked_sources": attempt.checked_sources,
                            "report": attempt.report,
                        },
                        ensure_ascii=False,
                    )
                )
            return

        if args.command == "unresolved-stats":
            stats = unresolved_store.stats()
            print(
                f"unique_vins={stats.unique_vins} "
                f"requests={stats.requests}"
            )
            return

        unresolved = unresolved_store.list(limit=args.limit)
        if args.command == "export-unresolved":
            output = (
                Path(args.output)
                if args.output
                else unresolved_store.path.with_name(
                    "vin_unresolved_export.csv"
                )
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            _export_unresolved_csv(output, unresolved)
            print(f"exported={len(unresolved)} path={output}")
            return

        for item in unresolved:
            details = " | ".join(
                value
                for value in (
                    item.vin,
                    f"requests={item.request_count}",
                    f"reason={item.failure_code}",
                    item.make,
                    item.model,
                    item.model_year,
                    item.engine,
                    (
                        f"provider={item.online_search_provider}"
                        if item.online_search_provider
                        else ""
                    ),
                    f"last={item.last_requested_at}",
                )
                if value
            )
            print(details)
        return

    store = VinStore(args.database)
    store.initialize(seed_path=BASE_DIR / "vin_verified.json")

    if args.command == "stats":
        stats = store.stats()
        print(
            f"verified={stats.verified} "
            f"pending={stats.pending} "
            f"requests={stats.requests}"
        )
        return

    for record in store.pending(limit=args.limit):
        candidate_numbers = sorted(
            {
                number
                for fitment in record.fitments
                for number in (*fitment.oem_numbers, *fitment.turbo_numbers)
            }
        )
        search_state = (
            f"online={record.online_search_at}"
            if record.online_search_at
            else "online=not-searched"
        )
        details = " | ".join(
            value
            for value in (
                record.vin,
                search_state,
                record.make,
                record.model,
                record.model_year,
                record.engine,
                (
                    f"candidates={','.join(candidate_numbers)}"
                    if candidate_numbers
                    else ""
                ),
            )
            if value
        )
        print(details)


def _export_unresolved_csv(
    output: Path,
    unresolved: tuple[UnresolvedVin, ...],
) -> None:
    fieldnames = (
        "vin",
        "request_count",
        "failure_code",
        "failure_detail",
        "make",
        "model",
        "model_year",
        "engine",
        "power_kw",
        "online_search_provider",
        "online_search_at",
        "first_requested_at",
        "last_requested_at",
    )
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for item in unresolved:
            writer.writerow(
                {field: getattr(item, field) for field in fieldnames}
            )


if __name__ == "__main__":
    main()

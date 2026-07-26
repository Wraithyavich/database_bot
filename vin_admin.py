from __future__ import annotations

import argparse
import os
from pathlib import Path

from vin_search import VinStore


BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the VIN verification queue")
    parser.add_argument(
        "command",
        choices=("pending", "stats"),
        help="Queue operation to run",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("VIN_DATABASE_PATH", BASE_DIR / "vin_cache.sqlite"),
        help="Path to the writable VIN SQLite database",
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()

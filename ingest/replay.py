"""Reprocess the messages parked in ``failed_uplinks``.

Usage: python -m ingest.replay [--limit N]

Every pending row is run through the same path as a live message. A row that stores
correctly is marked ``replayed_at``; one that fails again keeps its place with the new
error and an incremented ``attempts``. Replaying twice is safe: the uplinks insert has
ON CONFLICT DO NOTHING. Exit code 1 if any row is still failing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ingest.sinks import Sinks
from telemetry import chirpstack_event

log = logging.getLogger("replay")


def replay(sinks: Sinks, limit: int | None = None) -> tuple[int, int]:
    """Return (replayed, still failing)."""
    conn = sinks._pg()
    sql = "SELECT id, payload FROM failed_uplinks WHERE replayed_at IS NULL ORDER BY id"
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        sql += f" LIMIT {int(limit)}"
    pending = conn.execute(sql).fetchall()
    done = failing = 0
    for row_id, payload in pending:
        try:
            sinks.store(chirpstack_event.extract(json.loads(bytes(payload))))
        except Exception as exc:  # noqa: BLE001
            failing += 1
            log.warning("row %d still failing: %s: %s", row_id, type(exc).__name__, exc)
            conn.execute(
                "UPDATE failed_uplinks SET attempts = attempts + 1, error = %s WHERE id = %s",
                (f"{type(exc).__name__}: {exc}", row_id),
            )
        else:
            done += 1
            conn.execute("UPDATE failed_uplinks SET replayed_at = now() WHERE id = %s", (row_id,))
    return done, failing


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None, help="replay at most N rows")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    done, failing = replay(Sinks(), args.limit)
    print(f"replayed {done}, still failing {failing}")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())

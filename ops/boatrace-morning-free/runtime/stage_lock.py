from __future__ import annotations

import argparse
import json
import os
import signal
import time
from datetime import date
from pathlib import Path
from typing import Any

import psycopg


LOCK_NAMESPACE = "boatrace-morning-stage-lock-v1"
LOCK_BUSY = 10
LOCK_ERROR = 20


def lock_identity(target_date: date, stage: str) -> str:
    return f"{LOCK_NAMESPACE}:{target_date.isoformat()}:{stage}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--stage", choices=("EARLY", "LATE"), required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--release-file", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=2400)
    args = parser.parse_args(argv)

    target_date = date.fromisoformat(args.target_date)
    ready_file = Path(args.ready_file)
    release_file = Path(args.release_file)
    identity = lock_identity(target_date, args.stage)
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    acquired = False
    lock_key: int | None = None
    started = time.monotonic()

    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT hashtextextended(%s, 0), "
                    "pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (identity, identity),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("stage lock query returned no row")
                lock_key = int(row[0])
                acquired = bool(row[1])

            if not acquired:
                _write_json(
                    ready_file,
                    {
                        "schema_version": "boatrace-morning-stage-lock-v1",
                        "status": "LOCK_BUSY",
                        "target_date": target_date.isoformat(),
                        "stage": args.stage,
                        "lock_key": lock_key,
                    },
                )
                return LOCK_BUSY

            _write_json(
                ready_file,
                {
                    "schema_version": "boatrace-morning-stage-lock-v1",
                    "status": "LOCK_ACQUIRED",
                    "target_date": target_date.isoformat(),
                    "stage": args.stage,
                    "lock_key": lock_key,
                },
            )

            while not release_file.exists() and not stop_requested:
                if time.monotonic() - started >= args.timeout_seconds:
                    raise TimeoutError("stage lock holder timed out")
                time.sleep(0.25)

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (identity,),
                )
                released = bool(cursor.fetchone()[0])
                if not released:
                    raise RuntimeError("stage lock was not held at release")
            acquired = False
            return 0
    except Exception as error:
        _write_json(
            ready_file,
            {
                "schema_version": "boatrace-morning-stage-lock-v1",
                "status": "LOCK_ERROR",
                "target_date": target_date.isoformat(),
                "stage": args.stage,
                "lock_key": lock_key,
                "error_type": type(error).__name__,
            },
        )
        return LOCK_ERROR
    finally:
        # Closing the database session releases a session-level advisory lock,
        # including abnormal exits where the explicit unlock did not run.
        acquired = False


if __name__ == "__main__":
    raise SystemExit(main())


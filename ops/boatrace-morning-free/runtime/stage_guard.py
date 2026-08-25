from __future__ import annotations

import argparse
import json
import os
from datetime import date
from typing import Any, Callable


PROCEED = 0
SKIP_ALREADY_STARTED = 10
CHECK_UNAVAILABLE = 20

_STAGE_STARTED_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM ux_app.run_executions AS execution
    JOIN LATERAL (
        SELECT event.lifecycle_status, event.event_at
        FROM ux_app.run_events AS event
        WHERE event.run_execution_id = execution.id
        ORDER BY event.event_seq DESC
        LIMIT 1
    ) AS latest ON true
    WHERE execution.target_date = %s
      AND execution.stage = %s
      AND (
          latest.lifecycle_status = 'SUCCEEDED'
          OR (
              latest.lifecycle_status = 'RUNNING'
              AND latest.event_at >= clock_timestamp() - interval '45 minutes'
          )
      )
) AS stage_started
"""

_CREDENTIAL_SCHEMA_SQL = """
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE lower(table_schema || '.' || table_name || '.' || column_name)
      ~ '(cloudflare|oauth|credential|secret|token|vault|wrangler)'
ORDER BY table_schema, table_name, ordinal_position
"""


def stage_started(connection: Any, target_date: date, stage: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(_STAGE_STARTED_SQL, (target_date, stage))
        row = cursor.fetchone()
    return bool(row and row[0])


def credential_schema_matches(connection: Any) -> list[dict[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(_CREDENTIAL_SCHEMA_SQL)
        rows = cursor.fetchall()
    return [
        {
            "table_schema": str(row[0]),
            "table_name": str(row[1]),
            "column_name": str(row[2]),
            "data_type": str(row[3]),
        }
        for row in rows
    ]


def _result(*, target_date: date, stage: str, status: str) -> str:
    return json.dumps(
        {
            "schema_version": "boatrace-morning-stage-guard-v2",
            "target_date": target_date.isoformat(),
            "stage": stage,
            "status": status,
        },
        ensure_ascii=False,
    )


def main(
    argv: list[str] | None = None,
    *,
    connect_factory: Callable[[str], Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--stage", choices=("EARLY", "LATE"), required=True)
    args = parser.parse_args(argv)
    target_date = date.fromisoformat(args.target_date)

    if connect_factory is None:
        try:
            import psycopg
        except Exception:
            print(
                _result(
                    target_date=target_date,
                    stage=args.stage,
                    status="CHECK_UNAVAILABLE",
                )
            )
            return CHECK_UNAVAILABLE
        connect_factory = psycopg.connect

    try:
        with connect_factory(os.environ["DATABASE_URL"]) as connection:
            schema_matches = credential_schema_matches(connection)
            already_started = stage_started(connection, target_date, args.stage)
    except Exception:
        print(
            _result(
                target_date=target_date,
                stage=args.stage,
                status="CHECK_UNAVAILABLE",
            )
        )
        return CHECK_UNAVAILABLE

    print(
        json.dumps(
            {
                "schema_version": "cloudflare-vault-schema-probe-v1",
                "match_count": len(schema_matches),
                "matches": schema_matches,
                "values_read": False,
                "secret_value_recorded": False,
            },
            ensure_ascii=False,
        )
    )

    if already_started:
        print(
            _result(
                target_date=target_date,
                stage=args.stage,
                status="SKIP_ALREADY_STARTED",
            )
        )
        return SKIP_ALREADY_STARTED

    print(_result(target_date=target_date, stage=args.stage, status="PROCEED"))
    return PROCEED


if __name__ == "__main__":
    raise SystemExit(main())

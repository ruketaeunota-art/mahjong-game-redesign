from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg


PENDING_STATUSES = {"QUEUED", "LEASED", "RETRY"}
ACCEPTED_STATUSES = {"ACCEPTED", "SENT", "DELIVERED"}
FAILED_STATUSES = {"FAILED", "EXPIRED", "SUPPRESSED"}


def _safe_snapshot(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    allowed = (
        "schema_version",
        "run_execution_id",
        "outbox_id",
        "inserted",
        "deduplicated",
        "replayed",
        "canonical_run_execution_id",
        "logical_target_date",
        "logical_stage",
        "status",
        "presentation",
        "ui_version",
        "payload_sha256",
    )
    return {key: raw.get(key) for key in allowed if key in raw}


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_accepted(
    snapshot: dict[str, Any],
    *,
    expected_skip_count: int,
) -> None:
    outbox_id = snapshot.get("outbox_id")
    if not isinstance(outbox_id, int) or outbox_id <= 0:
        raise RuntimeError("delivery snapshot has no valid outbox id")

    payload_sha256 = snapshot.get("payload_sha256")
    if not isinstance(payload_sha256, str) or len(payload_sha256.strip()) != 64:
        raise RuntimeError("delivery snapshot has no valid payload hash")

    presentation = snapshot.get("presentation")
    ui_version = snapshot.get("ui_version")
    if expected_skip_count > 0:
        if presentation != "MORNING_CARD_V2":
            raise RuntimeError("skip-detail digest was not enriched to MORNING_CARD_V2")
        if ui_version != "MORNING_USER_CARD_V3":
            raise RuntimeError("skip-detail digest has an unexpected UI version")
    elif presentation not in {"MORNING_CARD_V1", "MORNING_CARD_V2"}:
        raise RuntimeError("Morning digest has an unexpected presentation")


def _resolve_existing_run(
    connection: psycopg.Connection[Any],
    *,
    target_date: date,
    stage: str,
) -> tuple[int, int, bool]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                execution.id,
                GREATEST(
                    terminal.actionable_count - terminal.s_count - terminal.a_count,
                    0
                ) AS expected_skip_count,
                terminal.details ->> 'skip_audit_version' =
                    'morning-skip-audit-v1' AS skip_audit_enabled
            FROM ux_app.run_executions AS execution
            JOIN LATERAL (
                SELECT event.*
                FROM ux_app.run_events AS event
                WHERE event.run_execution_id = execution.id
                ORDER BY event.event_seq DESC
                LIMIT 1
            ) AS terminal ON true
            WHERE execution.target_date = %s
              AND execution.stage = %s
              AND terminal.lifecycle_status = 'SUCCEEDED'
            ORDER BY execution.id DESC
            LIMIT 1
            """,
            (target_date, stage),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("no successful Morning run exists for the logical stage")
    return int(row[0]), int(row[1]), bool(row[2])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id", type=int)
    source.add_argument("--target-date")
    parser.add_argument("--stage", choices=("EARLY", "LATE"))
    parser.add_argument("--expected-skip-count", type=int)
    parser.add_argument("--allow-legacy", action="store_true")
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=150)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)

    if args.run_id is not None and args.expected_skip_count is None:
        parser.error("--expected-skip-count is required with --run-id")
    if args.target_date is not None and args.stage is None:
        parser.error("--stage is required with --target-date")

    evidence_path = Path(args.evidence_file)
    started_at = datetime.now(timezone.utc)
    deadline = time.monotonic() + max(args.timeout_seconds, 1)
    attempts = 0
    last_snapshot: dict[str, Any] | None = None
    status = "STARTED"
    error_code: str | None = None
    error_type: str | None = None
    run_id = args.run_id
    expected_skip_count = args.expected_skip_count
    resolved_target_date = args.target_date
    resolved_stage = args.stage
    legacy_run = False

    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
            if run_id is None:
                target_date = date.fromisoformat(str(args.target_date))
                run_id, expected_skip_count, skip_audit_enabled = _resolve_existing_run(
                    connection,
                    target_date=target_date,
                    stage=str(args.stage),
                )
                if not skip_audit_enabled:
                    if not args.allow_legacy:
                        raise RuntimeError("existing Morning run predates skip-audit contract")
                    legacy_run = True
                    expected_skip_count = 0

            if run_id is None or expected_skip_count is None:
                raise RuntimeError("delivery gate run identity is incomplete")

            while True:
                attempts += 1
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT ux_app.append_morning_digest_v1(%s)",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                if row is None or not isinstance(row[0], dict):
                    raise RuntimeError("Morning digest status function returned no payload")

                last_snapshot = row[0]
                delivery_status = last_snapshot.get("status")

                if delivery_status in ACCEPTED_STATUSES:
                    _validate_accepted(
                        last_snapshot,
                        expected_skip_count=expected_skip_count,
                    )
                    status = "PASS_ACCEPTED_LEGACY" if legacy_run else "PASS_ACCEPTED"
                    break

                if delivery_status in FAILED_STATUSES:
                    status = "FAILED_TERMINAL"
                    error_code = f"OUTBOX_{delivery_status}"
                    break

                if delivery_status not in PENDING_STATUSES:
                    raise RuntimeError(
                        f"Morning digest has unexpected status {delivery_status!r}"
                    )

                if time.monotonic() >= deadline:
                    status = "TIMEOUT_PENDING"
                    error_code = "DELIVERY_ACCEPTANCE_TIMEOUT"
                    break
                time.sleep(max(args.poll_seconds, 0.25))
    except Exception as error:
        status = "FAILED_GATE"
        error_code = "DELIVERY_GATE_ERROR"
        error_type = type(error).__name__

    payload = {
        "schema_version": "boatrace-morning-delivery-gate-v2",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "started_at_utc": started_at.isoformat(),
        "run_execution_id": run_id,
        "target_date": resolved_target_date,
        "stage": resolved_stage,
        "expected_skip_count": expected_skip_count,
        "legacy_run": legacy_run,
        "attempts": attempts,
        "status": status,
        "error_code": error_code,
        "error_type": error_type,
        "snapshot": _safe_snapshot(last_snapshot),
    }
    _write_evidence(evidence_path, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    if status in {"PASS_ACCEPTED", "PASS_ACCEPTED_LEGACY"}:
        return 0
    if status == "TIMEOUT_PENDING":
        return 30
    if status == "FAILED_TERMINAL":
        return 31
    return 32


if __name__ == "__main__":
    raise SystemExit(main())

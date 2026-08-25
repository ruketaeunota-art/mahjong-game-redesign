from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--expected-skip-count", type=int, required=True)
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=150)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)

    evidence_path = Path(args.evidence_file)
    started_at = datetime.now(timezone.utc)
    deadline = time.monotonic() + max(args.timeout_seconds, 1)
    attempts = 0
    last_snapshot: dict[str, Any] | None = None
    status = "STARTED"
    error_code: str | None = None
    error_type: str | None = None

    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
            while True:
                attempts += 1
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT ux_app.append_morning_digest_v1(%s)",
                        (args.run_id,),
                    )
                    row = cursor.fetchone()
                if row is None or not isinstance(row[0], dict):
                    raise RuntimeError("Morning digest status function returned no payload")

                last_snapshot = row[0]
                delivery_status = last_snapshot.get("status")

                if delivery_status in ACCEPTED_STATUSES:
                    _validate_accepted(
                        last_snapshot,
                        expected_skip_count=args.expected_skip_count,
                    )
                    status = "PASS_ACCEPTED"
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
        "schema_version": "boatrace-morning-delivery-gate-v1",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "started_at_utc": started_at.isoformat(),
        "run_execution_id": args.run_id,
        "expected_skip_count": args.expected_skip_count,
        "attempts": attempts,
        "status": status,
        "error_code": error_code,
        "error_type": error_type,
        "snapshot": _safe_snapshot(last_snapshot),
    }
    _write_evidence(evidence_path, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    if status == "PASS_ACCEPTED":
        return 0
    if status == "TIMEOUT_PENDING":
        return 30
    if status == "FAILED_TERMINAL":
        return 31
    return 32


if __name__ == "__main__":
    raise SystemExit(main())

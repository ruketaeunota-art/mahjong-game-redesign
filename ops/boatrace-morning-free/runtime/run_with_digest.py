from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from typing import Any

import production  # noqa: F401  # applies the production parser/scoring overlays
import runner

_original_persist = runner.persist

_ACTIVE_OUTBOX_STATUSES = {'QUEUED', 'LEASED', 'RETRY', 'ACCEPTED'}
_DEDUPLICATED_TERMINAL_OUTBOX_STATUSES = {'FAILED', 'EXPIRED', 'SUPPRESSED'}
def _validate_outbox_result(
    digest: dict[str, Any],
    *,
    expected_kind: str,
) -> dict[str, Any]:
    outbox_id = digest.get('outbox_id')
    status = digest.get('status')
    deduplicated = digest.get('deduplicated') is True
    allowed_statuses = set(_ACTIVE_OUTBOX_STATUSES)
    replayed = digest.get('replayed') is True
    if deduplicated or replayed:
        allowed_statuses |= _DEDUPLICATED_TERMINAL_OUTBOX_STATUSES

    if not outbox_id or status not in allowed_statuses:
        raise RuntimeError(
            f'{expected_kind}_APPEND_INVALID:{outbox_id}:{status}'
        )

    return {
        'outbox_id': int(outbox_id),
        'status': str(status),
        'deduplicated': deduplicated,
        'replayed': replayed,
        'canonical_run_execution_id': int(
            digest.get('canonical_run_execution_id')
            or digest.get('run_execution_id')
            or 0
        ),
    }


def persist_with_digest(
    cur: Any,
    rid: int,
    target: date,
    stage: str,
    scored: list[dict[str, Any]],
    source_failures: list[str],
    now: datetime,
) -> dict[str, Any]:
    """Persist the terminal run state and its LINE digest in one transaction."""
    result = _original_persist(
        cur,
        rid,
        target,
        stage,
        scored,
        source_failures,
        now,
    )
    cur.execute(
        "SELECT ux_app.append_morning_digest_v1(%s)",
        (rid,),
    )
    row = cur.fetchone()
    if not row or not isinstance(row[0], dict):
        raise RuntimeError('MORNING_DIGEST_APPEND_RESULT_MISSING')

    digest = _validate_outbox_result(row[0], expected_kind='MORNING_DIGEST')

    return {
        **result,
        'digest_outbox_id': digest['outbox_id'],
        'digest_status': digest['status'],
        'digest_disposition': (
            'DEDUPLICATED'
            if digest['deduplicated']
            else ('APPENDED' if row[0].get('inserted') else 'REPLAY')
        ),
        'digest_canonical_run_execution_id': (
            digest['canonical_run_execution_id'] or rid
        ),
    }


def _append_terminal_failure(
    cur: Any,
    rid: int,
    error_code: str,
    failed_at: datetime,
) -> dict[str, Any]:
    """Append one sealed failure terminal/outbox for a committed RUNNING run."""
    cur.execute(
        "SELECT ux_app.append_morning_run_failure_v1(%s,%s,%s)",
        (rid, error_code, failed_at),
    )
    row = cur.fetchone()
    if not row or not isinstance(row[0], dict):
        raise RuntimeError('RUN_FAILURE_APPEND_RESULT_MISSING')

    failure = _validate_outbox_result(row[0], expected_kind='RUN_FAILURE')
    return {
        'failure_outbox_id': failure['outbox_id'],
        'failure_status': failure['status'],
        'failure_disposition': (
            'DEDUPLICATED'
            if failure['deduplicated']
            else ('REPLAY' if failure['replayed'] else 'APPENDED')
        ),
        'failure_canonical_run_execution_id': (
            failure['canonical_run_execution_id'] or rid
        ),
    }


class _SourceAcquisitionFailure(RuntimeError):
    pass


def _emit_unknown_outcome(
    *,
    rid: int,
    target: date,
    stage: str,
    error_code: str,
) -> int:
    """Record a bounded unknown outcome without generating another plan."""
    print(
        json.dumps(
            {
                'run_id': rid,
                'target_date': target.isoformat(),
                'stage': stage,
                'status': 'UNKNOWN',
                'error_code': error_code,
            },
            ensure_ascii=False,
        )
    )
    return 1


def _commit_failure_or_report_unknown(
    conn: Any,
    cur: Any,
    *,
    rid: int,
    target: date,
    stage: str,
    error_code: str,
) -> dict[str, Any] | None:
    """Save the sole failure plan, never replacing an unknown outcome."""
    try:
        failure_result = _append_terminal_failure(
            cur,
            rid,
            error_code,
            datetime.now(runner.JST),
        )
    except Exception:
        _emit_unknown_outcome(
            rid=rid,
            target=target,
            stage=stage,
            error_code='RUN_FAILURE_OUTCOME_UNKNOWN',
        )
        return None

    try:
        conn.commit()
    except Exception:
        _emit_unknown_outcome(
            rid=rid,
            target=target,
            stage=stage,
            error_code='RUN_FAILURE_COMMIT_OUTCOME_UNKNOWN',
        )
        return None

    return failure_result


def main() -> int:
    """Run the frozen scorer with auditable pre-acquisition failure handling."""
    ap = argparse.ArgumentParser()
    ap.add_argument('--target-date')
    ap.add_argument('--stage', choices=['EARLY', 'LATE'], required=True)
    ap.add_argument('--scheduled-for')
    args = ap.parse_args()

    now = datetime.now(runner.JST)
    target = (
        date.fromisoformat(args.target_date)
        if args.target_date
        else now.date()
    )
    scheduled = (
        datetime.fromisoformat(args.scheduled_for)
        if args.scheduled_for
        else now
    )
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=runner.JST)

    with runner.psycopg.connect(os.environ['DATABASE_URL']) as conn:
        with conn.cursor() as cur:
            # Commit the immutable RUNNING identity before any official-source
            # request.  A later parser/network failure can now be recorded
            # without inventing a second run or exposing raw exception text.
            rid = runner.create_run(cur, target, args.stage, scheduled)
            try:
                conn.commit()
            except Exception:
                return _emit_unknown_outcome(
                    rid=rid,
                    target=target,
                    stage=args.stage,
                    error_code='RUNNING_IDENTITY_COMMIT_OUTCOME_UNKNOWN',
                )

            try:
                races, failures = runner.acquire(target)
                if not races:
                    raise _SourceAcquisitionFailure(
                        'no complete official races acquired'
                    )
            except Exception:
                failure_result = _commit_failure_or_report_unknown(
                    conn,
                    cur,
                    rid=rid,
                    target=target,
                    stage=args.stage,
                    error_code='SOURCE_ACQUISITION_FAILED',
                )
                if failure_result is None:
                    return 1
                print(
                    json.dumps(
                        {
                            'run_id': rid,
                            'target_date': target.isoformat(),
                            'stage': args.stage,
                            'status': 'FAILED',
                            'error_code': 'SOURCE_ACQUISITION_FAILED',
                            'result': failure_result,
                        },
                        ensure_ascii=False,
                    )
                )
                return 1

            try:
                v2, v21 = runner.load_config(cur)
                scored = [
                    runner.score_race(race, v2, v21)
                    for race in races
                ]
            except Exception:
                conn.rollback()
                failure_result = _commit_failure_or_report_unknown(
                    conn,
                    cur,
                    rid=rid,
                    target=target,
                    stage=args.stage,
                    error_code='SCORER_FAILED',
                )
                if failure_result is None:
                    return 1
                print(
                    json.dumps(
                        {
                            'run_id': rid,
                            'target_date': target.isoformat(),
                            'stage': args.stage,
                            'status': 'FAILED',
                            'error_code': 'SCORER_FAILED',
                            'result': failure_result,
                        },
                        ensure_ascii=False,
                    )
                )
                return 1

            try:
                result = runner.persist(
                    cur,
                    rid,
                    target,
                    args.stage,
                    scored,
                    failures,
                    now,
                )
            except Exception:
                # A statement failure is known to have rolled back, so the
                # committed RUNNING identity may receive its one failure plan.
                conn.rollback()
                failure_result = _commit_failure_or_report_unknown(
                    conn,
                    cur,
                    rid=rid,
                    target=target,
                    stage=args.stage,
                    error_code='PERSISTENCE_FAILED',
                )
                if failure_result is None:
                    return 1
                print(
                    json.dumps(
                        {
                            'run_id': rid,
                            'target_date': target.isoformat(),
                            'stage': args.stage,
                            'status': 'FAILED',
                            'error_code': 'PERSISTENCE_FAILED',
                            'result': failure_result,
                        },
                        ensure_ascii=False,
                    )
                )
                return 1

            try:
                conn.commit()
            except Exception:
                # A commit error is not proof that the success plan is absent.
                # Keep the exact run identity for recovery; never add a
                # divergent RUN_FAILURE plan after an unknown success outcome.
                return _emit_unknown_outcome(
                    rid=rid,
                    target=target,
                    stage=args.stage,
                    error_code='SUCCESS_COMMIT_OUTCOME_UNKNOWN',
                )

    print(
        json.dumps(
            {
                'run_id': rid,
                'target_date': target.isoformat(),
                'stage': args.stage,
                'result': result,
                'source_failures': len(failures),
            },
            ensure_ascii=False,
        )
    )
    return 0


runner.persist = persist_with_digest

if __name__ == '__main__':
    raise SystemExit(main())

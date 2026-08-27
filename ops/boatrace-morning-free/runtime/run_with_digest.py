from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime
from typing import Any

import late_diff
import production  # noqa: F401  # applies the production parser/scoring overlays
import runner
import audit_overlay  # noqa: F401  # installs skip-audit persistence overlay

_original_persist = runner.persist

_ACTIVE_OUTBOX_STATUSES = {'QUEUED', 'LEASED', 'RETRY', 'ACCEPTED'}
_DEDUPLICATED_TERMINAL_OUTBOX_STATUSES = {'FAILED', 'EXPIRED', 'SUPPRESSED'}


def _validate_outbox_result(
    digest: dict[str, Any],
    *,
    expected_kind: str,
    expected_run_execution_id: int | None = None,
) -> dict[str, Any]:
    suppressed = digest.get('suppressed') is True
    if suppressed:
        if (
            expected_kind != 'MORNING_DIGEST'
            or not late_diff.validate_suppression_result(
                digest,
                expected_run_execution_id=expected_run_execution_id,
            )
        ):
            raise RuntimeError(f'{expected_kind}_SUPPRESSION_INVALID')
        baseline_outbox_id = digest.get('baseline_outbox_id')
        return {
            'outbox_id': None,
            'baseline_outbox_id': (
                int(baseline_outbox_id)
                if isinstance(baseline_outbox_id, int)
                else None
            ),
            'decision_id': (
                int(digest['decision_id'])
                if isinstance(digest.get('decision_id'), int)
                else None
            ),
            'status': late_diff.SUPPRESSION_STATUS,
            'deduplicated': True,
            'replayed': digest['replayed'],
            'suppressed': True,
            'decision_reason_code': digest['decision_reason_code'],
            'canonical_run_execution_id': int(digest['run_execution_id']),
        }

    if (
        expected_kind == 'MORNING_DIGEST'
        and digest.get('schema_version')
        == 'boatrace-morning-digest-outbox-result-v4'
    ):
        if digest.get('schema_version') != 'boatrace-morning-digest-outbox-result-v4':
            raise RuntimeError('MORNING_DIGEST_V2_SCHEMA_INVALID')
        run_id = digest.get('run_execution_id')
        decision_id = digest.get('decision_id')
        outbox_id = digest.get('outbox_id')
        status = digest.get('status')
        deduplicated = digest.get('deduplicated') is True
        replayed = digest.get('replayed') is True
        allowed_statuses = set(_ACTIVE_OUTBOX_STATUSES)
        if deduplicated or replayed:
            allowed_statuses |= _DEDUPLICATED_TERMINAL_OUTBOX_STATUSES
        if (
            not isinstance(run_id, int)
            or run_id <= 0
            or (
                expected_run_execution_id is not None
                and run_id != expected_run_execution_id
            )
            or not isinstance(decision_id, int)
            or decision_id <= 0
            or not isinstance(outbox_id, int)
            or outbox_id <= 0
            or status not in allowed_statuses
            or digest.get('suppressed') is not False
        ):
            raise RuntimeError('MORNING_DIGEST_V2_RESULT_INVALID')
        pending = status in {'QUEUED', 'LEASED', 'RETRY'}
        if digest.get('canonical_delivery_pending') is not pending:
            raise RuntimeError('MORNING_DIGEST_V2_PENDING_STATE_INVALID')
        delivery_required = digest.get('delivery_required')
        reason_code = digest.get('decision_reason_code')
        if delivery_required is not True and not (
            reason_code == 'STAGE_ALREADY_FINALIZED' and deduplicated
        ):
            raise RuntimeError('MORNING_DIGEST_V2_DELIVERY_STATE_INVALID')
        semantic_sha = digest.get('canonical_semantic_sha256')
        if not isinstance(digest.get('inserted'), bool):
            raise RuntimeError('MORNING_DIGEST_V2_INSERT_STATE_INVALID')
        if not isinstance(digest.get('deduplicated'), bool):
            raise RuntimeError('MORNING_DIGEST_V2_DEDUPE_STATE_INVALID')
        if not isinstance(digest.get('replayed'), bool):
            raise RuntimeError('MORNING_DIGEST_V2_REPLAY_STATE_INVALID')
        if reason_code not in {
            'EARLY_SEND',
            'LATE_SEND_CHANGED',
            'LATE_SEND_UNPRESENTED_BUY',
            'LATE_SEND_NO_ACCEPTED_EARLY',
            'STAGE_ALREADY_FINALIZED',
        }:
            raise RuntimeError('MORNING_DIGEST_V2_REASON_INVALID')
        if (
            digest.get('semantic_policy_id') != late_diff.SEMANTIC_POLICY_ID
            or not isinstance(semantic_sha, str)
            or re.fullmatch(r'[0-9a-f]{64}', semantic_sha) is None
        ):
            raise RuntimeError('MORNING_DIGEST_V2_SEMANTIC_INVALID')
        return {
            'outbox_id': outbox_id,
            'baseline_outbox_id': digest.get('baseline_outbox_id'),
            'decision_id': decision_id,
            'status': status,
            'deduplicated': deduplicated,
            'replayed': replayed,
            'suppressed': False,
            'decision_reason_code': reason_code,
            'canonical_run_execution_id': run_id,
        }

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
        'baseline_outbox_id': None,
        'decision_id': None,
        'status': str(status),
        'deduplicated': deduplicated,
        'replayed': replayed,
        'suppressed': False,
        'decision_reason_code': None,
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
    evaluations = result.pop('_morning_evaluations', None)
    v2_digest_enabled = result.pop('_morning_digest_v2_enabled', None)
    legacy_late_diff_result = result.pop('_legacy_late_diff_result', None)
    if not isinstance(evaluations, list) or len(evaluations) != len(scored):
        raise RuntimeError('MORNING_EVALUATION_INPUT_MISSING')
    if not isinstance(v2_digest_enabled, bool):
        raise RuntimeError('MORNING_DIGEST_CAPABILITY_RESULT_MISSING')
    if v2_digest_enabled:
        cur.execute(
            "SELECT ux_app.append_morning_digest_v2(%s,%s)",
            (rid, runner.Jsonb(evaluations)),
        )
        row = cur.fetchone()
        if not row or not isinstance(row[0], dict):
            raise RuntimeError('MORNING_DIGEST_V2_APPEND_RESULT_MISSING')
        raw_digest = row[0]
    elif (
        isinstance(legacy_late_diff_result, dict)
        and legacy_late_diff_result.get('suppressed') is True
    ):
        raw_digest = legacy_late_diff_result
    else:
        cur.execute(
            "SELECT ux_app.append_morning_digest_v1(%s)",
            (rid,),
        )
        row = cur.fetchone()
        if not row or not isinstance(row[0], dict):
            raise RuntimeError('MORNING_DIGEST_V1_APPEND_RESULT_MISSING')
        raw_digest = row[0]

    digest = _validate_outbox_result(
        raw_digest,
        expected_kind='MORNING_DIGEST',
        expected_run_execution_id=rid,
    )

    return {
        **result,
        'digest_outbox_id': digest['outbox_id'],
        'digest_baseline_outbox_id': digest['baseline_outbox_id'],
        'digest_decision_id': digest['decision_id'],
        'digest_status': digest['status'],
        'digest_disposition': (
            'SUPPRESSED'
            if digest['suppressed']
            else (
                'DEDUPLICATED'
                if digest['deduplicated']
                else ('APPENDED' if raw_digest.get('inserted') else 'REPLAY')
            )
        ),
        'late_diff_reason': (
            digest['decision_reason_code']
            or (
                legacy_late_diff_result.get('reason_code')
                if isinstance(legacy_late_diff_result, dict)
                else None
            )
        ),
        'morning_digest_protocol': 'V2' if v2_digest_enabled else 'V1_COMPAT',
        'morning_evaluation_count': len(evaluations),
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

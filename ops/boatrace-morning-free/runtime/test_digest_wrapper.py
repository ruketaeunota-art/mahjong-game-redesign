from __future__ import annotations

import contextlib
import io
import os
import sys
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import run_with_digest

runner = run_with_digest.runner


class FakeCursor:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.responses = responses

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.calls.append((sql, params))

    def fetchone(self) -> tuple[dict[str, Any]]:
        return (self.responses.pop(0),)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor, *, fail_on_commit: int | None = None) -> None:
        self.cursor_value = cursor
        self.fail_on_commit = fail_on_commit
        self.commit_count = 0
        self.rollback_count = 0

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commit_count += 1
        if self.commit_count == self.fail_on_commit:
            raise RuntimeError('simulated commit transport failure')

    def rollback(self) -> None:
        self.rollback_count += 1


def _fake_persist(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        'inspected': 2,
        'actionable': 1,
        's': 0,
        'a': 0,
        'handoff': 0,
        '_morning_evaluations': [],
        '_morning_digest_v2_enabled': False,
        '_legacy_late_diff_result': {
            'suppressed': False,
            'reason_code': 'NOT_LATE',
        },
    }


def _restore_runner_attr(name: str, value: Any) -> None:
    if value is None:
        delattr(runner, name)
    else:
        setattr(runner, name, value)


def test_normal_digest() -> None:
    original = run_with_digest._original_persist
    try:
        run_with_digest._original_persist = _fake_persist
        cursor = FakeCursor([
            {
                'outbox_id': 321,
                'status': 'QUEUED',
                'inserted': True,
                'deduplicated': False,
                'run_execution_id': 99,
            }
        ])
        result = run_with_digest.persist_with_digest(
            cursor,
            99,
            date(2026, 8, 19),
            'EARLY',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
    finally:
        run_with_digest._original_persist = original

    assert cursor.calls == [
        ('SELECT ux_app.append_morning_digest_v1(%s)', (99,))
    ]
    assert result['digest_outbox_id'] == 321
    assert result['digest_status'] == 'QUEUED'
    assert result['digest_disposition'] == 'APPENDED'
    assert result['digest_canonical_run_execution_id'] == 99
    assert result['inspected'] == 2


def test_deduplicated_terminal_digest() -> None:
    original = run_with_digest._original_persist
    try:
        run_with_digest._original_persist = _fake_persist
        cursor = FakeCursor([
            {
                'outbox_id': 320,
                'status': 'FAILED',
                'inserted': False,
                'deduplicated': True,
                'canonical_run_execution_id': 98,
            }
        ])
        result = run_with_digest.persist_with_digest(
            cursor,
            99,
            date(2026, 8, 19),
            'EARLY',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
    finally:
        run_with_digest._original_persist = original

    assert result['digest_outbox_id'] == 320
    assert result['digest_status'] == 'FAILED'
    assert result['digest_disposition'] == 'DEDUPLICATED'
    assert result['digest_canonical_run_execution_id'] == 98


def test_legacy_identical_late_is_suppressed_without_v1_append() -> None:
    suppression = {
        'schema_version': 'boatrace-morning-late-suppression-compat-v1',
        'run_execution_id': 99,
        'decision_id': None,
        'outbox_id': None,
        'baseline_outbox_id': 320,
        'inserted': False,
        'deduplicated': True,
        'replayed': False,
        'suppressed': True,
        'delivery_required': False,
        'canonical_delivery_pending': False,
        'decision_reason_code': 'LATE_SUPPRESSED_NO_MATERIAL_CHANGE',
        'status': 'SUPPRESSED',
        'semantic_policy_id': 'morning-late-diff-v1',
        'canonical_semantic_sha256': 'b' * 64,
        'baseline_run_execution_id': 98,
        'payload_sha256': 'a' * 64,
        'logical_target_date': '2026-08-19',
        'logical_stage': 'LATE',
        'change_kinds': [],
    }

    def fake_persist(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = _fake_persist()
        result['_legacy_late_diff_result'] = suppression
        return result

    original = run_with_digest._original_persist
    try:
        run_with_digest._original_persist = fake_persist
        cursor = FakeCursor([])
        result = run_with_digest.persist_with_digest(
            cursor,
            99,
            date(2026, 8, 19),
            'LATE',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
        replay = run_with_digest.persist_with_digest(
            cursor,
            99,
            date(2026, 8, 19),
            'LATE',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
    finally:
        run_with_digest._original_persist = original

    assert cursor.calls == []
    assert result['digest_outbox_id'] is None
    assert result['digest_baseline_outbox_id'] == 320
    assert result['digest_status'] == 'SUPPRESSED'
    assert result['digest_disposition'] == 'SUPPRESSED'
    assert result['morning_digest_protocol'] == 'V1_COMPAT'
    assert replay['digest_outbox_id'] is None
    assert replay['digest_disposition'] == 'SUPPRESSED'


def test_v2_capability_calls_only_atomic_v2_rpc() -> None:
    def fake_persist(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = _fake_persist()
        result['_morning_digest_v2_enabled'] = True
        result['_legacy_late_diff_result'] = None
        return result

    response = {
        'schema_version': 'boatrace-morning-digest-outbox-result-v4',
        'run_execution_id': 99,
        'decision_id': 700,
        'outbox_id': 321,
        'status': 'QUEUED',
        'inserted': True,
        'deduplicated': False,
        'replayed': False,
        'suppressed': False,
        'delivery_required': True,
        'canonical_delivery_pending': True,
        'decision_reason_code': 'EARLY_SEND',
        'semantic_policy_id': 'morning-late-diff-v1',
        'canonical_semantic_sha256': 'c' * 64,
        'baseline_run_execution_id': None,
        'baseline_outbox_id': None,
        'change_kinds': [],
    }
    original = run_with_digest._original_persist
    try:
        run_with_digest._original_persist = fake_persist
        cursor = FakeCursor([response])
        result = run_with_digest.persist_with_digest(
            cursor,
            99,
            date(2026, 8, 19),
            'EARLY',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
    finally:
        run_with_digest._original_persist = original

    assert len(cursor.calls) == 1
    assert cursor.calls[0][0] == 'SELECT ux_app.append_morning_digest_v2(%s,%s)'
    assert cursor.calls[0][1][0] == 99
    assert result['morning_digest_protocol'] == 'V2'
    assert result['digest_decision_id'] == 700
    assert result['digest_outbox_id'] == 321


def test_v2_no_change_late_accepts_nullable_outbox() -> None:
    def fake_persist(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = _fake_persist()
        result['_morning_digest_v2_enabled'] = True
        result['_legacy_late_diff_result'] = None
        return result

    response = {
        'schema_version': 'boatrace-morning-digest-outbox-result-v4',
        'run_execution_id': 99,
        'decision_id': 701,
        'outbox_id': None,
        'status': 'SUPPRESSED',
        'inserted': True,
        'deduplicated': True,
        'replayed': False,
        'suppressed': True,
        'delivery_required': False,
        'canonical_delivery_pending': False,
        'decision_reason_code': 'LATE_SUPPRESSED_NO_MATERIAL_CHANGE',
        'semantic_policy_id': 'morning-late-diff-v1',
        'canonical_semantic_sha256': 'c' * 64,
        'baseline_run_execution_id': 98,
        'baseline_outbox_id': 320,
        'change_kinds': [],
    }
    original = run_with_digest._original_persist
    try:
        run_with_digest._original_persist = fake_persist
        cursor = FakeCursor([response])
        result = run_with_digest.persist_with_digest(
            cursor,
            99,
            date(2026, 8, 19),
            'LATE',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
    finally:
        run_with_digest._original_persist = original

    assert result['digest_outbox_id'] is None
    assert result['digest_baseline_outbox_id'] == 320
    assert result['digest_decision_id'] == 701
    assert result['digest_disposition'] == 'SUPPRESSED'


def test_v2_exact_replay_reuses_canonical_outbox() -> None:
    def fake_persist(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = _fake_persist()
        result['_morning_digest_v2_enabled'] = True
        result['_legacy_late_diff_result'] = None
        return result

    response = {
        'schema_version': 'boatrace-morning-digest-outbox-result-v4',
        'run_execution_id': 99,
        'decision_id': 700,
        'outbox_id': 321,
        'status': 'ACCEPTED',
        'inserted': False,
        'deduplicated': True,
        'replayed': True,
        'suppressed': False,
        'delivery_required': True,
        'canonical_delivery_pending': False,
        'decision_reason_code': 'EARLY_SEND',
        'semantic_policy_id': 'morning-late-diff-v1',
        'canonical_semantic_sha256': 'c' * 64,
        'baseline_run_execution_id': None,
        'baseline_outbox_id': None,
        'change_kinds': [],
    }
    original = run_with_digest._original_persist
    try:
        run_with_digest._original_persist = fake_persist
        cursor = FakeCursor([response])
        result = run_with_digest.persist_with_digest(
            cursor,
            99,
            date(2026, 8, 19),
            'EARLY',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
    finally:
        run_with_digest._original_persist = original

    assert len(cursor.calls) == 1
    assert 'append_morning_digest_v2' in cursor.calls[0][0]
    assert result['digest_outbox_id'] == 321
    assert result['digest_disposition'] == 'DEDUPLICATED'


def test_v2_finalized_stage_reuses_prior_outbox_without_delivery() -> None:
    def fake_persist(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = _fake_persist()
        result['_morning_digest_v2_enabled'] = True
        result['_legacy_late_diff_result'] = None
        return result

    response = {
        'schema_version': 'boatrace-morning-digest-outbox-result-v4',
        'run_execution_id': 100,
        'decision_id': 702,
        'outbox_id': 321,
        'status': 'ACCEPTED',
        'inserted': True,
        'deduplicated': True,
        'replayed': False,
        'suppressed': False,
        'delivery_required': False,
        'canonical_delivery_pending': False,
        'decision_reason_code': 'STAGE_ALREADY_FINALIZED',
        'semantic_policy_id': 'morning-late-diff-v1',
        'canonical_semantic_sha256': 'c' * 64,
        'baseline_run_execution_id': None,
        'baseline_outbox_id': None,
        'change_kinds': [],
    }
    original = run_with_digest._original_persist
    try:
        run_with_digest._original_persist = fake_persist
        cursor = FakeCursor([response])
        result = run_with_digest.persist_with_digest(
            cursor,
            100,
            date(2026, 8, 19),
            'LATE',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
    finally:
        run_with_digest._original_persist = original

    assert len(cursor.calls) == 1
    assert 'append_morning_digest_v2' in cursor.calls[0][0]
    assert result['digest_outbox_id'] == 321
    assert result['digest_disposition'] == 'DEDUPLICATED'
    assert result['late_diff_reason'] == 'STAGE_ALREADY_FINALIZED'


def test_v2_finalized_stage_after_no_outbox_suppression_stays_nullable() -> None:
    def fake_persist(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = _fake_persist()
        result['_morning_digest_v2_enabled'] = True
        result['_legacy_late_diff_result'] = None
        return result

    response = {
        'schema_version': 'boatrace-morning-digest-outbox-result-v4',
        'run_execution_id': 101,
        'decision_id': 703,
        'outbox_id': None,
        'status': 'SUPPRESSED',
        'inserted': True,
        'deduplicated': True,
        'replayed': False,
        'suppressed': True,
        'delivery_required': False,
        'canonical_delivery_pending': False,
        'decision_reason_code': 'STAGE_ALREADY_FINALIZED',
        'semantic_policy_id': 'morning-late-diff-v1',
        'canonical_semantic_sha256': 'd' * 64,
        'baseline_run_execution_id': None,
        'baseline_outbox_id': None,
        'change_kinds': [],
    }
    original = run_with_digest._original_persist
    try:
        run_with_digest._original_persist = fake_persist
        cursor = FakeCursor([response])
        result = run_with_digest.persist_with_digest(
            cursor,
            101,
            date(2026, 8, 19),
            'LATE',
            [],
            [],
            datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        )
    finally:
        run_with_digest._original_persist = original

    assert len(cursor.calls) == 1
    assert 'append_morning_digest_v2' in cursor.calls[0][0]
    assert result['digest_outbox_id'] is None
    assert result['digest_baseline_outbox_id'] is None
    assert result['digest_disposition'] == 'SUPPRESSED'
    assert result['late_diff_reason'] == 'STAGE_ALREADY_FINALIZED'


def test_terminal_acquisition_failure() -> None:
    cursor = FakeCursor([
        {
            'outbox_id': 777,
            'status': 'QUEUED',
            'inserted': True,
            'deduplicated': False,
            'run_execution_id': 99,
        }
    ])
    result = run_with_digest._append_terminal_failure(
        cursor,
        99,
        'SOURCE_ACQUISITION_FAILED',
        datetime(2026, 8, 19, 8, 30, tzinfo=timezone.utc),
    )

    assert cursor.calls == [
        (
            'SELECT ux_app.append_morning_run_failure_v1(%s,%s,%s)',
            (
                99,
                'SOURCE_ACQUISITION_FAILED',
                datetime(2026, 8, 19, 8, 30, tzinfo=timezone.utc),
            ),
        ),
    ]
    assert result['failure_outbox_id'] == 777
    assert result['failure_status'] == 'QUEUED'
    assert result['failure_disposition'] == 'APPENDED'


def test_deduplicated_acquisition_failure() -> None:
    cursor = FakeCursor([
        {
            'outbox_id': 776,
            'status': 'ACCEPTED',
            'inserted': False,
            'replayed': False,
            'deduplicated': True,
            'canonical_run_execution_id': 98,
        }
    ])
    result = run_with_digest._append_terminal_failure(
        cursor,
        99,
        'SOURCE_ACQUISITION_FAILED',
        datetime(2026, 8, 19, 8, 31, tzinfo=timezone.utc),
    )

    assert result['failure_outbox_id'] == 776
    assert result['failure_status'] == 'ACCEPTED'
    assert result['failure_disposition'] == 'DEDUPLICATED'
    assert result['failure_canonical_run_execution_id'] == 98


def test_source_failure_is_recorded_after_running_identity() -> None:
    cursor = FakeCursor([])
    conn = FakeConnection(cursor)
    calls: list[str] = []
    original = {
        'psycopg': getattr(runner, 'psycopg', None),
        'create_run': getattr(runner, 'create_run', None),
        'acquire': getattr(runner, 'acquire', None),
        'append_failure': run_with_digest._append_terminal_failure,
        'argv': sys.argv[:],
        'database_url': os.environ.get('DATABASE_URL'),
    }
    try:
        runner.psycopg = SimpleNamespace(connect=lambda _: conn)
        runner.create_run = lambda *args: calls.append('create') or 99
        runner.acquire = lambda _: calls.append('acquire') or ([], [])
        run_with_digest._append_terminal_failure = (
            lambda *args: calls.append('append_failure') or {
                'failure_outbox_id': 777,
                'failure_status': 'QUEUED',
                'failure_disposition': 'APPENDED',
                'failure_canonical_run_execution_id': 99,
            }
        )
        os.environ['DATABASE_URL'] = 'postgresql://test'
        sys.argv = ['run_with_digest.py', '--target-date', '2026-08-19', '--stage', 'LATE']
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_with_digest.main()
    finally:
        _restore_runner_attr('psycopg', original['psycopg'])
        _restore_runner_attr('create_run', original['create_run'])
        _restore_runner_attr('acquire', original['acquire'])
        run_with_digest._append_terminal_failure = original['append_failure']
        sys.argv = original['argv']
        if original['database_url'] is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = original['database_url']

    assert result == 1
    assert calls == ['create', 'acquire', 'append_failure']
    assert conn.commit_count == 2
    assert conn.rollback_count == 0


def test_unknown_success_commit_does_not_create_failure() -> None:
    cursor = FakeCursor([])
    conn = FakeConnection(cursor, fail_on_commit=2)
    append_calls: list[str] = []
    original = {
        'psycopg': getattr(runner, 'psycopg', None),
        'create_run': getattr(runner, 'create_run', None),
        'acquire': getattr(runner, 'acquire', None),
        'load_config': getattr(runner, 'load_config', None),
        'score_race': getattr(runner, 'score_race', None),
        'persist': getattr(runner, 'persist', None),
        'append_failure': run_with_digest._append_terminal_failure,
        'argv': sys.argv[:],
        'database_url': os.environ.get('DATABASE_URL'),
    }
    try:
        runner.psycopg = SimpleNamespace(connect=lambda _: conn)
        runner.create_run = lambda *args: 99
        runner.acquire = lambda _: ([{'race': 1}], [])
        runner.load_config = lambda _: ({}, {})
        runner.score_race = lambda *args: {'scored': True}
        runner.persist = lambda *args: {'inspected': 1}
        run_with_digest._append_terminal_failure = (
            lambda *args: append_calls.append('append_failure') or {}
        )
        os.environ['DATABASE_URL'] = 'postgresql://test'
        sys.argv = ['run_with_digest.py', '--target-date', '2026-08-19', '--stage', 'EARLY']
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run_with_digest.main()
    finally:
        _restore_runner_attr('psycopg', original['psycopg'])
        _restore_runner_attr('create_run', original['create_run'])
        _restore_runner_attr('acquire', original['acquire'])
        _restore_runner_attr('load_config', original['load_config'])
        _restore_runner_attr('score_race', original['score_race'])
        _restore_runner_attr('persist', original['persist'])
        run_with_digest._append_terminal_failure = original['append_failure']
        sys.argv = original['argv']
        if original['database_url'] is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = original['database_url']

    assert result == 1
    assert append_calls == []
    assert conn.commit_count == 2
    assert 'SUCCESS_COMMIT_OUTCOME_UNKNOWN' in output.getvalue()


def main() -> None:
    test_normal_digest()
    test_deduplicated_terminal_digest()
    test_legacy_identical_late_is_suppressed_without_v1_append()
    test_v2_capability_calls_only_atomic_v2_rpc()
    test_v2_no_change_late_accepts_nullable_outbox()
    test_v2_exact_replay_reuses_canonical_outbox()
    test_v2_finalized_stage_reuses_prior_outbox_without_delivery()
    test_v2_finalized_stage_after_no_outbox_suppression_stays_nullable()
    test_terminal_acquisition_failure()
    test_deduplicated_acquisition_failure()
    test_source_failure_is_recorded_after_running_identity()
    test_unknown_success_commit_does_not_create_failure()
    print('morning digest wrapper self-test: PASS')


if __name__ == '__main__':
    main()

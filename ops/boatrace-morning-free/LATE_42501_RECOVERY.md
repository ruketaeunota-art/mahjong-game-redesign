# Morning LATE SQLSTATE 42501 — scoped context fix (2026-10-16)

**Status: PR draft / no production application.**

On 2026-10-15 the scheduled LATE run, run execution 110, failed with
`PERSISTENCE_FAILED`. GitHub Actions logged
`MORNING_PERSISTENCE_DIAGNOSTIC_V1 phase=AUDIT_AND_RUN_EVENT,
exception_chain=[InsufficientPrivilege], sqlstate=42501`.

## Diagnosis
The active production database has no `ux_app.append_morning_digest_v2(bigint,jsonb)`.
The legacy `late_diff.evaluate_legacy_preterminal_suppression` read
`ux_app.notification_outbox` directly during LATE only.
`boatrace_morning_runner` has **no SELECT** on that sensitive table.
EARLY does not invoke this preterminal comparison and therefore was able to
succeed. This is a concrete, independently confirmed permission mismatch,
rather than a generic rendering or upstream source failure.

## Proposed restricted repair
1. Apply `ops/boatrace-morning-free/migrations/20261016_morning_late_context_v1.sql`
   as `neondb_owner` in a **reviewed** deployment.
2. This creates `ux_app.morning_late_diff_context_v1(date)`, owned by
   `neondb_owner`, using an explicit `SECURITY DEFINER` search path,
   caller `session_user` allowlist, per-target-date query, restricted output
   (only presence of a prior LATE digest, and ACCEPTED EARLY identity plus
   four semantic fields). EXECUTE is revoked from PUBLIC and granted only
   to `boatrace_morning_runner`. Broad outbox SELECT remains denied.
3. Verify as **actual runner role**:
   `SELECT current_user, has_table_privilege(current_user,'ux_app.notification_outbox','SELECT'), (ux_app.morning_late_diff_context_v1(CURRENT_DATE))->>'schema_version';`
   Expect role `boatrace_morning_runner`, `false`,
   `morning-late-diff-context-v1`. Validate no message payload or DSN is logged.
4. **Only then** merge/deploy the runtime source and matching
   `runtime_manifest.json` and `version.txt` in this PR. Re-run the complete
   runtime test suite and verify the next authorized scheduled LATE
   reaches terminal SUCCEEDED. Do not retroactively rewrite failed 10/15
   events or synthesize accepted LINE receipts.
5. The caller uses the same advisory lock and semantic-SHA comparison as
   before; it must fail closed on a missing/invalid context RPC. The patch
   does not adjust any morning score, ticket, wager, or LINE send policy.

## Verification performed already — isolated clone only
Child Neon branch: `br-delicate-wind-axfm7xkn` /
`audit-morning-late-rpc-20261016`.

- The exact migration executed successfully inside a Neon child-branch
  transaction (CREATE FUNCTION, REVOKE PUBLIC, GRANT runner).
- Via Neon `role_name=boatrace_morning_runner`, with **current_user and
  session_user both matching that role**, `SELECT COUNT(*) FROM
  ux_app.notification_outbox` is rejected with permission denied.
- The same role can execute the scoped context, returns the exact contract,
  `late_outbox_exists=false` and an EARLY baseline for 2026-10-15.
- Public GitHub Actions validation run
  https://github.com/ruketaeunota-art/mahjong-game-redesign/actions/runs/38828635469
  succeeded: compiled runtime, validated pinned file hashes and **54/54
  Python tests PASSED**.
- This is **not proof of a successful production LATE run**.

## Rollback
Do not turn on the new runtime unless the restricted DB function is installed.
If the new runtime itself is faulty, restore the previously pinned runtime
commit and its matching manifest/version; do not modify historical outbox
contents or broaden permissions. The isolated child branch can be deleted
later after the review evidence has been recorded.

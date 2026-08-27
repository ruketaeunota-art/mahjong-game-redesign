from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import late_diff
import runner


def _skip_record(race: dict[str, Any], deadline: datetime) -> dict[str, Any]:
    signals = list(race.get('signals') or [])
    strongest = max(signals, key=lambda item: float(item.get('strength') or 0.0), default=None)
    max_strength = float(strongest.get('strength') or 0.0) if strongest else 0.0
    reasons = [
        *list(race.get('reason_codes') or []),
        'MORNING_GRADE_B',
        'SA_THRESHOLD_NOT_MET',
    ]
    if race.get('feature_degraded'):
        reasons.append('FEATURE_DEGRADED')
    return {
        'venue_code': int(race['venue_code']),
        'race_no': int(race['race_no']),
        'official_deadline_at': deadline.isoformat(),
        'morning_grade': str(race.get('race_grade') or 'B'),
        'reason_codes': sorted(set(reasons)),
        'max_signal_strength': round(max_strength, 6),
        'sa_strength_threshold': 1.75,
        'strongest_signal': (
            {
                'lane': int(strongest['lane']),
                'direction': strongest.get('direction'),
                'scope': strongest.get('scope'),
                'priority': strongest.get('priority'),
                'strength': round(float(strongest.get('strength') or 0.0), 6),
                'ratio': round(float(strongest.get('ratio') or 0.0), 6),
            }
            if strongest
            else None
        ),
        'degradation_codes': list(race.get('degradation_codes') or []),
    }


def persist_with_skip_audit(
    cur: Any,
    rid: int,
    target: date,
    stage: str,
    scored: list[dict[str, Any]],
    source_failures: list[str],
    now: datetime,
) -> dict[str, Any]:
    source_evidence = runner.source_manifest()
    source_artifact_sha256 = runner.source_manifest_sha256()
    scorer_artifact_sha256 = runner.sha({
        'schema_version': 'boatrace-morning-scorer-output-manifest-v1',
        'races': scored,
    })
    owned: list[tuple[dict[str, Any], datetime]] = []
    for race in scored:
        deadline = runner.dt_deadline(target, race['deadline_time_jst'])
        hour = deadline.astimezone(runner.JST).hour
        if (stage == 'EARLY' and hour < 10) or (stage == 'LATE' and hour >= 10):
            owned.append((race, deadline))

    actionable = [
        (race, deadline)
        for race, deadline in owned
        if now < deadline - timedelta(minutes=10)
    ]
    candidates = [
        (race, deadline)
        for race, deadline in actionable
        if race['race_grade'] in ('S', 'A')
    ]
    s_candidates = [item for item in candidates if item[0]['race_grade'] == 'S']
    a_candidates = [item for item in candidates if item[0]['race_grade'] == 'A']
    skipped = [
        _skip_record(race, deadline)
        for race, deadline in actionable
        if race['race_grade'] not in ('S', 'A')
    ]
    morning_evaluations = late_diff.build_full_evaluations(
        scored,
        target_date=target,
        stage=stage,
        now=now,
    )

    for race, deadline in candidates:
        payload = {
            'schema_version': 'boatrace-ux-morning-projection-v1',
            'target_date': target.isoformat(),
            'stage': stage,
            'venue_code': race['venue_code'],
            'race_no': race['race_no'],
            'morning_grade': race['race_grade'],
            'deadline_time_jst': race['deadline_time_jst'],
            'signals': race['signals'],
            'tickets': race['tickets'],
            'entrants': race['entrants'],
            'feature_degraded': True,
            'degradation_codes': race['degradation_codes'],
        }
        payload_sha = runner.sha(payload)
        projection_key = runner.sha({
            'kind': 'MORNING',
            'target_date': target.isoformat(),
            'venue': race['venue_code'],
            'race': race['race_no'],
            'revision': 1,
        })
        cur.execute(
            "INSERT INTO ux_app.race_projections("
            "projection_key,run_execution_id,revision,projection_kind,target_date,"
            "venue_code,race_no,official_deadline_at,morning_grade,canonical_decision,"
            "reason_codes,attempt_present,publishable_plan_verified,operational_code,"
            "display_state,classifier_policy_id,performance_kind,source_ref,"
            "source_artifact_sha256,payload,payload_sha256,spend_yen,projected_at,"
            "actionable_until) VALUES(%s,%s,1,'MORNING',%s,%s,%s,%s,%s,NULL,"
            "'[]'::jsonb,false,false,NULL,%s,NULL,NULL,%s,%s,%s,%s,0,clock_timestamp(),NULL) "
            "ON CONFLICT (projection_kind,target_date,venue_code,race_no,revision) DO NOTHING",
            (
                projection_key, rid, target, race['venue_code'], race['race_no'], deadline,
                race['race_grade'], race['race_grade'], runner.SOURCE_REF,
                source_artifact_sha256, runner.Jsonb(payload), payload_sha,
            ),
        )

    handoffs = 0
    logic = (
        'v2.1-prod-20260809-003'
        if stage == 'EARLY'
        else 'v2.1-prod-reporter-20260812-004'
    )
    for race, deadline in s_candidates:
        if not race['tickets']:
            continue
        entrants = runner.build_handoff_entrants(race['entrants'])
        payload = {
            'schema_version': 'purchase-assist-handoff-v2',
            'target_date': target.isoformat(),
            'venue_code': race['venue_code'],
            'race_no': race['race_no'],
            'deadline_at': deadline.isoformat(),
            'logic_version': logic,
            'notification_policy': runner.PURCHASE_POLICY,
            'race_grade': 'S',
            'morning_hypothesis': 'Morning V2.1 frozen-spec value signal',
            'scorer_tickets': race['tickets'],
            'entrants': entrants,
            'source_scorer_ref': runner.SOURCE_REF,
        }
        payload_sha = runner.sha(payload)
        handoff_key = runner.sha({
            'target': target.isoformat(),
            'venue': race['venue_code'],
            'race': race['race_no'],
            'logic': logic,
            'source': runner.SOURCE_REF,
        })
        cur.execute(
            "INSERT INTO purchase_assist.handoffs("
            "handoff_key,schema_version,target_date,venue_code,race_no,deadline_at,"
            "logic_version,notification_policy,source_scorer_ref,source_scorer_sha256,"
            "source_scorer_artifact_id,race_grade,morning_hypothesis,scorer_tickets,"
            "entrants,canonical_payload,payload_sha256) VALUES(%s,'purchase-assist-handoff-v2',"
            "%s,%s,%s,%s,%s,%s,%s,%s,NULL,'S',%s,%s,%s,%s,%s) "
            "ON CONFLICT (handoff_key) DO NOTHING RETURNING id",
            (
                handoff_key, target, race['venue_code'], race['race_no'], deadline,
                logic, runner.PURCHASE_POLICY, runner.SOURCE_REF,
                scorer_artifact_sha256, payload['morning_hypothesis'],
                runner.Jsonb(race['tickets']), runner.Jsonb(entrants),
                runner.Jsonb(payload), payload_sha,
            ),
        )
        if cur.fetchone():
            handoffs += 1

    source_state = 'SOURCE_OK' if not source_failures else 'PARTIAL_SOURCE'
    v2_digest_enabled = late_diff.v2_rpc_available(cur)
    legacy_semantics: dict[str, Any] | None = None
    legacy_late_diff_result: dict[str, Any] | None = None
    if not v2_digest_enabled:
        legacy_semantics = late_diff.build_legacy_semantics(
            morning_evaluations,
            source_state=source_state,
            history_state='DEGRADED_HISTORY',
            feature_state='FEATURE_DEGRADED',
        )
        legacy_late_diff_result = late_diff.evaluate_legacy_preterminal_suppression(
            cur,
            run_execution_id=rid,
            target_date=target,
            stage=stage,
            semantic_result=legacy_semantics,
        )
    outcome = (
        'CANDIDATES_FOUND'
        if candidates
        else ('PARTIAL_SOURCE' if source_failures else 'NO_CANDIDATES')
    )
    details = {
        'stage': stage,
        'runner': runner.SOURCE_REF,
        'full_inspected_count': len(scored),
        'full_actionable_count': len(actionable),
        'source_failures': source_failures[:100],
        'source_manifest': source_evidence,
        'source_manifest_sha256': source_artifact_sha256,
        'source_artifact_count': len(source_evidence['artifacts']),
        'scorer_artifact_sha256': scorer_artifact_sha256,
        'morning_evaluation_input_version': 'FULL_SCORED_DAY_V1',
        'morning_evaluation_input_count': len(morning_evaluations),
        'morning_digest_v2_capability': v2_digest_enabled,
        'degraded': True,
        'top3_structural_overlay_suppressed': True,
        'skip_audit_version': 'morning-skip-audit-v1',
        'skip_audit_count': len(skipped),
        'skip_audit': skipped,
        **(
            late_diff.legacy_semantic_detail_fields(legacy_semantics)
            if legacy_semantics is not None
            else {}
        ),
        **(
            late_diff.legacy_suppression_audit_fields(
                legacy_late_diff_result
            )
            if stage == 'LATE' and legacy_late_diff_result is not None
            else {}
        ),
    }
    cur.execute(
        "INSERT INTO ux_app.run_events("
        "run_execution_id,event_seq,event_at,lifecycle_status,outcome,source_state,"
        "history_state,feature_state,inspected_count,actionable_count,s_count,a_count,"
        "handoff_count,source_artifact_ref,source_artifact_sha256,details,finished_at) "
        "VALUES(%s,2,clock_timestamp(),'SUCCEEDED',%s,%s,'DEGRADED_HISTORY',"
        "'FEATURE_DEGRADED',%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())",
        (
            rid, outcome, source_state, len(owned), len(actionable), len(s_candidates),
            len(a_candidates), handoffs, runner.SOURCE_REF,
            source_artifact_sha256, runner.Jsonb(details),
        ),
    )
    return {
        'inspected': len(owned),
        'actionable': len(actionable),
        's': len(s_candidates),
        'a': len(a_candidates),
        'handoff': handoffs,
        '_morning_evaluations': morning_evaluations,
        '_morning_digest_v2_enabled': v2_digest_enabled,
        '_legacy_late_diff_result': legacy_late_diff_result,
    }


runner.persist = persist_with_skip_audit

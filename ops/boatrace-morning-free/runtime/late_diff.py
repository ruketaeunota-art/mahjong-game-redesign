from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


SEMANTIC_POLICY_ID = "morning-late-diff-v1"
SUPPRESSION_REASON = "LATE_SUPPRESSED_NO_MATERIAL_CHANGE"
SUPPRESSION_STATUS = "SUPPRESSED"
SA_STRENGTH_THRESHOLD = 1.75
S_STRENGTH_THRESHOLD = 3.0

_JST = ZoneInfo("Asia/Tokyo")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DEADLINE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")
_COMBINATION = re.compile(r"^([1-6])-([1-6])-([1-6])$")
_LEGACY_DETAIL_VERSION = "late_diff_semantic_version"
_LEGACY_DETAIL_STATUS = "late_diff_semantic_status"
_LEGACY_DETAIL_SNAPSHOT = "late_diff_semantic_snapshot"
_LEGACY_DETAIL_SHA256 = "late_diff_semantic_sha256"


def canonical(value: Any) -> bytes:
    """Validate that a value is finite JSON and return deterministic bytes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("evaluation integer is invalid")
    return value


def _number(value: Any, *, code: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(code)
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(code)
    return result


def _code(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_CODE.fullmatch(value) is None:
        raise ValueError("evaluation code is invalid")
    return value


def _codes(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("evaluation codes are not an array")
    return sorted({_code(value) for value in values})


def _event_context(race: dict[str, Any]) -> tuple[int | None, str | None, str | None]:
    event_day = race.get("event_day")
    if event_day is not None:
        event_day = _strict_int(event_day)
        if event_day not in range(1, 13):
            raise ValueError("evaluation event day is invalid")
    event_phase = race.get("event_phase")
    if event_phase is not None and event_phase not in {
        "REGULAR",
        "SEMIFINAL",
        "FINAL",
    }:
        raise ValueError("evaluation event phase is invalid")
    event_day_label = race.get("event_day_label")
    if event_day_label is not None and (
        not isinstance(event_day_label, str)
        or event_day_label != event_day_label.strip()
        or not event_day_label
        or len(event_day_label) > 64
    ):
        raise ValueError("evaluation event day label is invalid")
    return event_day, event_phase, event_day_label


def _deadline_at(target_date: date, raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("evaluation deadline is invalid")
    match = _DEADLINE.fullmatch(raw)
    if match is None:
        raise ValueError("evaluation deadline is invalid")
    hour, minute, second = (int(part) for part in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("evaluation deadline is outside clock bounds")
    return datetime.combine(target_date, time(hour, minute, second), _JST)


def _signals(values: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValueError("evaluation signals are not an array")
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[int, str, str, str]] = set()
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("evaluation signal is not an object")
        lane = _strict_int(raw.get("lane"))
        if lane not in range(1, 7):
            raise ValueError("evaluation signal lane is invalid")
        direction = _code(raw.get("direction"))
        scope = _code(raw.get("scope"))
        priority = _code(raw.get("priority"))
        if priority not in {"S", "A", "B"}:
            raise ValueError("evaluation signal priority is invalid")
        identity = (lane, direction, scope, priority)
        if identity in identities:
            raise ValueError("evaluation signal is duplicated")
        identities.add(identity)
        normalized.append({
            "lane": lane,
            "direction": direction,
            "scope": scope,
            "priority": priority,
            "strength": _number(
                raw.get("strength"),
                code="evaluation signal strength is invalid",
            ),
            "ratio": _number(
                raw.get("ratio"),
                code="evaluation signal ratio is invalid",
            ),
        })
    normalized.sort(
        key=lambda signal: (
            signal["lane"],
            signal["direction"],
            signal["scope"],
            signal["priority"],
        )
    )
    canonical(normalized)
    if not normalized:
        return normalized, None
    strongest = max(
        normalized,
        key=lambda signal: (
            signal["strength"],
            signal["lane"],
            signal["direction"],
            signal["scope"],
            signal["priority"],
        ),
    )
    strongest_signal = {
        **strongest,
        "strength": round(strongest["strength"], 6),
        "ratio": round(strongest["ratio"], 6),
    }
    return normalized, strongest_signal


def _ticket_combinations(values: Any, *, candidate: bool) -> list[str]:
    if not candidate:
        return []
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValueError("evaluation tickets are not an array")
    combinations: set[str] = set()
    for ticket in values:
        if not isinstance(ticket, dict):
            raise ValueError("evaluation ticket is not an object")
        combination = ticket.get("combo") or ticket.get("combination")
        if not isinstance(combination, str):
            raise ValueError("evaluation ticket combination is invalid")
        match = _COMBINATION.fullmatch(combination)
        if match is None or len(set(match.groups())) != 3:
            raise ValueError("evaluation ticket combination is invalid")
        if combination in combinations:
            raise ValueError("evaluation ticket is duplicated")
        combinations.add(combination)
    return sorted(combinations)


def build_full_evaluations(
    scored: Any,
    *,
    target_date: date,
    stage: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Build FULL_SCORED_DAY input; PostgreSQL remains decision authority."""
    if stage not in {"EARLY", "LATE"}:
        raise ValueError("evaluation stage is invalid")
    if not isinstance(target_date, date) or isinstance(target_date, datetime):
        raise ValueError("evaluation target date is invalid")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("evaluation clock must be timezone-aware")
    if not isinstance(scored, list) or len(scored) > 288:
        raise ValueError("evaluation scored input is invalid")

    evaluations: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for race in scored:
        if not isinstance(race, dict):
            raise ValueError("evaluation race is not an object")
        venue_code = _strict_int(race.get("venue_code"))
        race_no = _strict_int(race.get("race_no"))
        identity = (venue_code, race_no)
        if (
            venue_code not in range(1, 25)
            or race_no not in range(1, 13)
            or identity in seen
        ):
            raise ValueError("evaluation race identity is invalid")
        seen.add(identity)

        event_day, event_phase, event_day_label = _event_context(race)
        deadline = _deadline_at(target_date, race.get("deadline_time_jst"))
        deadline_hour = deadline.astimezone(_JST).hour
        stage_owned = (
            (stage == "EARLY" and deadline_hour < 10)
            or (stage == "LATE" and deadline_hour >= 10)
        )
        actionable = stage_owned and now < deadline - timedelta(minutes=10)
        morning_grade = race.get("race_grade")
        if morning_grade not in {"S", "A", "B"}:
            raise ValueError("evaluation grade is invalid")
        candidate = morning_grade in {"S", "A"}
        signals, strongest_signal = _signals(race.get("signals"))
        max_signal_strength = max(
            (signal["strength"] for signal in signals),
            default=0.0,
        )
        reason_codes = _codes(race.get("reason_codes"))
        if not candidate:
            reason_codes.extend(["MORNING_GRADE_B", "SA_THRESHOLD_NOT_MET"])
            if race.get("feature_degraded") is True:
                reason_codes.append("FEATURE_DEGRADED")
        reason_codes = sorted(set(reason_codes))

        evaluations.append({
            "venue_code": venue_code,
            "race_no": race_no,
            "event_day": event_day,
            "event_phase": event_phase,
            "event_day_label": event_day_label,
            "official_deadline_at": deadline.isoformat(),
            "morning_grade": morning_grade,
            "actionable": actionable,
            "actual_odds_evaluated": False,
            "ev_status": "EV_UNASSESSED",
            "purchase_decision": "SKIP",
            "max_signal_strength": round(max_signal_strength, 6),
            "sa_strength_threshold": SA_STRENGTH_THRESHOLD,
            "s_strength_threshold": S_STRENGTH_THRESHOLD,
            "reason_codes": reason_codes,
            "degradation_codes": _codes(race.get("degradation_codes")),
            "strongest_signal": strongest_signal,
            "signals": signals,
            "ticket_combinations": _ticket_combinations(
                race.get("tickets"),
                candidate=candidate,
            ),
        })

    evaluations.sort(key=lambda item: (item["venue_code"], item["race_no"]))
    canonical(evaluations)
    return evaluations


def v2_rpc_available(cur: Any) -> bool:
    """Use V2 only when both atomic append and non-mutating status exist."""
    cur.execute(
        """
        SELECT to_regprocedure(
                   'ux_app.append_morning_digest_v2(bigint,jsonb)'
               ) IS NOT NULL
           AND to_regprocedure(
                   'ux_app.morning_digest_v2_status(bigint)'
               ) IS NOT NULL
        """
    )
    row = cur.fetchone()
    return bool(row and row[0] is True)


def build_legacy_semantics(
    evaluations: list[dict[str, Any]],
    *,
    source_state: str,
    history_state: str,
    feature_state: str,
) -> dict[str, Any]:
    """Build the temporary V1-compatible late-scope comparison document."""
    if source_state not in {"SOURCE_OK", "PARTIAL_SOURCE"}:
        raise ValueError("legacy semantic source state is invalid")
    if history_state not in {"FULL_HISTORY", "DEGRADED_HISTORY"}:
        raise ValueError("legacy semantic history state is invalid")
    if feature_state not in {"FEATURE_OK", "FEATURE_DEGRADED"}:
        raise ValueError("legacy semantic feature state is invalid")
    late_rows: list[dict[str, Any]] = []
    for evaluation in evaluations:
        deadline = datetime.fromisoformat(evaluation["official_deadline_at"])
        if deadline.astimezone(_JST).hour < 10:
            continue
        strongest = evaluation.get("strongest_signal")
        strongest_identity = (
            {
                "lane": strongest["lane"],
                "direction": strongest["direction"],
                "scope": strongest["scope"],
                "priority": strongest["priority"],
            }
            if isinstance(strongest, dict)
            else None
        )
        grade = evaluation["morning_grade"]
        semantic_reasons = sorted({
            f"MORNING_GRADE_{grade}",
            *evaluation["reason_codes"],
        })
        late_rows.append({
            "venue_code": evaluation["venue_code"],
            "race_no": evaluation["race_no"],
            "event_day": evaluation["event_day"],
            "event_phase": evaluation["event_phase"],
            "event_day_label": evaluation["event_day_label"],
            "official_deadline_at": evaluation["official_deadline_at"],
            "morning_grade": grade,
            "decision_state": (
                "MORNING_CANDIDATE" if grade in {"S", "A"} else "SKIP"
            ),
            "semantic_reason_codes": semantic_reasons,
            "degradation_codes": evaluation["degradation_codes"],
            "strongest_signal_identity": strongest_identity,
            "ticket_combinations": evaluation["ticket_combinations"],
        })
    late_rows.sort(key=lambda row: (row["venue_code"], row["race_no"]))
    snapshot = {
        "schema_version": "boatrace-morning-late-semantic-v1",
        "semantic_policy_id": SEMANTIC_POLICY_ID,
        "quality": {
            "source_state": source_state,
            "history_state": history_state,
            "feature_state": feature_state,
        },
        "late_scope_count": len(late_rows),
        "late_rows": late_rows,
    }
    return {
        "status": "READY",
        "snapshot": snapshot,
        "sha256": hashlib.sha256(canonical(snapshot)).hexdigest(),
    }


def legacy_semantic_detail_fields(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "READY":
        return {
            _LEGACY_DETAIL_VERSION: SEMANTIC_POLICY_ID,
            _LEGACY_DETAIL_STATUS: "UNAVAILABLE",
        }
    return {
        _LEGACY_DETAIL_VERSION: SEMANTIC_POLICY_ID,
        _LEGACY_DETAIL_STATUS: "READY",
        _LEGACY_DETAIL_SNAPSHOT: result["snapshot"],
        _LEGACY_DETAIL_SHA256: result["sha256"],
    }


def _validated_legacy_details(
    details: Any,
) -> tuple[dict[str, Any], str] | None:
    if not isinstance(details, dict):
        return None
    if details.get(_LEGACY_DETAIL_VERSION) != SEMANTIC_POLICY_ID:
        return None
    if details.get(_LEGACY_DETAIL_STATUS) != "READY":
        return None
    snapshot = details.get(_LEGACY_DETAIL_SNAPSHOT)
    claimed_sha = details.get(_LEGACY_DETAIL_SHA256)
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("semantic_policy_id") != SEMANTIC_POLICY_ID:
        return None
    if not isinstance(claimed_sha, str) or re.fullmatch(
        r"[0-9a-f]{64}",
        claimed_sha,
    ) is None:
        return None
    if hashlib.sha256(canonical(snapshot)).hexdigest() != claimed_sha:
        return None
    return snapshot, claimed_sha


def _validated_legacy_result(
    result: Any,
) -> tuple[dict[str, Any], str] | None:
    if not isinstance(result, dict):
        return None
    return _validated_legacy_details(legacy_semantic_detail_fields(result))


def _not_suppressed(reason_code: str) -> dict[str, Any]:
    return {"suppressed": False, "reason_code": reason_code}


def _compare_legacy_with_accepted_early(
    cur: Any,
    *,
    run_execution_id: int,
    target_date: Any,
    current_semantics: tuple[dict[str, Any], str] | None,
) -> dict[str, Any]:
    target_text = target_date.isoformat() if isinstance(target_date, date) else str(target_date)
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"morning-late-diff-v1:{target_text}:self:LINE",),
    )
    cur.fetchone()
    cur.execute(
        """
        SELECT outbox.id
        FROM ux_app.notification_outbox AS outbox
        JOIN ux_app.run_executions AS owner_run
          ON owner_run.id = outbox.run_execution_id
        WHERE owner_run.target_date = %s
          AND owner_run.stage = 'LATE'
          AND outbox.event_kind = 'MORNING_DIGEST'
          AND outbox.channel = 'LINE'
          AND outbox.recipient_key = 'self'
        ORDER BY outbox.id
        LIMIT 1
        FOR SHARE OF outbox
        """,
        (target_date,),
    )
    if cur.fetchone() is not None:
        return _not_suppressed("LATE_OUTBOX_EXISTS")
    if current_semantics is None:
        return _not_suppressed("CURRENT_SEMANTICS_MISSING")
    cur.execute(
        """
        SELECT owner_run.id,
               outbox.id,
               outbox.status,
               btrim(outbox.payload_sha256::text),
               terminal.details
        FROM ux_app.notification_outbox AS outbox
        JOIN ux_app.run_executions AS owner_run
          ON owner_run.id = outbox.run_execution_id
        JOIN LATERAL (
            SELECT event.lifecycle_status, event.details
            FROM ux_app.run_events AS event
            WHERE event.run_execution_id = owner_run.id
            ORDER BY event.event_seq DESC
            LIMIT 1
        ) AS terminal ON true
        WHERE owner_run.target_date = %s
          AND owner_run.stage = 'EARLY'
          AND terminal.lifecycle_status = 'SUCCEEDED'
          AND outbox.event_kind = 'MORNING_DIGEST'
          AND outbox.channel = 'LINE'
          AND outbox.recipient_key = 'self'
          AND outbox.status = 'ACCEPTED'
        ORDER BY outbox.id
        LIMIT 1
        FOR SHARE OF outbox
        """,
        (target_date,),
    )
    baseline = cur.fetchone()
    if baseline is None:
        return _not_suppressed("ACCEPTED_EARLY_BASELINE_MISSING")
    baseline_run_id, baseline_outbox_id, baseline_status, payload_sha, details = baseline
    baseline_semantics = _validated_legacy_details(details)
    if baseline_semantics is None:
        return _not_suppressed("BASELINE_SEMANTICS_MISSING")
    if baseline_status != "ACCEPTED" or not isinstance(payload_sha, str) or re.fullmatch(
        r"[0-9a-f]{64}", payload_sha
    ) is None:
        return _not_suppressed("BASELINE_ACCEPTANCE_INVALID")
    current_snapshot, current_sha = current_semantics
    baseline_snapshot, baseline_sha = baseline_semantics
    if current_sha != baseline_sha or current_snapshot != baseline_snapshot:
        return _not_suppressed("MATERIAL_DECISION_CHANGE")
    if any(
        row.get("morning_grade") in {"S", "A"}
        for row in baseline_snapshot.get("late_rows", [])
    ):
        return _not_suppressed("BASELINE_CANDIDATE_NOT_PREVIOUSLY_PRESENTED")
    return {
        "schema_version": "boatrace-morning-late-suppression-compat-v1",
        "run_execution_id": int(run_execution_id),
        "decision_id": None,
        "outbox_id": None,
        "baseline_outbox_id": int(baseline_outbox_id),
        "inserted": False,
        "deduplicated": True,
        "replayed": False,
        "suppressed": True,
        "delivery_required": False,
        "canonical_delivery_pending": False,
        "decision_reason_code": SUPPRESSION_REASON,
        "status": SUPPRESSION_STATUS,
        "semantic_policy_id": SEMANTIC_POLICY_ID,
        "canonical_semantic_sha256": current_sha,
        "baseline_run_execution_id": int(baseline_run_id),
        "payload_sha256": payload_sha,
        "logical_target_date": target_text,
        "logical_stage": "LATE",
        "change_kinds": [],
    }


def evaluate_legacy_preterminal_suppression(
    cur: Any,
    *,
    run_execution_id: int,
    target_date: Any,
    stage: str,
    semantic_result: Any,
) -> dict[str, Any]:
    if stage != "LATE":
        return _not_suppressed("NOT_LATE")
    return _compare_legacy_with_accepted_early(
        cur,
        run_execution_id=run_execution_id,
        target_date=target_date,
        current_semantics=_validated_legacy_result(semantic_result),
    )


def evaluate_legacy_terminal_suppression(
    cur: Any,
    run_execution_id: int,
) -> dict[str, Any]:
    cur.execute(
        """
        SELECT execution.target_date, execution.stage,
               terminal.lifecycle_status, terminal.details
        FROM ux_app.run_executions AS execution
        JOIN LATERAL (
            SELECT event.lifecycle_status, event.details
            FROM ux_app.run_events AS event
            WHERE event.run_execution_id = execution.id
            ORDER BY event.event_seq DESC
            LIMIT 1
        ) AS terminal ON true
        WHERE execution.id = %s
        """,
        (run_execution_id,),
    )
    current = cur.fetchone()
    if current is None:
        return _not_suppressed("CURRENT_RUN_MISSING")
    target_date, stage, lifecycle_status, details = current
    if stage != "LATE":
        return _not_suppressed("NOT_LATE")
    if lifecycle_status != "SUCCEEDED":
        return _not_suppressed("CURRENT_RUN_NOT_SUCCEEDED")
    return _compare_legacy_with_accepted_early(
        cur,
        run_execution_id=run_execution_id,
        target_date=target_date,
        current_semantics=_validated_legacy_details(details),
    )


def legacy_suppression_audit_fields(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("suppressed") is True:
        if not validate_legacy_suppression_result(result):
            raise ValueError("legacy suppression result is invalid")
        return {
            "late_diff_notification_disposition": "SUPPRESSED",
            "late_diff_reason_code": SUPPRESSION_REASON,
            "late_diff_baseline_run_execution_id": result[
                "baseline_run_execution_id"
            ],
            "late_diff_baseline_outbox_id": result["baseline_outbox_id"],
        }
    reason = result.get("reason_code")
    if not isinstance(reason, str) or _SAFE_CODE.fullmatch(reason) is None:
        reason = "FAIL_OPEN_UNSPECIFIED"
    return {
        "late_diff_notification_disposition": "SEND",
        "late_diff_reason_code": reason,
    }


def validate_legacy_suppression_result(
    result: Any,
    *,
    expected_run_execution_id: int | None = None,
) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("schema_version") != "boatrace-morning-late-suppression-compat-v1":
        return False
    run_id = result.get("run_execution_id")
    if not isinstance(run_id, int) or run_id <= 0:
        return False
    if expected_run_execution_id is not None and run_id != expected_run_execution_id:
        return False
    if result.get("decision_id") is not None or result.get("outbox_id") is not None:
        return False
    if result.get("status") != SUPPRESSION_STATUS:
        return False
    if result.get("suppressed") is not True or result.get("delivery_required") is not False:
        return False
    if result.get("canonical_delivery_pending") is not False:
        return False
    if result.get("decision_reason_code") != SUPPRESSION_REASON:
        return False
    if result.get("semantic_policy_id") != SEMANTIC_POLICY_ID:
        return False
    if result.get("inserted") is not False or result.get("deduplicated") is not True:
        return False
    if result.get("replayed") is not False or result.get("change_kinds") != []:
        return False
    for key in ("baseline_run_execution_id", "baseline_outbox_id"):
        value = result.get(key)
        if not isinstance(value, int) or value <= 0:
            return False
    semantic_sha = result.get("canonical_semantic_sha256")
    payload_sha = result.get("payload_sha256")
    if not isinstance(semantic_sha, str) or re.fullmatch(r"[0-9a-f]{64}", semantic_sha) is None:
        return False
    if not isinstance(payload_sha, str) or re.fullmatch(r"[0-9a-f]{64}", payload_sha) is None:
        return False
    return True


def validate_suppression_result(
    result: Any,
    *,
    expected_run_execution_id: int | None = None,
) -> bool:
    """Validate only a DB-sealed no-outbox suppression result."""
    if not isinstance(result, dict):
        return False
    if result.get("schema_version") == "boatrace-morning-late-suppression-compat-v1":
        return validate_legacy_suppression_result(
            result,
            expected_run_execution_id=expected_run_execution_id,
        )
    if result.get("schema_version") != "boatrace-morning-digest-outbox-result-v4":
        return False
    run_id = result.get("run_execution_id")
    decision_id = result.get("decision_id")
    if not isinstance(run_id, int) or run_id <= 0:
        return False
    if expected_run_execution_id is not None and run_id != expected_run_execution_id:
        return False
    if not isinstance(decision_id, int) or decision_id <= 0:
        return False
    if result.get("outbox_id") is not None:
        return False
    if result.get("status") != SUPPRESSION_STATUS:
        return False
    if result.get("suppressed") is not True:
        return False
    if result.get("delivery_required") is not False:
        return False
    if result.get("canonical_delivery_pending") is not False:
        return False
    reason_code = result.get("decision_reason_code")
    if reason_code not in {
        SUPPRESSION_REASON,
        "STAGE_ALREADY_FINALIZED",
    }:
        return False
    if result.get("semantic_policy_id") != SEMANTIC_POLICY_ID:
        return False
    if result.get("deduplicated") is not True:
        return False
    inserted = result.get("inserted")
    replayed = result.get("replayed")
    if not isinstance(inserted, bool) or not isinstance(replayed, bool):
        return False
    if inserted == replayed:
        return False
    if reason_code == SUPPRESSION_REASON:
        for key in ("baseline_run_execution_id", "baseline_outbox_id"):
            value = result.get(key)
            if not isinstance(value, int) or value <= 0:
                return False
    elif (
        result.get("baseline_run_execution_id") is not None
        or result.get("baseline_outbox_id") is not None
    ):
        return False
    semantic_sha = result.get("canonical_semantic_sha256")
    if not isinstance(semantic_sha, str) or re.fullmatch(
        r"[0-9a-f]{64}",
        semantic_sha,
    ) is None:
        return False
    if result.get("change_kinds") != []:
        return False
    return True

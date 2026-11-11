-- Morning LATE: least-privilege read bridge for the legacy difference check.
-- Do NOT grant notification_outbox SELECT to the Morning runner.
-- Install only after verification on a cloned Neon branch.
CREATE OR REPLACE FUNCTION ux_app.morning_late_diff_context_v1(p_target_date date)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ux_app
AS $function$
DECLARE
    v_late_exists boolean;
    v_early jsonb;
BEGIN
    IF session_user NOT IN ('boatrace_morning_runner', 'neondb_owner') THEN
        RAISE EXCEPTION 'morning late context caller identity mismatch'
            USING ERRCODE = '42501';
    END IF;
    IF p_target_date IS NULL THEN
        RAISE EXCEPTION 'morning late context target date required';
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM ux_app.notification_outbox AS o
          JOIN ux_app.run_executions AS owner_run
            ON owner_run.id = o.run_execution_id
         WHERE owner_run.target_date = p_target_date
           AND owner_run.stage = 'LATE'
           AND o.event_kind = 'MORNING_DIGEST'
           AND o.channel = 'LINE'
           AND o.recipient_key = 'self'
    ) INTO v_late_exists;

    SELECT jsonb_build_object(
        'run_execution_id', owner_run.id,
        'outbox_id', o.id,
        'status', o.status,
        'payload_sha256', btrim(o.payload_sha256::text),
        'semantic_details', jsonb_build_object(
            'late_diff_semantic_version', terminal.details->'late_diff_semantic_version',
            'late_diff_semantic_status', terminal.details->'late_diff_semantic_status',
            'late_diff_semantic_snapshot', terminal.details->'late_diff_semantic_snapshot',
            'late_diff_semantic_sha256', terminal.details->'late_diff_semantic_sha256'
        )
    )
    INTO v_early
    FROM ux_app.notification_outbox AS o
    JOIN ux_app.run_executions AS owner_run
      ON owner_run.id = o.run_execution_id
    JOIN LATERAL (
        SELECT e.lifecycle_status, e.details
          FROM ux_app.run_events AS e
         WHERE e.run_execution_id = owner_run.id
         ORDER BY e.event_seq DESC
         LIMIT 1
    ) AS terminal ON true
    WHERE owner_run.target_date = p_target_date
      AND owner_run.stage = 'EARLY'
      AND terminal.lifecycle_status = 'SUCCEEDED'
      AND o.event_kind = 'MORNING_DIGEST'
      AND o.channel = 'LINE'
      AND o.recipient_key = 'self'
      AND o.status = 'ACCEPTED'
    ORDER BY o.id
    LIMIT 1;

    RETURN jsonb_build_object(
        'schema_version', 'morning-late-diff-context-v1',
        'late_outbox_exists', v_late_exists,
        'early_baseline', v_early
    );
END
$function$;

REVOKE ALL ON FUNCTION ux_app.morning_late_diff_context_v1(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ux_app.morning_late_diff_context_v1(date)
    TO boatrace_morning_runner;

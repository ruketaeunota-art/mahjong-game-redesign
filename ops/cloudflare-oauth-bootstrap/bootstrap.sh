#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${CLOUDFLARE_ACCOUNT_ID:?CLOUDFLARE_ACCOUNT_ID is required}"
: "${WORKER_NAME:?WORKER_NAME is required}"
: "${OPS_DIR:?OPS_DIR is required}"

WRANGLER="./node_modules/.bin/wrangler"
HEALTH_URL="https://boatrace-line-free.ruketaeunota.workers.dev/health"
EXPECTED_SERVICE_VERSION="0.2.6-skip-audit-v1"
PATCH_MARKER="BOATRACE_MORNING_V2_LIVEPATCH_20260825"
BOOTSTRAP_STAGE="initializing"
DEPLOYED_VERSION_ID=""
RESULT_STATUS="FAILED"
AUTH_STORED=false
HEALTH_STATUS="NOT_STARTED"

mkdir -p "$OPS_DIR"
rm -f "$OPS_DIR/device-session.json" "$OPS_DIR/result.json"
rm -rf /tmp/boatrace-line-bootstrap
mkdir -p /tmp/boatrace-line-bootstrap

write_result() {
  local exit_code="$1"
  EXIT_CODE="$exit_code" RESULT_STATUS="$RESULT_STATUS" BOOTSTRAP_STAGE="$BOOTSTRAP_STAGE" \
  DEPLOYED_VERSION_ID="$DEPLOYED_VERSION_ID" AUTH_STORED="$AUTH_STORED" HEALTH_STATUS="$HEALTH_STATUS" \
  python - <<'PY'
import json, os
from datetime import datetime, timezone
from pathlib import Path
payload = {
    "schema_version": "cloudflare-oauth-bootstrap-result-v1",
    "status": os.environ["RESULT_STATUS"],
    "exit_code": int(os.environ["EXIT_CODE"]),
    "stage": os.environ["BOOTSTRAP_STAGE"],
    "worker": os.environ["WORKER_NAME"],
    "account_id": os.environ["CLOUDFLARE_ACCOUNT_ID"],
    "authentication": "WRANGLER_DEVICE_FLOW_PERSISTED_IN_NEON_VAULT",
    "vault_store_success": os.environ["AUTH_STORED"] == "true",
    "cloudflare_version_id": os.environ["DEPLOYED_VERSION_ID"] or None,
    "health_status": os.environ["HEALTH_STATUS"],
    "expected_service_version": "0.2.6-skip-audit-v1",
    "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
    "source_commit_sha": os.environ.get("GITHUB_SHA"),
    "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    "secret_material_recorded_in_git": False,
}
Path(os.environ["OPS_DIR"], "result.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY
}

finalize() {
  local code=$?
  set +e
  if [ "$code" -eq 0 ]; then
    RESULT_STATUS="PASS"
  fi
  write_result "$code"
  git pull --rebase origin main >/dev/null 2>&1 || true
  rm -f "$OPS_DIR/device-session.json"
  git config user.name github-actions[bot]
  git config user.email 41898282+github-actions[bot]@users.noreply.github.com
  git add -A "$OPS_DIR"
  if ! git diff --cached --quiet; then
    git commit -m "Record Cloudflare OAuth bootstrap ${RESULT_STATUS}" >/dev/null 2>&1 || true
    for attempt in 1 2 3; do
      git push origin HEAD:main >/dev/null 2>&1 && break
      git pull --rebase origin main >/dev/null 2>&1 || true
    done
  fi
  rm -rf /tmp/boatrace-line-bootstrap /tmp/wrangler-device.log /tmp/wrangler-auth.json /tmp/wrangler-config.tar.gz
  exit "$code"
}
trap finalize EXIT

store_wrangler_config() {
  BOOTSTRAP_STAGE="persisting_oauth_credential"
  python - <<'PY'
from pathlib import Path
import tarfile
home = Path.home()
archive = Path('/tmp/wrangler-config.tar.gz')
roots = [home / '.config' / '.wrangler', home / '.wrangler']
added = 0
with tarfile.open(archive, 'w:gz') as handle:
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob('*')):
            if not path.is_file():
                continue
            relative = path.relative_to(home)
            if 'logs' in relative.parts:
                continue
            handle.add(path, arcname=str(relative), recursive=False)
            added += 1
if added == 0:
    raise SystemExit('WRANGLER_CONFIG_NOT_FOUND')
PY
  python - <<'PY'
import base64, json, os
from datetime import datetime, timezone
from pathlib import Path
payload = {
    'schema_version': 'wrangler-oauth-bundle-v1',
    'account_id': os.environ['CLOUDFLARE_ACCOUNT_ID'],
    'created_at_utc': datetime.now(timezone.utc).isoformat(),
    'wrangler_version': '4.123.0',
    'config_bundle_b64': base64.b64encode(Path('/tmp/wrangler-config.tar.gz').read_bytes()).decode('ascii'),
}
Path('/tmp/wrangler-auth.json').write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
PY
  python - <<'PY'
import json, os
from pathlib import Path
import psycopg
payload = Path('/tmp/wrangler-auth.json').read_text(encoding='utf-8')
with psycopg.connect(os.environ['DATABASE_URL']) as conn:
    with conn.cursor() as cur:
        cur.execute('SELECT public.boatrace_cloudflare_oauth_store(%s)', (payload,))
        result = cur.fetchone()[0]
    conn.commit()
print(json.dumps({'vault_store': True, 'payload_sha256': result['payload_sha256'], 'payload_bytes': result['payload_bytes']}))
PY
  AUTH_STORED=true
  rm -f /tmp/wrangler-auth.json /tmp/wrangler-config.tar.gz
}

BOOTSTRAP_STAGE="starting_device_authorization"
"$WRANGLER" login --device --browser=false --no-use-keyring > /tmp/wrangler-device.log 2>&1 &
login_pid=$!

verification_uri=""
user_code=""
for attempt in $(seq 1 120); do
  readarray -t parsed < <(python - <<'PY'
import re
from pathlib import Path
path = Path('/tmp/wrangler-device.log')
text = path.read_text(errors='replace') if path.exists() else ''
text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
uri = re.search(r'To authorize .*?please visit:\s*\n\s*(https://[^\s]+)', text, re.S)
code = re.search(r'and enter the code:\s*\n\s*([A-Za-z0-9-]+)', text, re.S)
print(uri.group(1).rstrip(').,') if uri else '')
print(code.group(1) if code else '')
PY
  )
  verification_uri="${parsed[0]:-}"
  user_code="${parsed[1]:-}"
  if [ -n "$verification_uri" ] && [ -n "$user_code" ]; then
    break
  fi
  if ! kill -0 "$login_pid" 2>/dev/null; then
    sed -E 's/[A-Za-z0-9_-]{32,}/[REDACTED]/g' /tmp/wrangler-device.log
    exit 31
  fi
  sleep 1
done
[ -n "$verification_uri" ]
[ -n "$user_code" ]

BOOTSTRAP_STAGE="waiting_for_user_authorization"
VERIFICATION_URI="$verification_uri" USER_CODE="$user_code" python - <<'PY'
import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
now = datetime.now(timezone.utc)
uri = os.environ['VERIFICATION_URI']
code = os.environ['USER_CODE']
separator = '&' if '?' in uri else '?'
payload = {
    'schema_version': 'cloudflare-device-session-persistent-v1',
    'status': 'WAITING_FOR_USER_AUTHORIZATION',
    'created_at_utc': now.isoformat(),
    'expires_at_utc': (now + timedelta(minutes=5)).isoformat(),
    'verification_uri': uri,
    'verification_uri_complete': uri + separator + urlencode({'user_code': code}),
    'user_code': code,
    'workflow_run_id': os.environ['GITHUB_RUN_ID'],
    'credential_destination': 'NEON_ENCRYPTED_VAULT',
    'repeat_authorization_expected': False,
}
Path(os.environ['OPS_DIR'], 'device-session.json').write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
)
PY

git config user.name github-actions[bot]
git config user.email 41898282+github-actions[bot]@users.noreply.github.com
git add "$OPS_DIR/device-session.json"
git commit -m 'Open persistent Cloudflare authorization session'
git push origin HEAD:main

authorized=false
for attempt in $(seq 1 150); do
  if grep -Fq 'Successfully logged in.' /tmp/wrangler-device.log 2>/dev/null; then
    authorized=true
    break
  fi
  if ! kill -0 "$login_pid" 2>/dev/null; then
    break
  fi
  sleep 2
done
if [ "$authorized" != true ]; then
  wait "$login_pid" || true
  sed -E 's/[A-Za-z0-9_-]{32,}/[REDACTED]/g' /tmp/wrangler-device.log
  exit 41
fi
wait "$login_pid"

BOOTSTRAP_STAGE="verifying_cloudflare_account"
"$WRANGLER" whoami > /tmp/wrangler-whoami.log
if ! grep -Fq "$CLOUDFLARE_ACCOUNT_ID" /tmp/wrangler-whoami.log; then
  cat /tmp/wrangler-whoami.log
  exit 42
fi

store_wrangler_config

BOOTSTRAP_STAGE="retrieving_current_worker"
auth_json="$("$WRANGLER" auth token --json)"
auth_type="$(jq -er '.type' <<<"$auth_json")"
token="$(jq -er '.token' <<<"$auth_json")"
[ "$auth_type" = "oauth" ]
[ -n "$token" ]
echo "::add-mask::$token"

curl --fail --silent --show-error --max-time 45 \
  --dump-header /tmp/boatrace-line-bootstrap/current.headers \
  --header "Authorization: Bearer $token" \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/workers/scripts/$WORKER_NAME/content/v2" \
  --output /tmp/boatrace-line-bootstrap/current.raw

BOOTSTRAP_STAGE="patching_current_worker"
python - <<'PY'
import email.policy
import re
from email.parser import BytesParser
from pathlib import Path

headers = Path('/tmp/boatrace-line-bootstrap/current.headers').read_text(errors='replace')
match = re.search(r'(?im)^content-type:\s*([^\r\n]+)', headers)
content_type = match.group(1).strip() if match else 'application/javascript'
raw = Path('/tmp/boatrace-line-bootstrap/current.raw').read_bytes()

if content_type.lower().startswith(('application/javascript', 'text/javascript', 'application/octet-stream')):
    script = raw
elif content_type.lower().startswith('multipart/'):
    message = BytesParser(policy=email.policy.default).parsebytes(
        f'MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n'.encode() + raw
    )
    candidates = []
    for part in message.iter_parts():
        filename = part.get_filename() or part.get_param('name', header='content-disposition') or ''
        part_type = part.get_content_type().lower()
        payload = part.get_payload(decode=True) or b''
        if filename.endswith(('.js', '.mjs')) or 'javascript' in part_type:
            candidates.append((len(payload), filename, payload))
    if not candidates:
        raise SystemExit('NO_JAVASCRIPT_MODULE_IN_WORKER_DOWNLOAD')
    candidates.sort(reverse=True)
    script = candidates[0][2]
else:
    raise SystemExit(f'UNSUPPORTED_WORKER_CONTENT_TYPE:{content_type}')

source = script.decode('utf-8')
marker = 'BOATRACE_MORNING_V2_LIVEPATCH_20260825'
if marker not in source:
    build = re.search(r'function\s+buildLineMessage\s*\(\s*notification\s*\)\s*\{', source)
    if build is None:
        raise SystemExit('BUILD_LINE_MESSAGE_MARKER_NOT_FOUND')
    helper = r'''
// BOATRACE_MORNING_V2_LIVEPATCH_20260825
function buildMorningV2Livepatch(payload) {
  const fallback = "【BOAT RACE 朝のレース選定】\n朝時点のBUY候補はありません。\n※実オッズEVはこの通知では未評価です";
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return { type: "text", text: fallback };
  }
  const canonical = typeof payload.canonical_text === "string" && payload.canonical_text.trim().length > 0
    ? payload.canonical_text.slice(0, 4900)
    : fallback;
  const lines = Array.isArray(payload.skip_summary_lines)
    ? payload.skip_summary_lines.filter((line) => typeof line === "string" && line.length > 0 && line.length <= 140).slice(0, 10)
    : [];
  if (lines.length === 0) return { type: "text", text: canonical };
  const targetDate = typeof payload.target_date === "string" ? payload.target_date : "当日";
  const stageLabel = payload.stage === "LATE" ? "朝の更新" : "早朝時点";
  const actionable = Number.isSafeInteger(payload.actionable_count) ? payload.actionable_count : lines.length;
  const rows = lines.map((line) => ({ type: "text", text: `・${line}`, margin: "sm", size: "sm", color: "#10223D", wrap: true }));
  return {
    type: "flex",
    altText: `BOAT RACE 朝のレース選定 見送り ${lines.length}件`,
    contents: {
      type: "bubble",
      size: "mega",
      body: {
        type: "box",
        layout: "vertical",
        backgroundColor: "#FFF9ED",
        paddingAll: "20px",
        contents: [
          { type: "text", text: "見送り", size: "sm", weight: "bold", color: "#61718A" },
          { type: "text", text: "朝のレース選定", margin: "md", size: "xl", weight: "bold", color: "#10223D", wrap: true },
          { type: "text", text: `${targetDate}・${stageLabel}`, margin: "sm", size: "sm", color: "#61718A", wrap: true },
          { type: "separator", margin: "lg", color: "#E0D7C7" },
          { type: "text", text: "朝時点のBUY候補はありません", margin: "lg", size: "md", weight: "bold", color: "#10223D", wrap: true },
          { type: "box", layout: "horizontal", margin: "md", contents: [
            { type: "text", text: "判定対象", size: "sm", color: "#61718A" },
            { type: "text", text: `${actionable}件`, size: "sm", weight: "bold", color: "#10223D", align: "end" }
          ] },
          { type: "separator", margin: "lg", color: "#E0D7C7" },
          { type: "text", text: "見送り内訳", margin: "lg", size: "sm", weight: "bold", color: "#10223D" },
          ...rows,
          { type: "separator", margin: "lg", color: "#E0D7C7" },
          { type: "text", text: "実オッズEVはこの通知では未評価です", margin: "lg", size: "sm", color: "#61718A", wrap: true }
        ]
      }
    }
  };
}

'''
    source = source[:build.start()] + helper + source[build.start():]
    branch = re.search(r'if\s*\(\s*notification\.eventKind\s*===\s*["\']MORNING_DIGEST["\']\s*\)\s*\{', source)
    if branch is None:
        raise SystemExit('MORNING_DIGEST_BRANCH_MARKER_NOT_FOUND')
    insertion = '''\n    if (notification.payload && typeof notification.payload === "object" && !Array.isArray(notification.payload) && notification.payload.presentation === "MORNING_CARD_V2") {\n      return buildMorningV2Livepatch(notification.payload);\n    }'''
    source = source[:branch.end()] + insertion + source[branch.end():]

health_pattern = re.compile(r'jsonResponse\(\{\s*status:\s*["\']ok["\']\s*,\s*enabled\s*\}\)')
if 'serviceVersion: env.SERVICE_VERSION' not in source:
    source, count = health_pattern.subn(
        'jsonResponse({ status: "ok", enabled, serviceName: env.SERVICE_NAME ?? "boatrace-line-dispatcher", serviceVersion: env.SERVICE_VERSION ?? "unknown" })',
        source,
        count=1,
    )
    if count != 1:
        raise SystemExit('HEALTH_RESPONSE_MARKER_NOT_FOUND')

if source.count(marker) != 1:
    raise SystemExit('LIVEPATCH_MARKER_COUNT_INVALID')
if 'buildMorningV2Livepatch(notification.payload)' not in source:
    raise SystemExit('LIVEPATCH_CALL_MISSING')
if 'serviceVersion: env.SERVICE_VERSION' not in source:
    raise SystemExit('HEALTH_VERSION_PATCH_MISSING')

Path('/tmp/boatrace-line-bootstrap/index.js').write_text(source, encoding='utf-8')
print({'downloaded_bytes': len(script), 'patched_bytes': len(source.encode('utf-8'))})
PY

cat > /tmp/boatrace-line-bootstrap/wrangler.jsonc <<'JSON'
{
  "$schema": "./node_modules/wrangler/config-schema.json",
  "name": "boatrace-line-free",
  "main": "index.js",
  "compatibility_date": "2026-08-15",
  "compatibility_flags": ["nodejs_compat"],
  "triggers": { "crons": ["* * * * *"] },
  "vars": {
    "DISPATCH_ENABLED": "true",
    "SERVICE_NAME": "boatrace-line-dispatcher",
    "SERVICE_VERSION": "0.2.6-skip-audit-v1",
    "BATCH_SIZE": "10",
    "LEASE_SECONDS": "45",
    "MAX_ATTEMPTS": "6",
    "LINE_TIMEOUT_MS": "8000",
    "RETRY_BASE_SECONDS": "30",
    "RETRY_MAX_SECONDS": "900"
  },
  "observability": {
    "enabled": true,
    "logs": { "enabled": true, "head_sampling_rate": 1, "invocation_logs": true },
    "traces": { "enabled": true, "head_sampling_rate": 0.01 }
  }
}
JSON

BOOTSTRAP_STAGE="deploying_worker"
(
  cd /tmp/boatrace-line-bootstrap
  "$GITHUB_WORKSPACE/node_modules/.bin/wrangler" deploy index.js \
    --config wrangler.jsonc \
    --no-bundle \
    --keep-vars \
    --message "Persisted OAuth bootstrap and Morning skip audit livepatch" \
    2>&1 | tee /tmp/boatrace-line-bootstrap/deploy.log
)
DEPLOYED_VERSION_ID="$(sed -nE 's/.*Current Version ID: ([0-9a-f-]{36}).*/\1/p' /tmp/boatrace-line-bootstrap/deploy.log | tail -1)"
if [ -z "$DEPLOYED_VERSION_ID" ]; then
  DEPLOYED_VERSION_ID="$("$WRANGLER" versions list --name "$WORKER_NAME" --json | jq -er 'if type=="array" then .[0].id // .[0].version_id else .items[0].id // .items[0].version_id end')"
fi
[[ "$DEPLOYED_VERSION_ID" =~ ^[0-9a-f-]{36}$ ]]

BOOTSTRAP_STAGE="verifying_production_health"
HEALTH_STATUS="RUNNING"
healthy=false
for attempt in $(seq 1 12); do
  http_code="$(curl -sS --max-time 15 -o /tmp/boatrace-line-bootstrap/health.json -w '%{http_code}' "$HEALTH_URL" || true)"
  if [ "$http_code" = "200" ] && jq -e --arg version "$EXPECTED_SERVICE_VERSION" '.status == "ok" and .enabled == true and .serviceName == "boatrace-line-dispatcher" and .serviceVersion == $version' /tmp/boatrace-line-bootstrap/health.json >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 3
done
if [ "$healthy" != true ]; then
  cat /tmp/boatrace-line-bootstrap/health.json 2>/dev/null || true
  HEALTH_STATUS="FAILED"
  exit 61
fi
HEALTH_STATUS="PASS"

BOOTSTRAP_STAGE="refreshing_persisted_oauth_credential"
store_wrangler_config

BOOTSTRAP_STAGE="completed"
RESULT_STATUS="PASS"
unset token auth_json
exit 0

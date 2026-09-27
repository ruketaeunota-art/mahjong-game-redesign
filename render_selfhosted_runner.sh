#!/usr/bin/env bash
set -Eeuo pipefail

: "${PORT:=10000}"
REPO_URL="https://github.com/ruketaeunota-art/x"
: "${RUNNER_LABELS:=ubuntu-24.04}"
: "${RUNNER_NAME_PREFIX:=render-v5}"

python3 -m http.server "${PORT}" --bind 0.0.0.0 >/tmp/render-runner-http.log 2>&1 &
HTTP_PID=$!
trap 'kill ${HTTP_PID} 2>/dev/null || true' EXIT

echo "RENDER_RUNNER_HTTP_READY port=${PORT}"
echo "RUNNER_TARGET_URL=${REPO_URL}"

if [[ -z "${RUNNER_TOKEN:-}" || "${RUNNER_TOKEN}" == "PENDING" ]]; then
  echo "RUNNER_TOKEN_REQUIRED"
  while true; do sleep 300; done
fi

if ! command -v gh >/dev/null 2>&1; then
  GH_ROOT="$PWD/.gh-cli"
  mkdir -p "$GH_ROOT"
  python3 - "$GH_ROOT" <<'PY'
import json, pathlib, shutil, sys, tarfile, tempfile, urllib.request
root = pathlib.Path(sys.argv[1])
req = urllib.request.Request(
    "https://api.github.com/repos/cli/cli/releases/latest",
    headers={"Accept": "application/vnd.github+json", "User-Agent": "render-selfhosted-runner"},
)
with urllib.request.urlopen(req) as r:
    release = json.load(r)
asset = next(
    a for a in release["assets"]
    if a["name"].endswith("_linux_amd64.tar.gz")
)
with tempfile.TemporaryDirectory() as td:
    archive = pathlib.Path(td) / asset["name"]
    urllib.request.urlretrieve(asset["browser_download_url"], archive)
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(td)
    gh = next(pathlib.Path(td).glob("gh_*/bin/gh"))
    shutil.copy2(gh, root / "gh")
(root / "gh").chmod(0o755)
print("GH_CLI_READY", release["tag_name"])
PY
  export PATH="$GH_ROOT:$PATH"
fi

gh --version | head -n 1

cd .actions-runner
export RUNNER_ALLOW_RUNASROOT=1
RUNNER_NAME="${RUNNER_NAME_PREFIX}-$(hostname)"

rm -f .runner .runner_migrated .credentials .credentials_migrated .credentials_rsaparams

./config.sh \
  --url "${REPO_URL}" \
  --token "${RUNNER_TOKEN}" \
  --name "${RUNNER_NAME}" \
  --work _work \
  --labels "${RUNNER_LABELS}" \
  --unattended \
  --replace \
  --ephemeral

echo "SELF_HOSTED_RUNNER_CONFIGURED name=${RUNNER_NAME} labels=${RUNNER_LABELS}"
exec ./run.sh

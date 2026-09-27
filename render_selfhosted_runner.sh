#!/usr/bin/env bash
set -Eeuo pipefail

: "${PORT:=10000}"
REPO_URL="https://github.com/ruketaeunota-art/x"
: "${RUNNER_LABELS:=ubuntu-24.04}"
: "${RUNNER_NAME_PREFIX:=render-v5}"
GH_VERSION="2.101.0"

python3 -m http.server "${PORT}" --bind 0.0.0.0 >/tmp/render-runner-http.log 2>&1 &
HTTP_PID=$!
trap 'kill ${HTTP_PID} 2>/dev/null || true' EXIT

echo "RENDER_RUNNER_HTTP_READY port=${PORT}"
echo "RUNNER_TARGET_URL=${REPO_URL}"

if [[ -z "${RUNNER_TOKEN:-}" || "${RUNNER_TOKEN}" == "PENDING" ]]; then
  echo "RUNNER_TOKEN_REQUIRED"
  while true; do sleep 300; done
fi

GH_ROOT="$PWD/.gh-cli"
if [[ ! -x "$GH_ROOT/gh" ]]; then
  mkdir -p "$GH_ROOT"
  python3 - "$GH_ROOT" "$GH_VERSION" <<'PY'
import pathlib, shutil, sys, tarfile, tempfile, urllib.request
root = pathlib.Path(sys.argv[1])
ver = sys.argv[2]
url = f"https://github.com/cli/cli/releases/download/v{ver}/gh_{ver}_linux_amd64.tar.gz"
with tempfile.TemporaryDirectory() as td:
    archive = pathlib.Path(td) / "gh.tar.gz"
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(td)
    gh = next(pathlib.Path(td).glob("gh_*/bin/gh"))
    shutil.copy2(gh, root / "gh")
(root / "gh").chmod(0o755)
PY
fi
export PATH="$GH_ROOT:$PATH"
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
  --replace

echo "SELF_HOSTED_RUNNER_CONFIGURED name=${RUNNER_NAME} labels=${RUNNER_LABELS}"
exec ./run.sh

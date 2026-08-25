from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


EXPECTED_SCHEMA_VERSION = "boatrace-morning-runtime-manifest-v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="runtime_manifest.json")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    contract_version = manifest.get("contract_version")
    schema_version = manifest.get("schema_version")

    if schema_version != EXPECTED_SCHEMA_VERSION:
        raise SystemExit("runtime manifest schema is invalid")
    if not isinstance(contract_version, str) or not contract_version:
        raise SystemExit("runtime manifest has no contract version")
    if not isinstance(files, dict) or not files:
        raise SystemExit("runtime manifest has no files")

    results: list[dict[str, Any]] = []
    failed = False
    for relative_name, expected in sorted(files.items()):
        path = manifest_path.parent / relative_name
        actual = sha256(path) if path.is_file() else None
        ok = actual == expected
        results.append(
            {
                "file": relative_name,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "ok": ok,
            }
        )
        failed = failed or not ok

    wrapper_path = manifest_path.parent / "run_with_digest.py"
    wrapper = wrapper_path.read_text(encoding="utf-8")
    binding_ok = "import audit_overlay" in wrapper
    failed = failed or not binding_ok

    version_path = manifest_path.parent / "version.txt"
    runtime_version = (
        version_path.read_text(encoding="utf-8").strip()
        if version_path.is_file()
        else None
    )
    version_ok = runtime_version == contract_version
    failed = failed or not version_ok

    required_files = {
        "runner.py",
        "production.py",
        "audit_overlay.py",
        "run_with_digest.py",
        "stage_guard.py",
        "stage_lock.py",
        "delivery_gate.py",
        "verify_runtime_manifest.py",
        "requirements.txt",
        "version.txt",
    }
    coverage_ok = required_files.issubset(files)
    failed = failed or not coverage_ok

    payload = {
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "contract_version": contract_version,
        "runtime_version": runtime_version,
        "status": "FAIL" if failed else "PASS",
        "audit_overlay_binding": binding_ok,
        "version_match": version_ok,
        "required_file_coverage": coverage_ok,
        "files": results,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

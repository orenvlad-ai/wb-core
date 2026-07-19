"""Bounded repo-owned reconciliation after an indeterminate SSH deploy disconnect."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from apps.registry_upload_http_entrypoint_hosted_runtime import (
    load_hosted_runtime_target,
    _validate_production_target_identity,
)


def reconcile(*, target_file: Path, expected_sha: str, pr: int, head: str, merge: str) -> dict[str, object]:
    target = load_hosted_runtime_target(target_file)
    _validate_production_target_identity(target, action=f"reconcile PR #{pr}")
    if len(expected_sha) != 40 or len(head) != 40 or len(merge) != 40:
        raise ValueError("expected-sha, head and merge must be exact 40-character SHA values")
    command = (
        "set +e; f=" + target.target_dir.rstrip("/") + "/.wb-core-deploy.json; "
        "meta=$(python3 -c 'import json; print(json.load(open(\"'" + target.target_dir.rstrip("/") + "/.wb-core-deploy.json\"))[\"commit\"])' 2>/dev/null); "
        "unit=$(systemctl is-active " + target.service_name + " 2>/dev/null); "
        "pid=$(systemctl show " + target.service_name + " -p MainPID --value 2>/dev/null); "
        "daemon=$(systemctl daemon-reload >/dev/null 2>&1; echo $?); "
        "probe=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/v1/sheet-vitrina-v1/status 2>/dev/null); "
        "printf '{\"metadata_sha\":\"%s\",\"unit\":\"%s\",\"pid\":\"%s\",\"daemon_reload_rc\":%s,\"probe_status\":\"%s\"}\n' \"$meta\" \"$unit\" \"$pid\" \"$daemon\" \"$probe\""
    )
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", target.ssh_destination, command], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("transport-indeterminate: bounded SSH readback failed")
    evidence = json.loads(result.stdout)
    healthy = (
        evidence.get("metadata_sha") == expected_sha
        and evidence.get("unit") == "active"
        and evidence.get("daemon_reload_rc") == 0
        and str(evidence.get("probe_status")) in {"200", "401", "403"}
    )
    return {"pr": pr, "head": head, "merge": merge, "expected_sha": expected_sha, "healthy": healthy, "evidence": evidence}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--merge", required=True)
    args = parser.parse_args()
    print(json.dumps(reconcile(target_file=args.target_file, expected_sha=args.expected_sha, pr=args.pr, head=args.head, merge=args.merge), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded exact-SHA reconciliation after an indeterminate SSH disconnect.

Only daemon-reload, service restart, probes and readback may be retried here.  File
sync, metadata writes and dependency installation deliberately remain fail-closed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.registry_upload_http_entrypoint_hosted_runtime import (
    HostedRuntimeTarget,
    _validate_production_target_identity,
    load_hosted_runtime_target,
)


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_RETRY_STAGES = frozenset({"daemon-reload", "restart", "probes", "readback"})
TRANSPORT_INDETERMINATE_RETURN_CODES = frozenset({255})
DEFAULT_ATTEMPTS = 3


@dataclass(frozen=True)
class ReconcileEvidence:
    metadata_sha: str
    runtime_sha: str
    unit: str
    main_pid: int
    probe_statuses: tuple[int, ...]
    target_id: str
    auth_env_ok: bool

    @property
    def mixed_deployment(self) -> bool:
        return bool(self.metadata_sha and self.runtime_sha and self.metadata_sha != self.runtime_sha)

    def healthy_for(self, expected_sha: str, expected_target_id: str) -> bool:
        return (
            self.target_id == expected_target_id
            and self.auth_env_ok
            and self.metadata_sha == expected_sha
            and self.runtime_sha == expected_sha
            and not self.mixed_deployment
            and self.unit == "active"
            and self.main_pid > 0
            and bool(self.probe_statuses)
            # Authenticated JSON routes reject unauthenticated loopback probes
            # with 401/403, while protected HTML routes use the canonical 303
            # redirect to the login form.  All four responses prove that the
            # exact application process is serving the expected auth boundary.
            and all(status in {200, 303, 401, 403} for status in self.probe_statuses)
        )


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _exact_sha(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if not SHA_RE.fullmatch(normalized):
        raise ValueError(f"{name} must be an exact 40-character SHA")
    return normalized


def _remote_command(target: HostedRuntimeTarget, operation: str) -> list[str]:
    target_dir = shlex.quote(target.target_dir.rstrip("/"))
    service = shlex.quote(target.service_name)
    target_id = shlex.quote(target.target_id)
    environment_file = shlex.quote(target.environment_file)
    paths = tuple(
        dict.fromkeys(
            (
                target.route_paths.get("SHEET_VITRINA_STATUS_HTTP_PATH")
                or "/v1/sheet-vitrina-v1/status",
                target.route_paths.get("SHEET_VITRINA_OPERATOR_UI_PATH")
                or "/sheet-vitrina-v1/operator",
            )
        )
    )
    path_words = " ".join(shlex.quote(path) for path in paths)
    if operation == "readback":
        shell = (
            "set +e; d=" + target_dir + "; "
            "meta=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[\"commit\"])' "
            '"$d/.wb-core-deploy.json" 2>/dev/null); '
            'runtime=$(tr -d "\\r\\n" < "$d/.wb-core-runtime-sha" 2>/dev/null); '
            f"unit=$(systemctl is-active {service} 2>/dev/null); "
            f"pid=$(systemctl show {service} -p MainPID --value 2>/dev/null); "
            "auth=false; if test -r " + environment_file + "; then "
            "auth=true; for k in WB_CORE_WEB_AUTH_USERNAME WB_CORE_WEB_AUTH_PASSWORD_HASH "
            "WB_CORE_WEB_AUTH_SESSION_SECRET; do "
            "grep -Eq \"^${k}=[^[:space:]]+\" " + environment_file + " || auth=false; done; fi; "
            'probes=""; for p in ' + path_words + "; do "
            "code=$(curl -sS -o /dev/null -w '%{http_code}' \"http://127.0.0.1:8765${p}\" 2>/dev/null); "
            'probes="${probes}${probes:+,}${code:-000}"; done; '
            "python3 - \"$meta\" \"$runtime\" \"$unit\" \"$pid\" \"$probes\" \"$auth\" "
            + target_id
            + " <<'PY'\n"
            "import json,sys\n"
            "print(json.dumps({'metadata_sha':sys.argv[1], 'runtime_sha':sys.argv[2], "
            "'unit':sys.argv[3], 'main_pid':sys.argv[4], 'probe_statuses':sys.argv[5], "
            "'auth_env_ok':sys.argv[6] == 'true', 'target_id':sys.argv[7]}, sort_keys=True))\nPY"
        )
    elif operation == "daemon-reload":
        shell = "systemctl daemon-reload"
    elif operation == "restart":
        shell = f"systemctl restart {service}"
    elif operation == "probes":
        shell = (
            "set -e; for p in " + path_words + "; do "
            "curl -fsS -o /dev/null \"http://127.0.0.1:8765${p}\"; done"
        )
    else:
        raise ValueError(f"unsafe reconciliation operation: {operation}")
    return ["ssh", "-o", "BatchMode=yes", target.ssh_destination, shell]


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _parse_evidence(payload: str) -> ReconcileEvidence:
    raw = json.loads(payload)
    statuses = tuple(
        int(value) for value in str(raw.get("probe_statuses") or "").split(",") if value
    )
    try:
        main_pid = int(raw.get("main_pid") or 0)
    except (TypeError, ValueError):
        main_pid = 0
    return ReconcileEvidence(
        metadata_sha=str(raw.get("metadata_sha") or "").strip().lower(),
        runtime_sha=str(raw.get("runtime_sha") or "").strip().lower(),
        unit=str(raw.get("unit") or "").strip(),
        main_pid=main_pid,
        probe_statuses=statuses,
        target_id=str(raw.get("target_id") or "").strip(),
        auth_env_ok=bool(raw.get("auth_env_ok")),
    )


def classify_disconnect(return_code: int) -> str:
    return "transport-indeterminate" if return_code in TRANSPORT_INDETERMINATE_RETURN_CODES else "failed"


def reconcile(
    *,
    target_file: Path,
    expected_sha: str,
    pr: int,
    head: str,
    merge: str,
    failed_stage: str = "readback",
    attempts: int = DEFAULT_ATTEMPTS,
    runner: Runner = _default_runner,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    target = load_hosted_runtime_target(target_file)
    _validate_production_target_identity(target, action=f"reconcile PR #{pr}")
    expected = _exact_sha(expected_sha, "expected-sha")
    exact_head = _exact_sha(head, "head")
    exact_merge = _exact_sha(merge, "merge")
    if expected != exact_merge:
        raise ValueError("expected-sha must equal the exact merge SHA")
    if attempts <= 0 or attempts > 5:
        raise ValueError("attempts must be between 1 and 5")

    history: list[dict[str, Any]] = []
    repairs_applied = False
    for attempt in range(1, attempts + 1):
        readback = runner(_remote_command(target, "readback"))
        if readback.returncode == 0:
            evidence = _parse_evidence(readback.stdout)
            history.append({"attempt": attempt, "operation": "readback", **evidence.__dict__})
            if evidence.healthy_for(expected, target.target_id):
                return {
                    "status": "reconciled",
                    "transport": "transport-indeterminate",
                    "pr": pr,
                    "head": exact_head,
                    "merge": exact_merge,
                    "expected_sha": expected,
                    "target_id": target.target_id,
                    "healthy": True,
                    "mixed_deployment": False,
                    "repairs_applied": repairs_applied,
                    "attempts": attempt,
                    "evidence": history,
                }
            # Wrong or mixed SHA is never repairable by restarting the same files.
            if (
                evidence.metadata_sha != expected
                or evidence.runtime_sha != expected
                or evidence.mixed_deployment
                or not evidence.auth_env_ok
            ):
                break
            if failed_stage in SAFE_RETRY_STAGES or evidence.unit != "active" or evidence.main_pid <= 0:
                for operation in ("daemon-reload", "restart", "probes"):
                    result = runner(_remote_command(target, operation))
                    history.append(
                        {"attempt": attempt, "operation": operation, "returncode": result.returncode}
                    )
                    if result.returncode not in ({0} | TRANSPORT_INDETERMINATE_RETURN_CODES):
                        break
                repairs_applied = True
        else:
            history.append(
                {"attempt": attempt, "operation": "readback", "returncode": readback.returncode}
            )
        if attempt < attempts:
            sleep(min(float(attempt), 2.0))

    return {
        "status": "halted",
        "transport": "transport-indeterminate",
        "pr": pr,
        "head": exact_head,
        "merge": exact_merge,
        "expected_sha": expected,
        "target_id": target.target_id,
        "healthy": False,
        "repairs_applied": repairs_applied,
        "attempts": len({int(item["attempt"]) for item in history}),
        "evidence": history,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--merge", required=True)
    parser.add_argument("--failed-stage", choices=sorted(SAFE_RETRY_STAGES | {"metadata", "sync"}), default="readback")
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = reconcile(
        target_file=args.target_file,
        expected_sha=args.expected_sha,
        pr=args.pr,
        head=args.head,
        merge=args.merge,
        failed_stage=args.failed_stage,
        attempts=args.attempts,
    )
    rendered = json.dumps(payload, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["healthy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

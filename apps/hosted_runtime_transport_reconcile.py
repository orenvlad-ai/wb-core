"""Bounded exact-SHA reconciliation after an indeterminate SSH disconnect.

Only daemon-reload, service restart, probes and readback may be retried here.
File sync and dependency installation deliberately remain fail-closed. A
separate explicit safe-finalize lane may CAS only an incomplete completion bit
after exact metadata/runtime SHA, immutable metadata bytes, auth, process and
probe evidence all agree. The explicit read-only mode disables every repair and
is used when Release Train needs deployment proof without runtime mutation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
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
DEFAULT_BACKOFF_SECONDS = (0.0, 15.0, 45.0, 120.0, 300.0)
SAFE_FINALIZE_CONTRACT = "wb_core_deploy_safe_finalize_v1"
DEPLOY_METADATA_SCHEMA = "wb_core_deploy_metadata_v2"


@dataclass(frozen=True)
class ReconcileEvidence:
    metadata_sha: str
    runtime_sha: str
    metadata_schema_version: str
    metadata_deployed_at: str
    metadata_sha256: str
    runtime_sha256: str
    deployment_complete: bool
    unit: str
    main_pid: int
    probe_statuses: tuple[int, ...]
    target_id: str
    auth_env_ok: bool

    @property
    def mixed_deployment(self) -> bool:
        return bool(self.metadata_sha and self.runtime_sha and self.metadata_sha != self.runtime_sha)

    def healthy_for(
        self,
        expected_sha: str,
        expected_target_id: str,
        *,
        require_deployment_complete: bool,
    ) -> bool:
        return (
            self.target_id == expected_target_id
            and self.auth_env_ok
            and self.metadata_sha == expected_sha
            and self.runtime_sha == expected_sha
            and not self.mixed_deployment
            and (self.deployment_complete or not require_deployment_complete)
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


def _remote_command(
    target: HostedRuntimeTarget,
    operation: str,
    *,
    expected_sha: str = "",
    expected_metadata_sha256: str = "",
    expected_runtime_sha256: str = "",
    expected_main_pid: int = 0,
    expected_post_metadata_sha256: str = "",
) -> list[str]:
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
            "complete=$(python3 -c 'import json,sys; print(\"true\" if json.load(open(sys.argv[1])).get(\"deployment_complete\") is True else \"false\")' "
            '"$d/.wb-core-deploy.json" 2>/dev/null); '
            "schema=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(\"schema_version\",\"\"))' "
            '"$d/.wb-core-deploy.json" 2>/dev/null); '
            "deployed=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(\"deployed_at\",\"\"))' "
            '"$d/.wb-core-deploy.json" 2>/dev/null); '
            "meta_hash=$(sha256sum \"$d/.wb-core-deploy.json\" 2>/dev/null | cut -d' ' -f1); "
            'runtime=$(tr -d "\\r\\n" < "$d/.wb-core-runtime-sha" 2>/dev/null); '
            "runtime_hash=$(sha256sum \"$d/.wb-core-runtime-sha\" 2>/dev/null | cut -d' ' -f1); "
            f"unit=$(systemctl is-active {service} 2>/dev/null); "
            f"pid=$(systemctl show {service} -p MainPID --value 2>/dev/null); "
            "auth=false; if test -r " + environment_file + "; then "
            "auth=true; for k in WB_CORE_WEB_AUTH_USERNAME WB_CORE_WEB_AUTH_PASSWORD_HASH "
            "WB_CORE_WEB_AUTH_SESSION_SECRET; do "
            "grep -Eq \"^${k}=[^[:space:]]+\" " + environment_file + " || auth=false; done; fi; "
            'probes=""; for p in ' + path_words + "; do "
            "code=$(curl -sS -o /dev/null -w '%{http_code}' \"http://127.0.0.1:8765${p}\" 2>/dev/null); "
            'probes="${probes}${probes:+,}${code:-000}"; done; '
            "python3 - \"$meta\" \"$runtime\" \"$schema\" \"$deployed\" \"$meta_hash\" \"$runtime_hash\" \"$complete\" \"$unit\" \"$pid\" \"$probes\" \"$auth\" "
            + target_id
            + " <<'PY'\n"
            "import json,sys\n"
            "print(json.dumps({'metadata_sha':sys.argv[1], 'runtime_sha':sys.argv[2], "
            "'metadata_schema_version':sys.argv[3], 'metadata_deployed_at':sys.argv[4], "
            "'metadata_sha256':sys.argv[5], 'runtime_sha256':sys.argv[6], "
            "'deployment_complete':sys.argv[7] == 'true', 'unit':sys.argv[8], "
            "'main_pid':sys.argv[9], 'probe_statuses':sys.argv[10], "
            "'auth_env_ok':sys.argv[11] == 'true', 'target_id':sys.argv[12]}, sort_keys=True))\nPY"
        )
    elif operation == "safe-finalize":
        if (
            not SHA_RE.fullmatch(expected_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_metadata_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_runtime_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_post_metadata_sha256)
            or expected_main_pid <= 0
        ):
            raise ValueError("safe-finalize requires exact immutable CAS evidence")
        shell = (
            "set -e; d=" + target_dir + "; "
            f'test "$(systemctl is-active {service})" = active; '
            f'test "$(systemctl show {service} -p MainPID --value)" = {expected_main_pid}; '
            "auth=true; test -r " + environment_file + "; "
            "for k in WB_CORE_WEB_AUTH_USERNAME WB_CORE_WEB_AUTH_PASSWORD_HASH "
            "WB_CORE_WEB_AUTH_SESSION_SECRET; do "
            "grep -Eq \"^${k}=[^[:space:]]+\" " + environment_file + "; done; "
            "for p in " + path_words + "; do "
            "code=$(curl -sS -o /dev/null -w '%{http_code}' \"http://127.0.0.1:8765${p}\"); "
            "case \"$code\" in 200|303|401|403) ;; *) exit 41 ;; esac; done; "
            "python3 - \"$d/.wb-core-deploy.json\" \"$d/.wb-core-runtime-sha\" "
            + shlex.quote(expected_sha)
            + " "
            + shlex.quote(expected_metadata_sha256)
            + " "
            + shlex.quote(expected_runtime_sha256)
            + " "
            + shlex.quote(expected_post_metadata_sha256)
            + " <<'PY'\n"
            "import hashlib,json,os,pathlib,sys\n"
            "meta_path=pathlib.Path(sys.argv[1]); runtime_path=pathlib.Path(sys.argv[2])\n"
            "expected_sha,expected_meta,expected_runtime,expected_post=sys.argv[3:7]\n"
            "raw=meta_path.read_bytes(); runtime_raw=runtime_path.read_bytes()\n"
            "if hashlib.sha256(raw).hexdigest()!=expected_meta: raise SystemExit('metadata CAS drift')\n"
            "if hashlib.sha256(runtime_raw).hexdigest()!=expected_runtime: raise SystemExit('runtime marker CAS drift')\n"
            "payload=json.loads(raw); allowed={'schema_version','commit','deployed_at','deployment_complete'}\n"
            "if set(payload)!=allowed or payload.get('schema_version')!='wb_core_deploy_metadata_v2': raise SystemExit('metadata schema drift')\n"
            "if payload.get('commit')!=expected_sha or payload.get('deployment_complete') is not False: raise SystemExit('metadata state drift')\n"
            "if runtime_raw.decode('utf-8').strip()!=expected_sha: raise SystemExit('runtime SHA drift')\n"
            "payload['deployment_complete']=True\n"
            "post=(json.dumps(payload,ensure_ascii=True,sort_keys=True,separators=(',',':'))+'\\n').encode('utf-8')\n"
            "if hashlib.sha256(post).hexdigest()!=expected_post: raise SystemExit('post metadata digest drift')\n"
            "tmp=meta_path.with_name(meta_path.name+'.safe-finalize.'+str(os.getpid())+'.tmp')\n"
            "try:\n"
            " fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o644)\n"
            " with os.fdopen(fd,'wb') as handle: handle.write(post); handle.flush(); os.fsync(handle.fileno())\n"
            " os.replace(tmp,meta_path)\n"
            " dirfd=os.open(meta_path.parent,os.O_RDONLY); os.fsync(dirfd); os.close(dirfd)\n"
            "finally:\n"
            " try: tmp.unlink()\n"
            " except FileNotFoundError: pass\n"
            "print(json.dumps({'status':'finalized','metadata_sha256':expected_post},sort_keys=True))\nPY"
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
        metadata_schema_version=str(
            raw.get("metadata_schema_version") or ""
        ).strip(),
        metadata_deployed_at=str(raw.get("metadata_deployed_at") or "").strip(),
        metadata_sha256=str(raw.get("metadata_sha256") or "").strip().lower(),
        runtime_sha256=str(raw.get("runtime_sha256") or "").strip().lower(),
        deployment_complete=bool(raw.get("deployment_complete")),
        unit=str(raw.get("unit") or "").strip(),
        main_pid=main_pid,
        probe_statuses=statuses,
        target_id=str(raw.get("target_id") or "").strip(),
        auth_env_ok=bool(raw.get("auth_env_ok")),
    )


def _evidence_fingerprint(value: Mapping[str, Any]) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _build_safe_finalize_plan(
    *,
    evidence: ReconcileEvidence,
    expected_sha: str,
    pr: int,
    head: str,
    merge: str,
    target: HostedRuntimeTarget,
) -> dict[str, Any]:
    if (
        not evidence.healthy_for(
            expected_sha,
            target.target_id,
            require_deployment_complete=False,
        )
        or evidence.deployment_complete
        or evidence.metadata_schema_version != DEPLOY_METADATA_SCHEMA
        or not evidence.metadata_deployed_at
        or not re.fullmatch(r"[0-9a-f]{64}", evidence.metadata_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", evidence.runtime_sha256)
    ):
        raise ValueError("incomplete deploy is not eligible for safe-finalize")
    post_payload = {
        "schema_version": DEPLOY_METADATA_SCHEMA,
        "commit": expected_sha,
        "deployed_at": evidence.metadata_deployed_at,
        "deployment_complete": True,
    }
    post_bytes = (
        json.dumps(
            post_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    plan: dict[str, Any] = {
        "contract_name": SAFE_FINALIZE_CONTRACT,
        "contract_version": 1,
        "pr": pr,
        "head": head,
        "merge": merge,
        "expected_sha": expected_sha,
        "target_id": target.target_id,
        "service_name": target.service_name,
        "precondition": dict(evidence.__dict__),
        "expected_effects": {
            "metadata_completion_cas_count": 1,
            "rsync_count": 0,
            "dependency_install_count": 0,
            "service_restart_count": 0,
            "business_data_mutation_count": 0,
            "post_metadata_sha256": hashlib.sha256(post_bytes).hexdigest(),
        },
    }
    plan["fingerprint"] = _evidence_fingerprint(plan)
    return plan


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
    require_deployment_complete: bool = True,
    allow_repairs: bool = True,
    allow_safe_finalize: bool = False,
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
    safe_finalize_applied = False
    safe_finalize_plan: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        readback = runner(_remote_command(target, "readback"))
        if readback.returncode == 0:
            evidence = _parse_evidence(readback.stdout)
            history.append({"attempt": attempt, "operation": "readback", **evidence.__dict__})
            if evidence.healthy_for(
                expected,
                target.target_id,
                require_deployment_complete=require_deployment_complete,
            ):
                return {
                    "status": "reconciled",
                    "transport": "transport-indeterminate",
                    "pr": pr,
                    "head": exact_head,
                    "merge": exact_merge,
                    "expected_sha": expected,
                    "target_id": target.target_id,
                    "service_name": target.service_name,
                    "healthy": True,
                    "mixed_deployment": False,
                    "repairs_applied": repairs_applied,
                    "safe_finalize_applied": safe_finalize_applied,
                    "safe_finalize_plan": safe_finalize_plan,
                    "read_only": not allow_repairs and not allow_safe_finalize,
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
            # Exact files can be visible before the deploy transaction writes its
            # final completion marker.  This is a settling state, not proof of a
            # wrong deployment. The explicit safe-finalize lane may change only
            # that bit through an immutable metadata/process/probe CAS.
            if require_deployment_complete and not evidence.deployment_complete:
                if allow_safe_finalize:
                    try:
                        safe_finalize_plan = _build_safe_finalize_plan(
                            evidence=evidence,
                            expected_sha=expected,
                            pr=pr,
                            head=exact_head,
                            merge=exact_merge,
                            target=target,
                        )
                    except ValueError as exc:
                        history.append(
                            {
                                "attempt": attempt,
                                "operation": "safe-finalize-plan",
                                "status": "blocked",
                                "error": str(exc),
                            }
                        )
                        break
                    history.append(
                        {
                            "attempt": attempt,
                            "operation": "safe-finalize-plan",
                            "fingerprint": safe_finalize_plan["fingerprint"],
                            "expected_effects": safe_finalize_plan[
                                "expected_effects"
                            ],
                        }
                    )
                    finalized = runner(
                        _remote_command(
                            target,
                            "safe-finalize",
                            expected_sha=expected,
                            expected_metadata_sha256=evidence.metadata_sha256,
                            expected_runtime_sha256=evidence.runtime_sha256,
                            expected_main_pid=evidence.main_pid,
                            expected_post_metadata_sha256=str(
                                safe_finalize_plan["expected_effects"][
                                    "post_metadata_sha256"
                                ]
                            ),
                        )
                    )
                    history.append(
                        {
                            "attempt": attempt,
                            "operation": "safe-finalize-cas",
                            "returncode": finalized.returncode,
                        }
                    )
                    if finalized.returncode not in (
                        {0} | TRANSPORT_INDETERMINATE_RETURN_CODES
                    ):
                        break
                    post_readback = runner(_remote_command(target, "readback"))
                    if post_readback.returncode != 0:
                        history.append(
                            {
                                "attempt": attempt,
                                "operation": "safe-finalize-readback",
                                "returncode": post_readback.returncode,
                            }
                        )
                        break
                    post = _parse_evidence(post_readback.stdout)
                    history.append(
                        {
                            "attempt": attempt,
                            "operation": "safe-finalize-readback",
                            **post.__dict__,
                        }
                    )
                    expected_post_hash = str(
                        safe_finalize_plan["expected_effects"][
                            "post_metadata_sha256"
                        ]
                    )
                    if (
                        post.healthy_for(
                            expected,
                            target.target_id,
                            require_deployment_complete=True,
                        )
                        and post.metadata_sha256 == expected_post_hash
                        and post.runtime_sha256 == evidence.runtime_sha256
                    ):
                        safe_finalize_applied = True
                        return {
                            "status": "reconciled",
                            "transport": "transport-indeterminate",
                            "pr": pr,
                            "head": exact_head,
                            "merge": exact_merge,
                            "expected_sha": expected,
                            "target_id": target.target_id,
                            "service_name": target.service_name,
                            "healthy": True,
                            "mixed_deployment": False,
                            "repairs_applied": repairs_applied,
                            "safe_finalize_applied": True,
                            "safe_finalize_plan": safe_finalize_plan,
                            "read_only": False,
                            "attempts": attempt,
                            "evidence": history,
                        }
                    break
                if attempt < attempts:
                    sleep(DEFAULT_BACKOFF_SECONDS[min(attempt, len(DEFAULT_BACKOFF_SECONDS) - 1)])
                continue
            if allow_repairs and (
                failed_stage in SAFE_RETRY_STAGES
                or evidence.unit != "active"
                or evidence.main_pid <= 0
            ):
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
            sleep(DEFAULT_BACKOFF_SECONDS[min(attempt, len(DEFAULT_BACKOFF_SECONDS) - 1)])

    return {
        "status": "halted",
        "transport": "transport-indeterminate",
        "pr": pr,
        "head": exact_head,
        "merge": exact_merge,
        "expected_sha": expected,
        "target_id": target.target_id,
        "service_name": target.service_name,
        "healthy": False,
        "repairs_applied": repairs_applied,
        "safe_finalize_applied": safe_finalize_applied,
        "safe_finalize_plan": safe_finalize_plan,
        "read_only": not allow_repairs and not allow_safe_finalize,
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
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="disable daemon-reload, restart and probe repair operations",
    )
    parser.add_argument(
        "--safe-finalize-incomplete",
        action="store_true",
        help=(
            "allow one exact-SHA immutable-CAS completion-marker finalize; "
            "never repeats sync, dependencies or restart"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.read_only and args.safe_finalize_incomplete:
        parser.error("--read-only cannot be combined with --safe-finalize-incomplete")
    payload = reconcile(
        target_file=args.target_file,
        expected_sha=args.expected_sha,
        pr=args.pr,
        head=args.head,
        merge=args.merge,
        failed_stage=args.failed_stage,
        attempts=args.attempts,
        allow_repairs=not args.read_only,
        allow_safe_finalize=bool(args.safe_finalize_incomplete),
    )
    rendered = json.dumps(payload, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["healthy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Preview/read exact fixed schedule target, apply or recover the same operation."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_maintenance import RuntimeScheduleClient, SystemdClient, _read_env_file, _build_web_auth_cookie
from packages.application import business_data_schedule_profile as profile
from packages.application import business_data_schedule_transition as transition
from packages.application import business_data_maintenance_pause as pause
from packages.application.business_data_write_barrier import barrier_status


def _prove_deployed_sha(expected: str) -> None:
    metadata = ROOT / ".wb-core-deploy.json"
    marker = ROOT / ".wb-core-runtime-sha"
    for path in (metadata, marker):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise RuntimeError("deployed revision metadata is unproven")
    value = json.loads(metadata.read_text())
    if (value.get("schema_version") != "wb_core_deploy_metadata_v2"
            or value.get("deployment_complete") is not True or value.get("commit") != expected
            or marker.read_text().strip() != expected):
        raise RuntimeError("exact completed deployed revision differs")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "status", "apply", "rollback"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--window-id", default="")
    parser.add_argument("--deployed-sha", default="")
    parser.add_argument("--reviewed-plan", type=Path)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="repo_owned_schedule_cli")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    runtime = args.runtime_dir.resolve()
    try:
        env = _read_env_file(args.env_file)
        client = RuntimeScheduleClient(base_url=args.base_url, cookie=_build_web_auth_cookie(env))
        options = {"systemd": SystemdClient(), "activity_reader": lambda: client._request(pause.ACTIVITY_PATH)}
        if args.action == "status":
            result = {"transition": profile.load_transition(runtime, args.operation_id),
                      "barrier": barrier_status(runtime), "current": pause.readback(runtime, **options),
                      "activation_readiness": profile.activation_readiness(runtime)}
        elif args.action == "preview":
            _prove_deployed_sha(args.deployed_sha)
            result = transition.preview(runtime, operation_id=args.operation_id, window_id=args.window_id,
                                        deployed_sha=args.deployed_sha, **options)
        elif args.action == "apply":
            _prove_deployed_sha(args.deployed_sha)
            plan = profile.read_private(args.reviewed_plan)
            if plan["operation_id"] != args.operation_id or plan["window_id"] != args.window_id:
                raise RuntimeError("CLI operation/window differs from reviewed plan")
            result = transition.apply(runtime, reviewed_plan=plan, expected_fingerprint=args.expected_fingerprint,
                                      deployed_sha=args.deployed_sha, actor=args.actor, reason=args.reason, **options)
        else:
            result = transition.rollback(runtime, operation_id=args.operation_id, actor=args.actor,
                                         reason=args.reason, **options)
    except Exception as exc:
        print(json.dumps({"status": "schedule_transition_incomplete", "error": type(exc).__name__ + ": " + str(exc),
                          "barrier": barrier_status(runtime)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

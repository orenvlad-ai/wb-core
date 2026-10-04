#!/usr/bin/env python3
"""Explicit indefinite pause/status/exact resume; never changes owner intent."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_maintenance import RuntimeScheduleClient, SystemdClient, _read_env_file, _build_web_auth_cookie
from packages.application import business_data_maintenance_pause as maintenance
from packages.application.business_data_write_barrier import barrier_status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "pause", "status", "resume"))
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--window-id", default="")
    parser.add_argument("--actor", default="repo_owned_maintenance_cli")
    parser.add_argument("--reason", default="")
    parser.add_argument("--wait-timeout-seconds", type=float, default=1200)
    args = parser.parse_args(argv)
    env = _read_env_file(args.env_file)
    base = args.base_url or env.get("BUSINESS_DATA_MAINTENANCE_BASE_URL") or (
        f"http://{env.get('REGISTRY_UPLOAD_HTTP_HOST', '127.0.0.1')}:{env.get('REGISTRY_UPLOAD_HTTP_PORT', '8765')}"
    )
    client = RuntimeScheduleClient(base_url=base, cookie=_build_web_auth_cookie(env))
    options = {"systemd": SystemdClient(), "activity_reader": lambda: client._request(maintenance.ACTIVITY_PATH)}
    runtime = args.runtime_dir.resolve()
    try:
        if args.action == "preflight":
            result = maintenance.preflight(runtime, **options)
        elif args.action == "status":
            result = {"state": maintenance.load_state(runtime), "barrier": barrier_status(runtime),
                      "current": maintenance.readback(runtime, **options)}
        else:
            mutate = maintenance.pause if args.action == "pause" else maintenance.resume
            identity = {"window_id": args.window_id, "actor": args.actor, "reason": args.reason}
            if args.action == "pause":
                identity["wait_timeout_seconds"] = args.wait_timeout_seconds
            result = mutate(runtime, **options, **identity)
    except Exception as exc:
        result = {"status": "maintenance_incomplete", "error": type(exc).__name__ + ": " + str(exc),
                  "barrier": barrier_status(runtime)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

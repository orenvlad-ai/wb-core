#!/usr/bin/env python3
"""Explicit preview/status/exact-target resume after canonical formula-pin deploy."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_maintenance import SystemdClient, RuntimeScheduleClient, _read_env_file, _build_web_auth_cookie
from packages.application import business_data_formula_resume as formula
from packages.application import business_data_schedule_profile as schedule
from packages.application import business_data_maintenance_pause as pause
from packages.application.business_data_write_barrier import barrier_status


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('preview', 'status', 'apply'))
    p.add_argument('--runtime-dir', required=True, type=Path)
    p.add_argument('--app-dir', required=True, type=Path)
    p.add_argument('--env-file', required=True, type=Path)
    p.add_argument('--base-url', default='')
    p.add_argument('--operation-id', required=True)
    p.add_argument('--window-id', default='')
    p.add_argument('--expected-sha', default='')
    p.add_argument('--reviewed-plan', type=Path)
    p.add_argument('--expected-fingerprint', default='')
    p.add_argument('--actor', default='repo_owned_formula_resume_cli')
    p.add_argument('--reason', default='')
    args = p.parse_args(argv)
    try:
        env = _read_env_file(args.env_file)
        client = RuntimeScheduleClient(base_url=args.base_url or env.get('BUSINESS_DATA_MAINTENANCE_BASE_URL') or
            f"http://{env.get('REGISTRY_UPLOAD_HTTP_HOST', '127.0.0.1')}:{env.get('REGISTRY_UPLOAD_HTTP_PORT', '8765')}",
            cookie=_build_web_auth_cookie(env))
        options = dict(systemd=SystemdClient(), activity_reader=lambda: client._request(pause.ACTIVITY_PATH))
        if args.action == 'preview':
            result = formula.preview(args.runtime_dir, operation_id=args.operation_id, window_id=args.window_id,
                expected_sha=args.expected_sha, app_dir=args.app_dir, **options)
        elif args.action == 'status':
            result = dict(transition=formula.load(args.runtime_dir, args.operation_id),
                barrier=barrier_status(args.runtime_dir), current=pause.readback(args.runtime_dir, **options))
        else:
            if not args.reviewed_plan:
                raise RuntimeError('private reviewed plan is required')
            plan = schedule.read_private(args.reviewed_plan)
            if (plan['operation_id'] != args.operation_id or plan['window_id'] != args.window_id
                    or plan['expected_sha'] != args.expected_sha or plan['app_dir'] != str(args.app_dir.absolute())):
                raise RuntimeError('CLI exact deployment/operation/window differs from plan')
            result = formula.apply(args.runtime_dir, reviewed_plan=plan, expected_fingerprint=args.expected_fingerprint,
                actor=args.actor, reason=args.reason, **options)
    except Exception as exc:
        # Transport exceptions may carry response bodies. Never emit credential,
        # provider body, guessed permission or an indeterminate success receipt.
        print(json.dumps(dict(status='formula_resume_incomplete', error_code=type(exc).__name__,
            block_reason=str(exc)[:240] if str(exc).startswith('formula resume ') else 'readback/transport unproven',
            pause_released=None, next_action='exact same-operation status/readback')))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Focused offline checks for the hosted-runtime deploy boundary."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import registry_upload_http_entrypoint_hosted_runtime as hosted  # noqa: E402


def main() -> int:
    target = hosted.load_hosted_runtime_target(hosted.DEFAULT_TARGET_FILE)
    assert target.target_status == hosted.ACTIVE_TARGET_STATUS
    assert target.target_role == hosted.PRIMARY_LIVE_TARGET_ROLE
    assert target.target_lifecycle == hosted.CURRENT_LIVE_TARGET_LIFECYCLE
    assert target.ssh_destination == hosted.ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION
    assert target.target_dir == hosted.ACTIVE_HOSTED_RUNTIME_TARGET_DIR
    assert not hasattr(target, "recovery_scratch_filesystem")

    plan = hosted.build_deploy_plan(target)
    assert plan["missing_for_deploy"] == []
    assert plan["target_action_blockers"] == []
    assert plan["target_mutation_guard"]["mutating_actions_blocked_by_default"] is False
    assert plan["deploy_sequence"][-2:] == [
        "probe loopback/runtime contour",
        "probe public contour",
    ]

    routes = hosted.load_public_route_manifest(hosted.DEFAULT_PUBLIC_ROUTE_ALLOWLIST_FILE)
    max_route = next(route for route in routes["routes"] if route["name"] == "max_webhook")
    assert max_route == {
        "name": "max_webhook", "match": "exact", "path": "/v1/max/webhook",
        "methods": ["POST"], "proxy_pass_url": "http://127.0.0.1:8783",
    }
    rendered = hosted.render_nginx_public_route_block(routes, proxy_pass_url="http://127.0.0.1:8765")
    max_block = rendered.split("location = /v1/max/webhook {", 1)[1].split("\n    }", 1)[0]
    for directive in (
        "proxy_pass http://127.0.0.1:8783;", "client_max_body_size 1m;",
        "proxy_read_timeout 25s;", "proxy_send_timeout 10s;", "client_body_timeout 10s;",
        "proxy_request_buffering on;", "access_log off;",
        "if ($request_method != POST) { return 405; }",
    ):
        assert directive in max_block
    assert "error_log /dev/null" not in max_block
    assert "client_max_body_size 32m;" in rendered

    parser = hosted.build_arg_parser()
    assert parser.parse_args(["print-plan"]).command == "print-plan"
    assert parser.parse_args(["deploy", "--dry-run"]).dry_run is True
    assert parser.parse_args(["root-storage-status"]).command == "root-storage-status"
    sanitation = parser.parse_args(
        [
            "storage-recovery-sanitation-plan",
            "--deployed-sha",
            "a" * 40,
            "--root",
            "backup",
            "--family",
            "calculation-parameters",
        ]
    )
    assert sanitation.storage_sanitation_action == "plan"

    source = Path(hosted.__file__).read_text(encoding="utf-8")
    for retired in (
        "abort-prepared",
        "recovery_scratch",
        "storage_recovery_sanitation_job",
        "hot_journal",
    ):
        assert retired not in source

    print("registry_upload_http_entrypoint_hosted_runtime_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

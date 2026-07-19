"""Regression checks for fail-closed hosted WebCore auth env preflight."""

from dataclasses import replace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.registry_upload_http_entrypoint_hosted_runtime import (
    HostedRuntimeTarget,
    _build_auth_env_preflight_command,
    _validate_production_target_identity,
    load_hosted_runtime_target,
)


def main() -> None:
    europe = load_hosted_runtime_target(Path("artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json"))
    _validate_production_target_identity(europe, action="smoke")
    for field, value in (
        ("environment_file", "__SET_ME__"),
        ("environment_file", "${ENV_FILE_TEMPLATE}"),
        ("target_status", "inactive"),
        ("target_role", "legacy"),
        ("ssh_destination", "wrong-alias"),
        ("target_id", "wrong-id"),
    ):
        try:
            _validate_production_target_identity(replace(europe, **{field: value}), action="smoke")
        except ValueError as exc:
            assert "secret-value" not in str(exc)
        else:
            raise AssertionError(f"identity mutation {field}={value!r} must fail closed")
    archived = load_hosted_runtime_target(Path("artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__selleros_api.json"))
    try:
        _validate_production_target_identity(archived, action="smoke")
    except ValueError:
        pass
    else:
        raise AssertionError("archived selleros target must fail closed")
    target = HostedRuntimeTarget(
        target_status="active",
        target_id="smoke",
        public_base_url="https://example.invalid",
        loopback_base_url="http://127.0.0.1:8765",
        ssh_destination="smoke-host",
        target_dir="/opt/app",
        service_name="service",
        restart_command="systemctl restart service",
        status_command="systemctl status service",
        environment_file="/opt/wb-ai/.env",
    )
    command = _build_auth_env_preflight_command(target)
    text = " ".join(command)
    for key in (
        "WB_CORE_WEB_AUTH_USERNAME",
        "WB_CORE_WEB_AUTH_PASSWORD_HASH",
        "WB_CORE_WEB_AUTH_SESSION_SECRET",
    ):
        assert key in text
    assert "missing required auth variable" in text
    assert "secret-value" not in text
    print("hosted_runtime_auth_preflight_smoke: ok")


if __name__ == "__main__":
    main()

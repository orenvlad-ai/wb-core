"""Regression checks for fail-closed hosted WebCore auth env preflight."""

from apps.registry_upload_http_entrypoint_hosted_runtime import (
    HostedRuntimeTarget,
    _build_auth_env_preflight_command,
)


def main() -> None:
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

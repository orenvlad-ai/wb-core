"""Smoke checks for exact identity/argument guards of reconciliation command."""
from pathlib import Path
from apps.hosted_runtime_transport_reconcile import reconcile


def main() -> None:
    try:
        reconcile(target_file=Path("artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__selleros_api.json"), expected_sha="0"*40, pr=1, head="1"*40, merge="2"*40)
    except ValueError:
        print("hosted_runtime_transport_reconcile_smoke: ok")
        return
    raise AssertionError("non-canonical target must fail before SSH")


if __name__ == "__main__":
    main()

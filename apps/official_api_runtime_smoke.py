"""Общий smoke-check для official-API execution path."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.official_api_runtime import (
    OfficialApiRuntimeError,
    assert_upstream_reachable,
    load_runtime_config,
)


def main() -> None:
    runtime = load_runtime_config(
        token_env_var="WB_TOKEN",
        default_base_url="https://discounts-prices-api.wildberries.ru",
        base_url_env_var="WB_OFFICIAL_API_BASE_URL",
    )
    print("env: ok")
    print(f"base_url: {runtime.base_url}")
    print(f"timeout_seconds: {runtime.timeout_seconds}")

    try:
        assert_upstream_reachable(
            base_url=runtime.base_url,
            timeout_seconds=runtime.timeout_seconds,
        )
    except OfficialApiRuntimeError as exc:
        raise SystemExit(f"reachability: failed -> {exc}") from exc

    print("reachability: ok")
    print("official-api-runtime-smoke passed")


if __name__ == "__main__":
    main()

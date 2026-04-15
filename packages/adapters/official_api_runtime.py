"""Минимальный shared runtime helper для official-API family."""

import socket
from dataclasses import dataclass
import os
from typing import Optional, Tuple


DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_WB_API_TOKEN_ENV = "WB_API_TOKEN"


class OfficialApiRuntimeError(RuntimeError):
    """Ошибка общей runtime boundary official-API family."""


@dataclass(frozen=True)
class OfficialApiRuntimeConfig:
    """Минимальная runtime-конфигурация для official-API adapter."""

    token: str
    base_url: str
    timeout_seconds: float


def load_runtime_config(
    *,
    token_env_var: str,
    default_base_url: str,
    base_url_env_var: Optional[str] = None,
    timeout_env_var: str = "OFFICIAL_API_TIMEOUT_SECONDS",
    default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> OfficialApiRuntimeConfig:
    """Собирает runtime config из env и валидирует обязательные поля."""

    token = _require_env(token_env_var)
    base_url = _read_base_url(default_base_url=default_base_url, base_url_env_var=base_url_env_var)
    timeout_seconds = _read_timeout(
        timeout_env_var=timeout_env_var,
        default_timeout_seconds=default_timeout_seconds,
    )
    return OfficialApiRuntimeConfig(
        token=token,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def assert_upstream_reachable(*, base_url: str, timeout_seconds: float) -> None:
    """Проверяет, что host upstream достижим хотя бы на TCP-уровне."""

    host, port = _parse_host_and_port(base_url)
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return
    except OSError as exc:
        raise OfficialApiRuntimeError(
            f"official API upstream is not reachable for {host}:{port}: {exc}"
        ) from exc


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise OfficialApiRuntimeError(f"required env {name} is not set")
    return value


def _read_base_url(*, default_base_url: str, base_url_env_var: Optional[str]) -> str:
    if base_url_env_var:
        override = os.environ.get(base_url_env_var, "").strip()
        if override:
            return override.rstrip("/")
    return default_base_url.rstrip("/")


def _read_timeout(*, timeout_env_var: str, default_timeout_seconds: float) -> float:
    raw_value = os.environ.get(timeout_env_var, "").strip()
    if not raw_value:
        return default_timeout_seconds

    try:
        timeout_seconds = float(raw_value)
    except ValueError as exc:
        raise OfficialApiRuntimeError(
            f"{timeout_env_var} must be numeric seconds, got {raw_value!r}"
        ) from exc

    if timeout_seconds <= 0:
        raise OfficialApiRuntimeError(f"{timeout_env_var} must be > 0")
    return timeout_seconds


def _parse_host_and_port(base_url: str) -> Tuple[str, int]:
    normalized = base_url.strip()
    if "://" in normalized:
        _, remainder = normalized.split("://", 1)
    else:
        remainder = normalized
    host_port = remainder.split("/", 1)[0]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
        return host, int(port)
    return host_port, 443

"""Контракты HTTP entrypoint для registry upload."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RegistryUploadHttpEntrypointConfig:
    host: str
    port: int
    upload_path: str
    runtime_dir: Path

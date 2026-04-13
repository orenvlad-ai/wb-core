"""Application-слой HTTP entrypoint для registry upload."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult


class RegistryUploadHttpEntrypoint:
    """Тонкий entrypoint, делегирующий ingest в DB-backed runtime."""

    def __init__(
        self,
        runtime_dir: Path,
        runtime: RegistryUploadDbBackedRuntime | None = None,
        activated_at_factory: Callable[[], str] | None = None,
    ) -> None:
        self.runtime = runtime or RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        self.activated_at_factory = activated_at_factory or _default_activated_at_factory

    def handle_bundle_payload(self, payload: Mapping[str, Any]) -> RegistryUploadResult:
        return self.runtime.ingest_bundle(
            payload,
            activated_at=self.activated_at_factory(),
        )


def _default_activated_at_factory() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

"""Адаптерная граница блока web-source snapshot."""

from typing import Any, Mapping, Protocol

from packages.contracts.web_source_snapshot_block import WebSourceSnapshotRequest


class WebSourceSnapshotSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: WebSourceSnapshotRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

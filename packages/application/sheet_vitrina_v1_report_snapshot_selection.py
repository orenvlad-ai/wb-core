"""Ready-snapshot selection helpers for sheet_vitrina_v1 reports."""

from __future__ import annotations

from dataclasses import dataclass

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime


@dataclass(frozen=True)
class ReadySnapshotSelection:
    requested_as_of_date: str
    selected_as_of_dates: tuple[str, ...]
    available_as_of_dates: tuple[str, ...]

    @property
    def latest_as_of_date(self) -> str | None:
        return self.selected_as_of_dates[0] if self.selected_as_of_dates else None


def select_latest_ready_snapshot_dates(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    requested_as_of_date: str,
    limit: int,
) -> ReadySnapshotSelection:
    """Return the latest persisted ready snapshot dates not newer than requested_as_of_date."""

    if limit <= 0:
        raise ValueError("ready snapshot selection limit must be positive")
    available = tuple(
        runtime.list_sheet_vitrina_ready_snapshot_dates(
            date_to=requested_as_of_date,
            descending=True,
        )
    )
    return ReadySnapshotSelection(
        requested_as_of_date=requested_as_of_date,
        selected_as_of_dates=available[:limit],
        available_as_of_dates=available,
    )

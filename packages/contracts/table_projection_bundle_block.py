"""Контракты блока table projection bundle."""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union


@dataclass(frozen=True)
class TableProjectionBundleRequest:
    """Минимальный входной контракт projection bundle блока."""

    bundle_type: str
    scenario: Literal["normal", "minimal"] = "normal"


@dataclass(frozen=True)
class TableProjectionSourceStatus:
    """Статус одного upstream source внутри projection bundle."""

    source_key: str
    kind: str
    freshness: Optional[str]
    requested_count: int
    covered_count: int
    missing_nm_ids: list[int] = field(default_factory=list)
    snapshot_date: Optional[str] = None
    date: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    extra: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class TableProjectionBundleItem:
    """Одна table-facing projection row на уровне SKU."""

    nm_id: int
    display_name: str
    group: str
    enabled: bool
    display_order: int
    web_source: dict[str, dict[str, Any]]
    official_api: dict[str, dict[str, Any]]
    history_summary: dict[str, Any]


@dataclass(frozen=True)
class TableProjectionBundleSuccess:
    kind: Literal["success"]
    as_of_date: Optional[str]
    count: int
    items: list[TableProjectionBundleItem]
    source_statuses: list[TableProjectionSourceStatus]


@dataclass(frozen=True)
class TableProjectionBundleEmpty:
    kind: Literal["empty"]
    as_of_date: Optional[str]
    count: int
    items: list[TableProjectionBundleItem]
    source_statuses: list[TableProjectionSourceStatus]
    detail: str


TableProjectionBundleResult = Union[TableProjectionBundleSuccess, TableProjectionBundleEmpty]


@dataclass(frozen=True)
class TableProjectionBundleEnvelope:
    result: TableProjectionBundleResult

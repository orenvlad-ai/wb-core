"""Контракты wide data matrix v1 fixture."""

from dataclasses import dataclass
from typing import Literal, Optional, Union


@dataclass(frozen=True)
class WideDataMatrixV1Request:
    bundle_type: str
    scenario: Literal["normal", "minimal"] = "normal"


@dataclass(frozen=True)
class WideDataMatrixColumn:
    column: str
    field: str
    label: str


@dataclass(frozen=True)
class WideDataMatrixBlock:
    block: Literal["TOTAL", "GROUP", "SKU"]
    row_count: int


@dataclass(frozen=True)
class WideDataMatrixRow:
    block: Literal["TOTAL", "GROUP", "SKU"]
    label: str
    key: str
    metric_key: str
    values: dict[str, Optional[float]]


@dataclass(frozen=True)
class WideDataMatrixV1Success:
    kind: Literal["success"]
    columns: list[WideDataMatrixColumn]
    dates: list[str]
    blocks: list[WideDataMatrixBlock]
    rows: list[WideDataMatrixRow]


@dataclass(frozen=True)
class WideDataMatrixV1Empty:
    kind: Literal["empty"]
    columns: list[WideDataMatrixColumn]
    dates: list[str]
    blocks: list[WideDataMatrixBlock]
    rows: list[WideDataMatrixRow]
    detail: str


WideDataMatrixV1Result = Union[WideDataMatrixV1Success, WideDataMatrixV1Empty]


@dataclass(frozen=True)
class WideDataMatrixV1Envelope:
    result: WideDataMatrixV1Result

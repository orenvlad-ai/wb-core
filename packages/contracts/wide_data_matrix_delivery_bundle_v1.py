"""Контракты delivery bundle wide data matrix v1."""

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class WideDataMatrixDeliveryBundleV1Request:
    bundle_type: str
    scenario: Literal["normal", "minimal"] = "normal"


@dataclass(frozen=True)
class WideDataMatrixDeliverySheet:
    sheet_name: str
    header: list[str]
    rows: list[list[Any]]


@dataclass(frozen=True)
class WideDataMatrixDeliveryBundleV1Envelope:
    delivery_contract_version: str
    snapshot_id: str
    as_of_date: str
    data_vitrina: WideDataMatrixDeliverySheet
    status: WideDataMatrixDeliverySheet

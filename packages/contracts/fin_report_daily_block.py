"""Контракты блока fin report daily."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FinReportDailyRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    snapshot_date: str
    nm_ids: list[int]
    scenario: Literal["normal", "storage_total"] = "normal"


@dataclass(frozen=True)
class FinReportDailyItem:
    """Элемент дневного финансового snapshot на уровне nmId."""

    nm_id: int
    fin_delivery_rub: float
    fin_storage_fee: float
    fin_deduction: float
    fin_commission: float
    fin_penalty: float
    fin_additional_payment: float
    fin_buyout_rub: float
    fin_commission_wb_portal: float
    fin_acquiring_fee: float
    fin_loyalty_rub: float


@dataclass(frozen=True)
class FinReportDailyStorageTotal:
    """Special total row для storage fee."""

    nm_id: Literal[0]
    fin_storage_fee_total: float


@dataclass(frozen=True)
class FinReportDailySuccess:
    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[FinReportDailyItem]
    storage_total: FinReportDailyStorageTotal


@dataclass(frozen=True)
class FinReportDailyEnvelope:
    result: FinReportDailySuccess

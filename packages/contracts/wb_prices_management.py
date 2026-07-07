"""Contracts for the WB prices management operator block."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


MAX_PRICE_CHANGES_PER_UPLOAD = 1000
PRICE_UPLOAD_STATUS_LABELS = {
    1: "processing",
    3: "success",
    4: "canceled",
    5: "partial_error",
    6: "all_error",
}
PRICE_UPLOAD_FINAL_STATUSES = {3, 4, 5, 6}


@dataclass(frozen=True)
class WbPriceSize:
    size_id: int | None
    tech_size_name: str
    price: float | None
    discounted_price: float | None
    club_discounted_price: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sizeID": self.size_id,
            "techSizeName": self.tech_size_name,
            "price": self.price,
            "discountedPrice": self.discounted_price,
            "clubDiscountedPrice": self.club_discounted_price,
        }


@dataclass(frozen=True)
class WbPriceGood:
    nm_id: int
    vendor_code: str
    sizes: list[WbPriceSize]
    price: float | None
    discounted_price: float | None
    club_discounted_price: float | None
    discount: int | None
    club_discount: int | None
    currency_iso_code_4217: str
    editable_size_price: bool
    wholesale_discount_threshold: list[Mapping[str, Any]]
    is_bad_turnover: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "nmID": self.nm_id,
            "vendorCode": self.vendor_code,
            "sizes": [size.to_dict() for size in self.sizes],
            "price": self.price,
            "discountedPrice": self.discounted_price,
            "clubDiscountedPrice": self.club_discounted_price,
            "discount": self.discount,
            "clubDiscount": self.club_discount,
            "currencyIsoCode4217": self.currency_iso_code_4217,
            "editableSizePrice": self.editable_size_price,
            "wholesaleDiscountThreshold": [dict(item) for item in self.wholesale_discount_threshold],
            "isBadTurnover": self.is_bad_turnover,
        }


@dataclass(frozen=True)
class WbPriceChange:
    nm_id: int
    price: int | None = None
    discount: int | None = None

    def to_upload_dict(self) -> dict[str, int]:
        payload = {"nmID": self.nm_id}
        if self.price is not None:
            payload["price"] = self.price
        if self.discount is not None:
            payload["discount"] = self.discount
        return payload


PriceUploadStatus = Literal["processing", "success", "canceled", "partial_error", "all_error", "unknown"]

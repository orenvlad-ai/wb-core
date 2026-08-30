"""One fail-closed application seam for existing internal WB writers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from typing import Any, Mapping, Sequence

from packages.application.change_registry import (
    ChangeRegistryRepository,
    canonical_digest,
    canonical_json,
    target_identity,
)


PRICE_FIELDS = (
    "original_price_minor",
    "discount_bps",
    "seller_price_minor",
)
SUPPORTED_ACCOUNT_SCOPE = "seller-portal-primary"


class InternalWriterRegistryError(RuntimeError):
    """Registry preparation or lifecycle persistence failed closed."""


@dataclass(frozen=True)
class PreparedWriterOperation:
    operation_id: str
    change_item_ids: Mapping[str, str]
    source_surface: str
    native_operation_id: str


class InternalWriterRegistry:
    """Records exact writer intent and proof without owning any WB client."""

    def __init__(
        self,
        *,
        runtime_dir: Any,
        seller_id: str,
        account_scope: str,
        timestamp_factory: Any | None = None,
        repository: ChangeRegistryRepository | None = None,
    ) -> None:
        self.seller_id = _identity(seller_id, "seller_id")
        self.account_scope = _identity(account_scope, "account_scope")
        if self.account_scope != SUPPORTED_ACCOUNT_SCOPE:
            raise InternalWriterRegistryError(
                "canonical single-seller account scope is invalid"
            )
        self.repository = repository or ChangeRegistryRepository(runtime_dir)
        self.timestamp_factory = timestamp_factory or (
            lambda: datetime.now(timezone.utc).isoformat()
        )

    def prepare_price(
        self,
        *,
        source_surface: str,
        actor: str,
        native_operation_id: str,
        nm_id: int,
        before: Mapping[str, Any],
        requested: Mapping[str, Any],
        explicit_fields: Sequence[str],
        requested_at: str,
        correlation_id: str = "",
        calculation_id: str = "",
        apply_operation_id: str = "",
        recommendation_item_id: str = "",
        native_audit_reference: str = "",
        stage: str = "",
    ) -> PreparedWriterOperation:
        exact_before = _price_tuple(before)
        exact_requested = _price_tuple(requested)
        return self.prepare_prices(
            source_surface=source_surface,
            actor=actor,
            native_operation_id=native_operation_id,
            changes=(
                {
                    "nm_id": _positive_int(nm_id, "nm_id"),
                    "before": exact_before,
                    "requested": exact_requested,
                    "explicit_fields": tuple(explicit_fields),
                },
            ),
            requested_at=requested_at,
            correlation_id=correlation_id,
            calculation_id=calculation_id,
            apply_operation_id=apply_operation_id,
            recommendation_item_id=recommendation_item_id,
            native_audit_reference=native_audit_reference,
            stage=stage,
        )

    def prepare_prices(
        self,
        *,
        source_surface: str,
        actor: str,
        native_operation_id: str,
        changes: Sequence[Mapping[str, Any]],
        requested_at: str,
        correlation_id: str = "",
        calculation_id: str = "",
        apply_operation_id: str = "",
        recommendation_item_id: str = "",
        native_audit_reference: str = "",
        stage: str = "",
    ) -> PreparedWriterOperation:
        atomic_values: list[tuple[Any, str, int, int, str]] = []
        explicit_by_nm: dict[str, list[str]] = {}
        for change in changes:
            nm_id = _positive_int(change.get("nm_id"), "nm_id")
            before = _price_tuple(change.get("before") or {})
            requested = _price_tuple(change.get("requested") or {})
            explicit = sorted(
                {str(item) for item in (change.get("explicit_fields") or ())}
            )
            explicit_by_nm[str(nm_id)] = explicit
            target = target_identity("price", nm_id=nm_id)
            atomic_values.extend(
                (
                    target,
                    field,
                    before[field],
                    requested[field],
                    recommendation_item_id,
                )
                for field in PRICE_FIELDS
            )
        return self._prepare(
            source_surface=source_surface,
            actor=actor,
            native_operation_id=native_operation_id,
            requested_at=requested_at,
            atomic_values=atomic_values,
            explicit_fields=tuple(
                f"{nm_id}:{field}"
                for nm_id, fields in sorted(explicit_by_nm.items())
                for field in fields
            ),
            correlation_id=correlation_id,
            calculation_id=calculation_id,
            apply_operation_id=apply_operation_id,
            native_audit_reference=native_audit_reference,
            stage=stage,
        )

    def prepare_bid(
        self,
        *,
        source_surface: str,
        actor: str,
        native_operation_id: str,
        nm_id: int,
        advert_id: int,
        placement: str,
        before_bid_minor: int,
        requested_bid_minor: int,
        requested_at: str,
        correlation_id: str = "",
        calculation_id: str = "",
        apply_operation_id: str = "",
        recommendation_item_id: str = "",
        native_audit_reference: str = "",
    ) -> PreparedWriterOperation:
        target = target_identity(
            "bid",
            nm_id=_positive_int(nm_id, "nm_id"),
            advert_id=_positive_int(advert_id, "advert_id"),
            placement=str(placement or "").strip().lower(),
        )
        return self._prepare(
            source_surface=source_surface,
            actor=actor,
            native_operation_id=native_operation_id,
            requested_at=requested_at,
            atomic_values=[
                (
                    target,
                    "bid_minor",
                    _non_negative_int(before_bid_minor, "before_bid_minor"),
                    _non_negative_int(requested_bid_minor, "requested_bid_minor"),
                    recommendation_item_id,
                )
            ],
            explicit_fields=("bid_minor",),
            correlation_id=correlation_id,
            calculation_id=calculation_id,
            apply_operation_id=apply_operation_id,
            native_audit_reference=native_audit_reference,
            stage="",
        )

    def prepare_campaign_state(
        self,
        *,
        source_surface: str,
        actor: str,
        native_operation_id: str,
        nm_id: int,
        advert_id: int,
        before_state: str,
        requested_state: str,
        requested_at: str,
        correlation_id: str = "",
        calculation_id: str = "",
        apply_operation_id: str = "",
        recommendation_item_id: str = "",
        native_audit_reference: str = "",
    ) -> PreparedWriterOperation:
        target = target_identity(
            "campaign",
            nm_id=_positive_int(nm_id, "nm_id"),
            advert_id=_positive_int(advert_id, "advert_id"),
            placement="",
        )
        return self._prepare(
            source_surface=source_surface,
            actor=actor,
            native_operation_id=native_operation_id,
            requested_at=requested_at,
            atomic_values=[
                (
                    target,
                    "campaign_state",
                    _campaign_state(before_state, "before_state"),
                    _campaign_state(requested_state, "requested_state"),
                    recommendation_item_id,
                )
            ],
            explicit_fields=("campaign_state",),
            correlation_id=correlation_id,
            calculation_id=calculation_id,
            apply_operation_id=apply_operation_id,
            native_audit_reference=native_audit_reference,
            stage="",
        )

    def submitted(
        self,
        prepared: PreparedWriterOperation,
        *,
        receipt_reference: str,
        receipt_basis: Mapping[str, Any],
    ) -> None:
        self._state(
            prepared,
            state="submitted",
            receipt_reference=receipt_reference,
            receipt_digest=canonical_digest(receipt_basis),
        )

    def fail_before_submit(
        self,
        prepared: PreparedWriterOperation,
        *,
        rejected: bool,
        error_code: str,
        error_message: str,
    ) -> None:
        self._state(
            prepared,
            state="rejected" if rejected else "failed",
            error_code=error_code,
            error_message=error_message,
        )

    def ambiguous(
        self,
        prepared: PreparedWriterOperation,
        *,
        error_code: str,
        error_message: str,
        receipt_reference: str = "",
    ) -> None:
        self._state(
            prepared,
            state="ambiguous",
            error_code=error_code,
            error_message=error_message,
            receipt_reference=receipt_reference,
        )

    def failed_after_submit(
        self,
        prepared: PreparedWriterOperation,
        *,
        error_code: str,
        error_message: str,
        receipt_reference: str = "",
    ) -> None:
        self._state(
            prepared,
            state="failed",
            error_code=error_code,
            error_message=error_message,
            receipt_reference=receipt_reference,
        )

    def confirm_price(
        self,
        prepared: PreparedWriterOperation,
        *,
        confirmed: Mapping[str, Any],
        readback_basis: Mapping[str, Any],
        receipt_reference: str = "",
        native_audit_references: Sequence[str] = (),
    ) -> None:
        exact = _price_tuple(confirmed)
        nm_ids = {
            key.split(":", 5)[1]
            for key in prepared.change_item_ids
            if key.startswith("price:")
        }
        if len(nm_ids) != 1:
            raise InternalWriterRegistryError(
                "single price confirmation requires exactly one nmID"
            )
        self.confirm_prices(
            prepared,
            confirmed_by_nm={int(next(iter(nm_ids))): exact},
            readback_basis=readback_basis,
            receipt_reference=receipt_reference,
            native_audit_references=native_audit_references,
        )

    def confirm_prices(
        self,
        prepared: PreparedWriterOperation,
        *,
        confirmed_by_nm: Mapping[int, Mapping[str, Any]],
        readback_basis: Mapping[str, Any],
        receipt_reference: str = "",
        native_audit_references: Sequence[str] = (),
    ) -> None:
        exact_by_nm = {
            int(nm_id): _price_tuple(value)
            for nm_id, value in confirmed_by_nm.items()
        }
        self._confirm(
            prepared,
            confirmed_by_item_key={
                key: exact_by_nm[int(key.split(":", 5)[1])][key.rsplit(":", 1)[1]]
                for key in prepared.change_item_ids
            },
            readback_basis=readback_basis,
            receipt_reference=receipt_reference,
            native_audit_references=native_audit_references,
        )

    def confirm_bid(
        self,
        prepared: PreparedWriterOperation,
        *,
        confirmed_bid_minor: int,
        readback_basis: Mapping[str, Any],
        receipt_reference: str = "",
        native_audit_references: Sequence[str] = (),
    ) -> None:
        self._confirm(
            prepared,
            confirmed_by_item_key={
                key: _non_negative_int(
                    confirmed_bid_minor, "confirmed_bid_minor"
                )
                for key in prepared.change_item_ids
            },
            readback_basis=readback_basis,
            receipt_reference=receipt_reference,
            native_audit_references=native_audit_references,
        )

    def confirm_campaign_state(
        self,
        prepared: PreparedWriterOperation,
        *,
        confirmed_state: str,
        readback_basis: Mapping[str, Any],
        receipt_reference: str = "",
        native_audit_references: Sequence[str] = (),
    ) -> None:
        exact_state = _campaign_state(confirmed_state, "confirmed_state")
        self._confirm(
            prepared,
            confirmed_by_item_key={
                key: exact_state for key in prepared.change_item_ids
            },
            readback_basis=readback_basis,
            receipt_reference=receipt_reference,
            native_audit_references=native_audit_references,
        )

    def find_by_receipt(
        self, receipt_reference: str
    ) -> PreparedWriterOperation | None:
        try:
            stored = self.repository.find_operation_by_receipt_reference(
                receipt_reference
            )
        except Exception as exc:
            raise InternalWriterRegistryError(str(exc)) from exc
        if stored is None:
            return None
        operation = stored["operation"]
        item_ids = {
            _item_key(item): str(item["change_item_id"])
            for item in stored["items"]
        }
        return PreparedWriterOperation(
            operation_id=str(operation["operation_id"]),
            change_item_ids=item_ids,
            source_surface=str(operation["source_surface"]),
            native_operation_id=str(operation["native_idempotency_key"]),
        )

    def read_by_receipt(self, receipt_reference: str) -> dict[str, Any] | None:
        try:
            return self.repository.find_operation_by_receipt_reference(
                receipt_reference
            )
        except Exception as exc:
            raise InternalWriterRegistryError(str(exc)) from exc

    def _prepare(
        self,
        *,
        source_surface: str,
        actor: str,
        native_operation_id: str,
        requested_at: str,
        atomic_values: Sequence[tuple[Any, str, Any, Any, str]],
        explicit_fields: Sequence[str],
        correlation_id: str,
        calculation_id: str,
        apply_operation_id: str,
        native_audit_reference: str,
        stage: str,
    ) -> PreparedWriterOperation:
        source = _identity(source_surface, "source_surface")
        native_id = _sanitized(native_operation_id, "native_operation_id", 240)
        exact_requested_at = _utc_timestamp(requested_at)
        created_at = _utc_timestamp(self.timestamp_factory())
        targets = [
            {
                "target_kind": target.target_kind,
                "nm_id": target.nm_id,
                "advert_id": target.advert_id,
                "placement": target.placement,
                "parameter_field": field,
                "before": before,
                "requested": requested,
            }
            for target, field, before, requested, _recommendation in atomic_values
        ]
        operation_basis = {
            "seller_id": self.seller_id,
            "account_scope": self.account_scope,
            "source_surface": source,
            "native_operation_id": native_id,
            "targets_digest": canonical_digest(targets),
            "stage": _sanitized(stage, "stage", 120),
        }
        operation_id = _stable_id("crwo", operation_basis)
        item_rows: list[dict[str, Any]] = []
        item_ids: dict[str, str] = {}
        for target, field, before, requested, recommendation in atomic_values:
            item_basis = {
                **operation_basis,
                "target_kind": target.target_kind,
                "nm_id": target.nm_id,
                "advert_id": target.advert_id,
                "placement": target.placement,
                "parameter_field": field,
            }
            item_id = _stable_id("crwi", item_basis)
            item_ids[_item_key(item_basis)] = item_id
            item_rows.append(
                {
                    "change_item_id": item_id,
                    "attempt_id": _stable_id("crwa", item_basis),
                    "attempt_event_id": _stable_id(
                        "crwe", {**item_basis, "state": "created"}
                    ),
                    "target": target,
                    "parameter_field": field,
                    "before_value": before,
                    "requested_value": requested,
                    "recommendation_item_id": recommendation,
                }
            )
        provenance = {
            "explicit_fields": sorted({str(item) for item in explicit_fields}),
            "native_audit_reference": _sanitized(
                native_audit_reference, "native_audit_reference", 320
            ),
            "stage": operation_basis["stage"],
        }
        try:
            prepared_result = self.repository.prepare_writer_operation(
                operation_id=operation_id,
                seller_id=self.seller_id,
                account_scope=self.account_scope,
                source_surface=source,
                actor_principal=_actor(actor),
                actor_kind="human" if str(actor or "").strip() else "system",
                requested_at=exact_requested_at,
                created_at=(
                    exact_requested_at
                    if _timestamp_moment(exact_requested_at)
                    > _timestamp_moment(created_at)
                    else created_at
                ),
                native_idempotency_key=native_id,
                correlation_id=_sanitized(correlation_id, "correlation_id", 240),
                calculation_id=_sanitized(calculation_id, "calculation_id", 240),
                apply_operation_id=_sanitized(
                    apply_operation_id, "apply_operation_id", 240
                ),
                provenance_digest=canonical_digest(
                    {**operation_basis, "values": targets, **provenance}
                ),
                items=item_rows,
                provenance_annotation_id=_stable_id("crwan", operation_basis),
                provenance_comment=canonical_json(provenance),
            )
            if prepared_result.get("created_new") is not True:
                raise InternalWriterRegistryError(
                    "native writer operation already exists; submit retry is blocked"
                )
        except Exception as exc:
            if isinstance(exc, InternalWriterRegistryError):
                raise
            raise InternalWriterRegistryError(
                f"change registry preparation failed: {exc}"
            ) from exc
        return PreparedWriterOperation(
            operation_id=operation_id,
            change_item_ids=item_ids,
            source_surface=source,
            native_operation_id=native_id,
        )

    def _state(
        self,
        prepared: PreparedWriterOperation,
        *,
        state: str,
        receipt_reference: str = "",
        receipt_digest: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        try:
            self.repository.append_writer_operation_state(
                operation_id=prepared.operation_id,
                state=state,
                occurred_at=_utc_timestamp(self.timestamp_factory()),
                receipt_reference=_sanitized(
                    receipt_reference, "receipt_reference", 320
                ),
                receipt_digest=receipt_digest,
                error_code=_sanitized(error_code, "error_code", 120),
                error_message=_sanitized(error_message, "error_message", 800),
            )
        except Exception as exc:
            raise InternalWriterRegistryError(
                f"change registry lifecycle write failed: {exc}"
            ) from exc

    def _confirm(
        self,
        prepared: PreparedWriterOperation,
        *,
        confirmed_by_item_key: Mapping[str, Any],
        readback_basis: Mapping[str, Any],
        receipt_reference: str,
        native_audit_references: Sequence[str],
    ) -> None:
        confirmed_values = {
            item_id: confirmed_by_item_key[key]
            for key, item_id in prepared.change_item_ids.items()
        }
        try:
            self.repository.confirm_writer_operation(
                operation_id=prepared.operation_id,
                confirmed_values=confirmed_values,
                confirmed_at=_utc_timestamp(self.timestamp_factory()),
                readback_digest=canonical_digest(readback_basis),
                receipt_reference=_sanitized(
                    receipt_reference, "receipt_reference", 320
                ),
                native_audit_references=tuple(native_audit_references),
            )
        except Exception as exc:
            raise InternalWriterRegistryError(
                f"change registry confirmation failed: {exc}"
            ) from exc


def price_tuple_from_wb(
    *, price: Any, discount: Any, seller_price: Any
) -> dict[str, int]:
    return {
        "original_price_minor": _money_minor(price),
        "discount_bps": _discount_bps(discount),
        "seller_price_minor": _money_minor(seller_price),
    }


def _price_tuple(value: Mapping[str, Any]) -> dict[str, int]:
    return {
        field: _non_negative_int(value.get(field), field) for field in PRICE_FIELDS
    }


def _item_key(value: Mapping[str, Any]) -> str:
    return ":".join(
        (
            str(value["target_kind"]),
            str(value["nm_id"]),
            str(value["advert_id"]),
            str(value["placement"]),
            str(value["parameter_field"]),
        )
    )


def _money_minor(value: Any) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _discount_bps(value: Any) -> int:
    result = int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if result > 10000:
        raise InternalWriterRegistryError("discount exceeds 100 percent")
    return _non_negative_int(result, "discount_bps")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InternalWriterRegistryError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InternalWriterRegistryError(f"{name} must be a non-negative integer")
    return value


def _campaign_state(value: Any, name: str) -> str:
    state = str(value or "").strip().lower()
    if state not in {"ready", "active", "paused"}:
        raise InternalWriterRegistryError(f"{name} is not an actionable campaign state")
    return state


def _identity(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 120 or "\x00" in text:
        raise InternalWriterRegistryError(f"{name} is invalid")
    return text


def _sanitized(value: Any, name: str, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > maximum or "\x00" in text:
        raise InternalWriterRegistryError(f"{name} is invalid")
    forbidden = ("bearer ", "authorization:", "cookie:", "token=", "secret=")
    if any(marker in text.casefold() for marker in forbidden):
        raise InternalWriterRegistryError(f"{name} contains credential material")
    return text


def _actor(value: Any) -> str:
    return _sanitized(value, "actor", 160) or "system:internal-writer"


def _utc_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise InternalWriterRegistryError("timestamp is invalid") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise InternalWriterRegistryError("timestamp requires timezone")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_moment(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _stable_id(prefix: str, basis: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


__all__ = [
    "InternalWriterRegistry",
    "InternalWriterRegistryError",
    "PreparedWriterOperation",
    "SUPPORTED_ACCOUNT_SCOPE",
    "price_tuple_from_wb",
]

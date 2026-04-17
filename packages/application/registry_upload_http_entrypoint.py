"""Application-слой HTTP entrypoint для registry upload и sheet_vitrina_v1 refresh/read split."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    DAILY_REFRESH_BUSINESS_HOUR,
    DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR,
    DAILY_REFRESH_SYSTEMD_UTC_TIME,
    current_business_date_iso,
    default_business_as_of_date,
    to_business_datetime,
)
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.contracts.cost_price_upload import CostPriceUploadResult
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult


class RegistryUploadHttpEntrypoint:
    """Тонкий entrypoint: ingest/update current truth, heavy refresh и cheap read готового snapshot."""

    def __init__(
        self,
        runtime_dir: Path,
        runtime: RegistryUploadDbBackedRuntime | None = None,
        activated_at_factory: Callable[[], str] | None = None,
        refreshed_at_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime or RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        self.activated_at_factory = activated_at_factory or _default_activated_at_factory
        self.refreshed_at_factory = refreshed_at_factory or _default_activated_at_factory
        self.now_factory = now_factory or _default_now_factory
        self.sheet_plan_block = SheetVitrinaV1LivePlanBlock(runtime=self.runtime)

    def handle_bundle_payload(self, payload: Mapping[str, Any]) -> RegistryUploadResult:
        return self.runtime.ingest_bundle(
            payload,
            activated_at=self.activated_at_factory(),
        )

    def handle_cost_price_payload(self, payload: Mapping[str, Any]) -> CostPriceUploadResult:
        return self.runtime.ingest_cost_price_payload(
            payload,
            activated_at=self.activated_at_factory(),
        )

    def handle_sheet_plan_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return asdict(self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date))

    def handle_sheet_status_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        payload = asdict(self.runtime.load_sheet_vitrina_refresh_status(as_of_date=as_of_date))
        payload["server_context"] = self.build_sheet_server_context()
        return payload

    def handle_sheet_refresh_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        current_state = self.runtime.load_current_state()
        plan = self.sheet_plan_block.build_plan(as_of_date=as_of_date)
        refresh_result = self.runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=self.refreshed_at_factory(),
            plan=plan,
        )
        payload = asdict(refresh_result)
        payload["server_context"] = self.build_sheet_server_context()
        return payload

    def build_sheet_server_context(self) -> dict[str, str]:
        now = self.now_factory()
        business_now = to_business_datetime(now).replace(microsecond=0).isoformat()
        return {
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "business_now": business_now,
            "default_as_of_date": default_business_as_of_date(now),
            "today_current_date": current_business_date_iso(now),
            "daily_refresh_business_time": f"{DAILY_REFRESH_BUSINESS_HOUR:02d}:00 {CANONICAL_BUSINESS_TIMEZONE_NAME}",
            "daily_refresh_systemd_time": DAILY_REFRESH_SYSTEMD_UTC_TIME,
            "daily_refresh_systemd_oncalendar": DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR,
        }


def _default_activated_at_factory() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_now_factory() -> datetime:
    return datetime.now(timezone.utc)

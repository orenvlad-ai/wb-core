"""Адаптерная граница блока fin report daily."""

import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.adapters.wb_finance_api import (
    FinanceApiError,
    WbFinanceApiClient,
)
from packages.contracts.fin_report_daily_block import FinReportDailyRequest
from packages.domain.finance_daily_report import (
    FinanceReportBasis, project_finance_daily_report, validate_finance_daily_request,
)
from packages.contracts.source_attempt_diagnostics import (
    SourceAttemptError, new_attempt, unknown_diagnostics,
)


FINANCE_BASE_URL = "https://finance-api.wildberries.ru"


class FinReportDailySource(Protocol):
    def fetch(self, request: FinReportDailyRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedFinReportDailySource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: FinReportDailyRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "storage_total":
            return self._artifacts_root / "legacy" / "storage_total__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedFinReportDailySource:
    def __init__(
        self,
        base_url: str = FINANCE_BASE_URL,
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_FINANCE_API_BASE_URL",
        timeout_seconds: float = 180.0,
        runtime_dir: Path | None = None,
        client: WbFinanceApiClient | None = None,
        report_basis: FinanceReportBasis | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._runtime_dir = runtime_dir
        self._client = client
        self._report_basis = report_basis

    def fetch(self, request: FinReportDailyRequest) -> Mapping[str, Any]:
        validate_finance_daily_request(request.snapshot_date, request.nm_ids)
        client = self._client
        if client is None:
            runtime = load_runtime_config(
                token_env_var=self._token_env_var,
                default_base_url=self._default_base_url,
                base_url_env_var=self._base_url_env_var,
                default_timeout_seconds=self._default_timeout_seconds,
            )
            client = WbFinanceApiClient(
                runtime.token,
                url=_finance_detailed_url(runtime.base_url),
                rate_gate_root=(
                    self._runtime_dir
                    or Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload"))
                ),
            )
        diagnostics = {**unknown_diagnostics("fin_report_daily"),
                       **new_attempt("fin_report_daily", request.snapshot_date),
                       "endpoint": "POST /api/finance/v1/sales-reports/detailed",
                       "mode": "official_finance_daily", "period": "daily",
                       "requested_count": len(set(request.nm_ids))}
        try:
            fetched = client.fetch_report(
                date_from=request.snapshot_date, date_to=request.snapshot_date, period="daily",
            )
        except FinanceApiError as exc:
            diagnostics.update(
                attempt_status="rejected", error_code=exc.code,
                source_digest=getattr(exc, "source_digest", None),
                source_observed_at=getattr(exc, "source_observed_at", None),
                pagination={"pages": exc.pages, "rrdid_start": 0, "rrdid_end": exc.cursor,
                            "terminal_status": exc.http_status, "complete": False},
            )
            observed_rows = getattr(exc, "_observed_rows", None)
            if observed_rows is not None:
                # A failed stream cannot use a complete-report qualification.
                diagnostics.update(project_finance_daily_report(
                    observed_rows, snapshot_date=request.snapshot_date,
                    nm_ids=request.nm_ids, source=diagnostics,
                ).diagnostics)
            exc.diagnostics = diagnostics
            raise
        diagnostics.update(source_observed_at=fetched.source_observed_at, source_digest=fetched.source_digest,
                           pagination={"pages": fetched.pages, "rrdid_start": 0,
                                       "rrdid_end": fetched.rrd_id_end,
                                       "terminal_status": fetched.terminal_status,
                                       "complete": fetched.terminal_status == 204})
        try:
            projected = project_finance_daily_report(
                fetched.rows, snapshot_date=request.snapshot_date, nm_ids=request.nm_ids,
                source=diagnostics, basis=self._report_basis,
            )
        except ValueError as exc:
            raise SourceAttemptError("finance_daily_report_basis_invalid", diagnostics) from exc
        diagnostics.update(projected.diagnostics)
        if diagnostics["finance_report"]["projection_status"] != "complete":
            raise SourceAttemptError("finance_daily_report_unusable", diagnostics)
        diagnostics["attempt_status"] = "returned"
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": diagnostics,
            "data": {"rows": projected.rows},
        }


def _finance_detailed_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/api/finance/v1/sales-reports/detailed"):
        return normalized
    return normalized + "/api/finance/v1/sales-reports/detailed"

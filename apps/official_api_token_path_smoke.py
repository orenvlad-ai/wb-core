"""Targeted smoke-check for canonical WB API token path."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.ads_bids_block import HttpBackedAdsBidsSource
from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource
from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV
from packages.adapters.prices_snapshot_block import HttpBackedPricesSnapshotSource
from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.adapters.sf_period_block import HttpBackedSfPeriodSource
from packages.adapters.spp_block import HttpBackedSppSource
from packages.adapters.stocks_block import HttpBackedStocksSource


def main() -> None:
    sources = {
        "prices_snapshot": HttpBackedPricesSnapshotSource(),
        "sf_period": HttpBackedSfPeriodSource(),
        "spp": HttpBackedSppSource(),
        "ads_bids": HttpBackedAdsBidsSource(),
        "stocks": HttpBackedStocksSource(),
        "sales_funnel_history": HttpBackedSalesFunnelHistorySource(),
        "ads_compact": HttpBackedAdsCompactSource(),
        "fin_report_daily": HttpBackedFinReportDailySource(),
    }
    for source_key, source in sources.items():
        token_env_var = getattr(source, "_token_env_var", None)
        if token_env_var != DEFAULT_WB_API_TOKEN_ENV:
            raise SystemExit(
                f"{source_key} token_env_var mismatch: expected {DEFAULT_WB_API_TOKEN_ENV}, got {token_env_var!r}"
            )
    print(f"canonical_token_env: ok -> {DEFAULT_WB_API_TOKEN_ENV}")
    print("official-api-token-path-smoke passed")


if __name__ == "__main__":
    main()

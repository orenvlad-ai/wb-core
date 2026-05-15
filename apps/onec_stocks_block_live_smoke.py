"""Optional live smoke for the bounded 1C/Soykasoft WB stocks source."""

from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.onec_stocks_block import (
    HttpBackedOnecStocksSource,
    ONEC_STOCKS_SMOKE_ACCOUNT_ID_ENV,
    ONEC_STOCKS_SMOKE_NM_ID_ENV,
    missing_onec_stocks_live_env,
)
from packages.application.onec_stocks_block import OnecStocksBlock
from packages.contracts.onec_stocks_block import OnecStocksRequest


DEFAULT_SMOKE_ACCOUNT_ID = "000000001"
DEFAULT_SMOKE_NM_ID = "428855306"


def main() -> None:
    require_live = "--require-live" in sys.argv
    missing = missing_onec_stocks_live_env()
    if missing:
        print("live-smoke skipped: missing env " + ", ".join(missing))
        if require_live:
            raise SystemExit(2)
        return

    account_id = os.environ.get(ONEC_STOCKS_SMOKE_ACCOUNT_ID_ENV, DEFAULT_SMOKE_ACCOUNT_ID).strip()
    nm_id_raw = os.environ.get(ONEC_STOCKS_SMOKE_NM_ID_ENV, DEFAULT_SMOKE_NM_ID).strip()
    if not account_id:
        raise SystemExit(f"{ONEC_STOCKS_SMOKE_ACCOUNT_ID_ENV} must be non-empty")
    if not nm_id_raw.isdigit():
        raise SystemExit(f"{ONEC_STOCKS_SMOKE_NM_ID_ENV} must contain digits only")

    source = HttpBackedOnecStocksSource()
    block = OnecStocksBlock(source)
    result = block.execute(
        OnecStocksRequest(
            snapshot_type="onec_stocks",
            account_id=account_id,
            nm_ids=[int(nm_id_raw)],
        )
    ).result
    if result.kind != "success":
        raise SystemExit(f"unexpected result kind: {result.kind}")
    if result.item_count < 1 or result.stage_count < 1:
        raise SystemExit(
            f"unexpected live counts: item_count={result.item_count}, stage_count={result.stage_count}"
        )
    print(f"live-smoke: ok -> {result.kind}")
    print(f"live-smoke: item_count={result.item_count}, stage_count={result.stage_count}")


if __name__ == "__main__":
    main()

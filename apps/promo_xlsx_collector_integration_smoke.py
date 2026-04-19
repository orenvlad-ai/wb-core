"""Bounded live integration smoke for repo-owned promo XLSX collector block."""

from __future__ import annotations

from tempfile import mkdtemp
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.promo_xlsx_collector_block import DEFAULT_SESSION_STATE_PATH, PlaywrightPromoCollectorDriver  # noqa: E402
from packages.application.promo_xlsx_collector_block import PromoXlsxCollectorBlock  # noqa: E402
from packages.contracts.promo_xlsx_collector_block import PromoXlsxCollectorRequest  # noqa: E402


def main() -> None:
    tmp = mkdtemp(prefix="wb-core-promo-xlsx-collector-smoke-")
    request = PromoXlsxCollectorRequest(
        output_root=tmp,
        storage_state_path=DEFAULT_SESSION_STATE_PATH,
        headless=True,
        max_candidates=5,
    )
    block = PromoXlsxCollectorBlock(PlaywrightPromoCollectorDriver(Path(tmp)))
    result = block.execute(request)
    if result.status not in {"success", "partial"}:
        raise AssertionError(f"integration smoke must reach bounded success/partial state, got {result.status}")
    if result.downloaded_count < 1:
        raise AssertionError("integration smoke must prove at least one current/future download with sidecar")
    if not result.hydration_attempts or not any(attempt.hydrated_success for attempt in result.hydration_attempts):
        raise AssertionError("canonical direct_open hydration sequence must succeed")
    downloaded = [promo for promo in result.promos if promo.status == "downloaded"]
    if not downloaded:
        raise AssertionError("downloaded promo outcome list must not be empty")
    first_downloaded = downloaded[0]
    if not first_downloaded.metadata_path or not Path(first_downloaded.metadata_path).exists():
        raise AssertionError("downloaded promo must persist metadata.json")
    if not first_downloaded.saved_path or not Path(first_downloaded.saved_path).exists():
        raise AssertionError("downloaded promo must persist workbook.xlsx")
    print(f"run_dir: {tmp}")
    print(f"downloaded_count: {result.downloaded_count}")
    print(f"skipped_past_count: {result.skipped_past_count}")
    print(f"first_downloaded: {first_downloaded.promo_title}")
    print("integration-smoke passed")


if __name__ == "__main__":
    main()

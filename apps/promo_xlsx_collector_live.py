"""Local live runner for bounded promo XLSX collector block."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
from tempfile import mkdtemp

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.promo_xlsx_collector_block import DEFAULT_SESSION_STATE_PATH, PlaywrightPromoCollectorDriver  # noqa: E402
from packages.application.promo_xlsx_collector_block import PromoXlsxCollectorBlock  # noqa: E402
from packages.contracts.promo_xlsx_collector_block import PromoXlsxCollectorRequest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--storage-state-path", default=DEFAULT_SESSION_STATE_PATH)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--max-downloads", type=int, default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    output_root = args.output_root or mkdtemp(prefix="wb-core-promo-xlsx-collector-")
    headless = True
    if args.headed:
        headless = False
    elif args.headless:
        headless = True

    request = PromoXlsxCollectorRequest(
        output_root=output_root,
        storage_state_path=args.storage_state_path,
        headless=headless,
        max_candidates=args.max_candidates,
        max_downloads=args.max_downloads,
    )
    driver = PlaywrightPromoCollectorDriver(Path(output_root))
    block = PromoXlsxCollectorBlock(driver)
    result = block.execute(request)
    print(f"run_dir: {output_root}")
    print(f"status: {result.status}")
    print(f"timeline_candidates_found: {result.timeline_candidates_found}")
    print(f"card_confirmed_count: {result.card_confirmed_count}")
    print(f"downloaded_count: {result.downloaded_count}")
    print(f"skipped_past_count: {result.skipped_past_count}")
    print(f"blocked_before_card_count: {result.blocked_before_card_count}")
    print(f"blocked_after_card_count: {result.blocked_after_card_count}")
    print(f"blocked_before_download_count: {result.blocked_before_download_count}")
    print(f"export_kinds: {','.join(result.export_kinds)}")


if __name__ == "__main__":
    main()

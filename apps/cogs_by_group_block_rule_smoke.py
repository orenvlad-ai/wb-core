"""Минимальный smoke-check для fixture-backed rule-source cogs path."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.cogs_by_group_block import RuleBackedCogsByGroupSource
from packages.application.cogs_by_group_block import CogsByGroupBlock
from packages.contracts.cogs_by_group_block import CogsByGroupRequest

ARTIFACTS = ROOT / "artifacts" / "cogs_by_group_block"


def main() -> None:
    source = RuleBackedCogsByGroupSource(ARTIFACTS)
    block = CogsByGroupBlock(source)
    result = asdict(
        block.execute(
            CogsByGroupRequest(
                snapshot_type="cogs_by_group",
                date_from="2026-04-01",
                date_to="2026-04-04",
                nm_ids=[210183919, 210184534],
            )
        )
    )

    if result["result"]["kind"] != "success":
        raise AssertionError(f"expected success, got {result['result']['kind']}")
    if result["result"]["count"] != 8:
        raise AssertionError(f"expected 8 rows, got {result['result']['count']}")

    items = {
        (item["date"], item["nm_id"]): item for item in result["result"]["items"]
    }
    if items[("2026-04-03", 210183919)]["cost_price_rub"] != 400.0:
        raise AssertionError("expected cost_price_rub=400.0 for 2026-04-03/210183919")
    if items[("2026-04-02", 210184534)]["cost_price_rub"] != 200.0:
        raise AssertionError("expected cost_price_rub=200.0 for 2026-04-02/210184534")

    print("normal: ok -> success")
    print("normal: count -> 8")
    print("rule-smoke-check passed")


if __name__ == "__main__":
    main()

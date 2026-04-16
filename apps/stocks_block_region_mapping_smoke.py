"""Targeted smoke-check for stocks region normalization and district decomposition."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.stocks_block import transform_legacy_payload


def main() -> None:
    _check_live_aliases_and_unmapped_note()
    _check_legacy_aliases_still_map()
    print("smoke-check passed")


def _check_live_aliases_and_unmapped_note() -> None:
    payload = {
        "snapshot_date": "2026-04-15",
        "requested_nm_ids": [101],
        "data": {
            "rows": [
                _row("2026-04-15", 101, "Центральный", 10),
                _row("2026-04-15", 101, "Южный и Северо-Кавказский", 5),
                _row("2026-04-15", 101, "Дальневосточный и Сибирский", 2),
                _row("2026-04-15", 101, "Армения", 1),
            ]
        },
    }
    result = asdict(transform_legacy_payload(payload))
    item = result["result"]["items"][0]
    if item["stock_total"] != 18.0:
        raise AssertionError(f"unexpected total stock after alias normalization: {item}")
    if item["stock_ru_south_caucasus"] != 5.0:
        raise AssertionError(f"south/caucasus stock must survive live alias normalization: {item}")
    if item["stock_ru_far_siberia"] != 2.0:
        raise AssertionError(f"far/siberia stock must survive live alias normalization: {item}")
    detail = result["result"]["detail"]
    if "Армения=1" not in detail:
        raise AssertionError(f"unmapped non-district quantity must surface in detail, got {detail!r}")
    print("live-shaped-region-aliases: ok -> south/caucasus and far/siberia survive new endpoint naming")


def _check_legacy_aliases_still_map() -> None:
    payload = {
        "snapshot_date": "2026-04-05",
        "requested_nm_ids": [202],
        "data": {
            "rows": [
                _row("2026-04-05", 202, "Южный + Северо-Кавказский", 7),
                _row("2026-04-05", 202, "Дальневосточный + Сибирский", 3),
            ]
        },
    }
    result = asdict(transform_legacy_payload(payload))
    item = result["result"]["items"][0]
    if item["stock_ru_south_caucasus"] != 7.0 or item["stock_ru_far_siberia"] != 3.0:
        raise AssertionError(f"legacy aliases must remain backward-compatible, got {item}")
    if result["result"]["detail"] != "":
        raise AssertionError(f"mapped legacy regions must not create unmapped detail, got {result['result']['detail']!r}")
    print("legacy-region-aliases: ok -> old plus-sign naming remains compatible")


def _row(snapshot_date: str, nm_id: int, region_name: str, stock_count: float) -> dict[str, object]:
    return {
        "snapshot_date": snapshot_date,
        "snapshot_ts": f"{snapshot_date} 12:00:00",
        "nmId": nm_id,
        "regionName": region_name,
        "stockCount": float(stock_count),
    }


if __name__ == "__main__":
    main()

"""Check the published readback against the scenario's independent arithmetic."""
from math import isclose


def verify(result, scenario, variant):
    expected = scenario["expected"]
    count = 0

    def equal(actual, wanted, label):
        nonlocal count
        count += 1
        numeric = type(wanted) in (int, float) and type(actual) in (int, float)
        matches = isclose(actual, wanted, rel_tol=1e-10, abs_tol=1e-10) if numeric else actual == wanted
        if not matches:
            raise ValueError(f"Проверка стенда: {label}: ожидалось {wanted!r}, получено {actual!r}")

    sku = f"SKU:{expected['sku']}|"
    closed, today = scenario["days"]
    equal(result["as_of"], closed, "as_of")
    equal(result["date_columns"], [closed, today], "date_columns")
    equal(result["finance_week_rows"], expected["finance_week_rows"], "Finance weekly rows (adapter probe)")
    equal(result["fbs"]["chrt_id"], expected["fbs_chrt_id"], "FBS chrtId (adapter probe)")
    equal(result["fbs"]["amount"], expected["fbs_amount"], "FBS quantity (adapter probe)")
    slots = {(s["source_key"], s["date"]): s for s in result["source_slots"]}

    def cell(key, day, wanted):
        equal(result["cells"][key][day], wanted, f"{key}@{day}")

    for day, current_only in ((closed, "closed_current_only"), (today, "today_current_only")):
        for metric, wanted in expected[current_only].items():
            cell(sku + metric, day, wanted)
        for source in ("card_rating", "prices_snapshot", "spp", "spp_proxy", "ads_bids"):
            equal(slots[source, day]["accepted"], True, f"accepted {source}@{day}")
        for metric, wanted in expected["daily"].items():
            cell(sku + metric, day, wanted)
        cell("TOTAL|fin_storage_fee_total", day, expected["storage_total"])
        equal(result["finance_daily"][day]["storage_total"], expected["storage_total"], f"Finance storage@{day}")
        # Only SKU 101 belongs to Vitrina, but cabinet-wide storage includes 999.
        equal(result["finance_daily"][day]["buyout_rub"], {str(expected["sku"]): expected["daily"]["fin_buyout_rub"]},
              f"Finance SKU scope@{day}")
        for metric, regular in expected["ads"].items():
            wanted = regular
            if day == closed and variant == "missing":
                wanted = ""
            elif day == closed and variant == "zero":
                wanted = "" if metric in {"ads_cpc", "ads_ctr", "ads_cr"} else 0
            cell(sku + metric, day, wanted)
        wanted_sum = "" if day == closed and variant == "missing" else 0 if day == closed and variant == "zero" else expected["ads"]["ads_sum"]
        cell("TOTAL|total_ads_sum", day, wanted_sum)
        ad = slots["ads_compact", day]
        unavailable = day == closed and variant == "missing"
        equal(ad["accepted"], not unavailable, f"accepted ads@{day}")
        equal(ad["kind"], "closure_retrying" if unavailable else "incomplete", f"ads kind@{day}")
        if not unavailable:
            for key in (sku + "ads_sum", "TOTAL|total_ads_sum"):
                presentation = result["cell_presentation"][key][day]
                equal(presentation["quality_state"], "partial", f"ads quality {key}@{day}")
                equal(presentation["completeness_state"], "unknown_scope", f"ads completeness {key}@{day}")
                equal(presentation["evidence"]["zero_fill_applied"], False, f"ads zero_fill {key}@{day}")
    cell(sku + "stock_total", closed, expected["closed_stock"])
    cell(sku + "stock_total", today, "")
    cell(sku + "stock_ru_central", closed, "")  # Region-only fixture is not a warehouse proof.
    return {"status": "passed", "checks": count, "scope": "source collection + Ready readback; FBS/weekly adapter probes"}

from canonical_cost_engine_vitrina_publication import _value_for_metric


def main() -> int:
    lookup = {
        101: {
            "stages": {
                "PRODUCTION": {"physical_quantity": 10, "paid_capital_rub": 100, "paid_equivalent_quantity": 10},
                "FF": {"physical_quantity": 2, "paid_capital_rub": 30, "paid_equivalent_quantity": 2},
                "FF_TO_WB": {"physical_quantity": 3, "paid_capital_rub": 45, "paid_equivalent_quantity": 3},
                "WB": {"physical_quantity": 4, "paid_capital_rub": 80, "paid_equivalent_quantity": 4, "recognized_capital_rub": 88},
            }
        }
    }
    assert _value_for_metric("onec_CHINA_TO_FF_qty", 101, lookup) == 10
    assert _value_for_metric("onec_CHINA_TO_FF_unit_cost_rub", 101, lookup) == 10
    assert _value_for_metric("onec_FF_STOCK_qty", 101, lookup) == 2
    assert _value_for_metric("onec_FF_TO_WB_cost_total_rub", 101, lookup) == 45
    assert _value_for_metric("onec_WB_STOCK_unit_cost_rub", 101, lookup) == 20
    assert _value_for_metric("our_wb_unit_cost_rub", 101, lookup) == 22
    print("canonical_cost_engine_vitrina_publication_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

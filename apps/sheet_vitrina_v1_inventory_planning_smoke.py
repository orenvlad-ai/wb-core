#!/usr/bin/env python3
"""Contract smoke for inventory_planning_v1 rows in the main Web Vitrina."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.application.ff_pool_cutover import MANIFESTS_TABLE  # noqa: E402
from packages.application.ff_pool_fbs_lifecycle import CURRENT_TABLE  # noqa: E402
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.inventory_planning_read_model import (  # noqa: E402
    INCIDENT_LINES_TABLE,
    INCIDENT_MANIFESTS_TABLE,
)
from packages.application.sheet_vitrina_v1_inventory_planning import (  # noqa: E402
    COMBINED_EFFECTIVE_ALIAS_KEY,
    COMBINED_TOTAL_ALIAS_KEY,
    INVENTORY_FBS_TOTAL_KEY,
    INVENTORY_WB_EFFECTIVE_KEY,
    INVENTORY_WB_TOTAL_KEY,
    inventory_planning_facility_metric_key,
    inventory_planning_total_metric_key,
    is_inventory_planning_presentation_metric_key,
)
from packages.application.warehouse_functional import STAGES  # noqa: E402


CURRENT_DATE = "2026-04-21"
NOW = "2026-04-21T12:00:00Z"
MISSING_INCIDENT_REASON = (
    "Недоступно: для активного инцидента нет exact persisted quantity "
    "evidence по полному SKU-срезу текущего снимка WB."
)


def main() -> int:
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    fixture.__enter__()
    try:
        runtime = fixture.entrypoint.runtime
        enabled = [item for item in runtime.load_current_state().config_v2 if item.enabled]
        first_nm_id, second_nm_id = int(enabled[0].nm_id), int(enabled[1].nm_id)
        _seed_inventory_planning(runtime.db_path, nm_ids=(first_nm_id, second_nm_id))

        before = runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-04-20")
        contract = fixture.entrypoint.web_vitrina_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from="2026-04-08",
            date_to=CURRENT_DATE,
        )
        after = runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-04-20")
        assert before == after, "read-time planning rows must not rewrite ready history"

        rows = {row.row_id: row for row in contract.rows}
        sku_specs = (
            (INVENTORY_WB_TOTAL_KEY, "Остаток WB: всего"),
            (INVENTORY_WB_EFFECTIVE_KEY, "Остаток WB без инц.: всего"),
            (INVENTORY_FBS_TOTAL_KEY, "Остаток FBS: всего"),
            (inventory_planning_facility_metric_key("moscow"), "Остаток FBS: Москва"),
            (COMBINED_EFFECTIVE_ALIAS_KEY, "Остаток без инц.: всего"),
            (COMBINED_TOTAL_ALIAS_KEY, "Остаток: всего"),
        )
        for sku_key, label in sku_specs:
            total_key = inventory_planning_total_metric_key(sku_key)
            assert rows[f"TOTAL|{total_key}"].metric_label == label
            for nm_id in (first_nm_id, second_nm_id):
                assert rows[f"SKU:{nm_id}|{sku_key}"].metric_label == label

        logical_labels = {
            (row.metric_key.removeprefix("total_"), row.metric_label)
            for row in contract.rows
            if is_inventory_planning_presentation_metric_key(row.metric_key)
        }
        assert logical_labels == set(sku_specs), logical_labels

        assert _value(rows, f"SKU:{first_nm_id}|{INVENTORY_WB_TOTAL_KEY}") == 10
        assert _value(rows, f"SKU:{second_nm_id}|{INVENTORY_WB_TOTAL_KEY}") == 20
        assert _value(rows, f"SKU:{first_nm_id}|{INVENTORY_FBS_TOTAL_KEY}") == -3
        assert _value(rows, f"SKU:{second_nm_id}|{INVENTORY_FBS_TOTAL_KEY}") == 10
        assert _value(rows, f"SKU:{first_nm_id}|{COMBINED_TOTAL_ALIAS_KEY}") == 7
        assert _value(rows, f"SKU:{second_nm_id}|{COMBINED_TOTAL_ALIAS_KEY}") == 30
        assert _value(rows, f"TOTAL|{inventory_planning_total_metric_key(INVENTORY_FBS_TOTAL_KEY)}") == 7
        assert _value(rows, f"TOTAL|{inventory_planning_total_metric_key(COMBINED_TOTAL_ALIAS_KEY)}") == 37
        assert 37 == 7 + 30, "TOTAL must equal WB + FBS once, without FF/FBS double count"

        for row_id in (
            f"SKU:{first_nm_id}|{INVENTORY_WB_EFFECTIVE_KEY}",
            f"SKU:{second_nm_id}|{INVENTORY_WB_EFFECTIVE_KEY}",
            f"TOTAL|{inventory_planning_total_metric_key(INVENTORY_WB_EFFECTIVE_KEY)}",
            f"SKU:{first_nm_id}|{COMBINED_EFFECTIVE_ALIAS_KEY}",
            f"TOTAL|{inventory_planning_total_metric_key(COMBINED_EFFECTIVE_ALIAS_KEY)}",
        ):
            row = rows[row_id]
            assert row.values_by_date[CURRENT_DATE] == ""
            presentation = row.presentation_by_date[CURRENT_DATE]
            assert presentation["quality_state"] == "inventory_planning_unavailable"
            assert presentation["reason"] == MISSING_INCIDENT_REASON

        _seed_exact_incident_evidence(
            runtime.db_path,
            nm_ids=(first_nm_id, second_nm_id),
        )
        exact_contract = fixture.entrypoint.web_vitrina_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from="2026-04-08",
            date_to=CURRENT_DATE,
        )
        exact_rows = {row.row_id: row for row in exact_contract.rows}
        assert _value(exact_rows, f"SKU:{first_nm_id}|{INVENTORY_WB_EFFECTIVE_KEY}") == 6
        assert _value(exact_rows, f"SKU:{second_nm_id}|{INVENTORY_WB_EFFECTIVE_KEY}") == 19
        assert _value(exact_rows, f"SKU:{first_nm_id}|{COMBINED_EFFECTIVE_ALIAS_KEY}") == 3
        assert _value(exact_rows, f"SKU:{second_nm_id}|{COMBINED_EFFECTIVE_ALIAS_KEY}") == 29
        assert _value(
            exact_rows,
            f"TOTAL|{inventory_planning_total_metric_key(COMBINED_EFFECTIVE_ALIAS_KEY)}",
        ) == 32

        # The familiar effective alias keeps exact old-date evidence while only
        # current inventory_planning_v1 becomes fail-closed.
        assert rows[f"TOTAL|{inventory_planning_total_metric_key(COMBINED_EFFECTIVE_ALIAS_KEY)}"].values_by_date[
            "2026-04-20"
        ] == 5
        assert rows[f"SKU:{first_nm_id}|{COMBINED_EFFECTIVE_ALIAS_KEY}"].values_by_date[
            "2026-04-20"
        ] == 5

        assert STAGES == (
            "production",
            "china_to_ff",
            "ff",
            "ff_to_wb",
            "wb",
            "wb_acceptance_discrepancy",
        )
        for consumer_path in (
            ROOT / "packages/application/factory_order_supply.py",
            ROOT / "packages/application/wb_regional_supply.py",
            ROOT / "packages/application/wb_supply_overlay.py",
        ):
            consumer_source = consumer_path.read_text(encoding="utf-8")
            assert "inventory_wb_total_qty_v1" not in consumer_source
            assert "inventory_fbs_total_qty_v1" not in consumer_source

        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {FACILITIES_TABLE} SET active=1,updated_at=? WHERE facility_id='orenburg'",
                ("2026-04-21T13:00:00Z",),
            )
            conn.commit()
        activated = fixture.entrypoint.web_vitrina_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from="2026-04-08",
            date_to=CURRENT_DATE,
        )
        activated_keys = {row.metric_key for row in activated.rows}
        assert inventory_planning_facility_metric_key("orenburg") in activated_keys
        assert _value(
            {row.row_id: row for row in activated.rows},
            f"TOTAL|{inventory_planning_total_metric_key(INVENTORY_FBS_TOTAL_KEY)}",
        ) == 207

        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {FACILITIES_TABLE} SET active=0,updated_at=? WHERE facility_id='moscow'",
                ("2026-04-21T14:00:00Z",),
            )
            conn.commit()
        deactivated = fixture.entrypoint.web_vitrina_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from="2026-04-08",
            date_to=CURRENT_DATE,
        )
        deactivated_keys = {row.metric_key for row in deactivated.rows}
        assert inventory_planning_facility_metric_key("moscow") not in deactivated_keys
        assert inventory_planning_facility_metric_key("orenburg") in deactivated_keys

        historical = fixture.entrypoint.web_vitrina_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            as_of_date="2026-04-20",
        )
        assert not any(
            is_inventory_planning_presentation_metric_key(row.metric_key)
            and row.metric_key not in {
                COMBINED_EFFECTIVE_ALIAS_KEY,
                inventory_planning_total_metric_key(COMBINED_EFFECTIVE_ALIAS_KEY),
            }
            for row in historical.rows
        )
        historical_effective = next(
            row
            for row in historical.rows
            if row.row_id == f"TOTAL|{inventory_planning_total_metric_key(COMBINED_EFFECTIVE_ALIAS_KEY)}"
        )
        assert historical_effective.metric_label == "Остаток без инц.: всего"
        assert historical_effective.values_by_date["2026-04-20"] == 5
    finally:
        fixture.__exit__(None, None, None)

    print("sheet_vitrina_v1_inventory_planning_smoke: OK")
    return 0


def _value(rows: dict[str, object], row_id: str) -> int | str:
    return rows[row_id].values_by_date[CURRENT_DATE]  # type: ignore[attr-defined]


def _seed_inventory_planning(db_path: Path, *, nm_ids: tuple[int, int]) -> None:
    first_nm_id, second_nm_id = nm_ids
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at) VALUES(1,'planning-v1-current',?)",
            (NOW,),
        )
        raw_rows = [
            {
                "nmId": nm_id,
                "warehouseId": -999999,
                "warehouseName": "Склад WB",
                "regionName": "Склад WB",
                "quantity": quantity,
            }
            for nm_id, quantity in ((first_nm_id, 10), (second_nm_id, 20))
        ]
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                   snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,
                   pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,
                   raw_rows_json,items_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "planning-v1-wb-current",
                "planning-v1-current",
                NOW,
                CURRENT_DATE,
                json.dumps(list(nm_ids)),
                1,
                1,
                "[0]",
                2,
                "sha256:planning-v1-wb-current",
                json.dumps(raw_rows, ensure_ascii=False),
                json.dumps(
                    [
                        {"nm_id": first_nm_id, "quantity": 10},
                        {"nm_id": second_nm_id, "quantity": 20},
                    ]
                ),
                NOW,
            ),
        )
        revision = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM sheet_vitrina_v1_wb_incident_policy_revisions WHERE seller_id='canonical'"
            ).fetchone()[0]
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_incident_policy_revisions(
                   seller_id,revision,active,warehouse_ids_json,warehouse_identities_json,
                   warehouse_entries_json,reason,effective_from,effective_to,policy_status,
                   actor,created_at,source,legacy_payloads_json
               ) VALUES('canonical',?,1,'[507]','[]','[]','browser smoke','2026-04-01','',
                        'active','smoke',?,'canonical_incident_registry','[]')""",
            (revision, NOW),
        )
        epoch = int(
            conn.execute(f"SELECT COALESCE(MAX(epoch),0)+100 FROM {FEATURE_EPOCHS_TABLE}").fetchone()[0]
        )
        conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(?,1,1,'planning-v1-smoke',?,'{{}}')",
            (epoch, NOW),
        )
        for facility_id, code, name, active in (
            ("moscow", "FF-MOSCOW", "Москва", 1),
            ("orenburg", "FF-ORENBURG", "Оренбург", 0),
        ):
            conn.execute(
                f"""INSERT INTO {FACILITIES_TABLE}(
                       facility_id,code,name,active,display_timezone,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(facility_id) DO UPDATE SET
                     code=excluded.code,name=excluded.name,active=excluded.active,
                     updated_at=excluded.updated_at""",
                (facility_id, code, name, active, "Asia/Yekaterinburg", NOW, NOW),
            )
            conn.execute(
                f"""INSERT INTO {FACILITY_PROFILES_TABLE}(
                       facility_id,city,future_fields_json,created_at,updated_at
                   ) VALUES(?,?,'{{}}',?,?)
                   ON CONFLICT(facility_id) DO UPDATE SET
                     city=excluded.city,updated_at=excluded.updated_at""",
                (facility_id, name, NOW, NOW),
            )
        balances = (
            ("moscow", first_nm_id, 5),
            ("moscow", second_nm_id, 11),
            ("orenburg", first_nm_id, 100),
            ("orenburg", second_nm_id, 100),
        )
        conn.executemany(
            f"""INSERT INTO {BALANCES_TABLE}(
                   facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,
                   source_watermark,updated_at
               ) VALUES(?,'FBS',?,?,?,'0',NULL,'planning-v1-smoke',?)""",
            ((facility_id, nm_id, epoch, quantity, NOW) for facility_id, nm_id, quantity in balances),
        )
        conn.execute(
            f"""INSERT INTO {MANIFESTS_TABLE}(
                   cutover_id,manifest_digest,deployed_sha,cutover_at,business_date,feature_epoch,
                   aggregate_revision,aggregate_digest,detail_digest,observation_watermark_sequence,
                   observation_watermark_digest,mapping_digest,fbw_origins_digest,
                   control_evidence_digest,non_target_digest,opening_document_id,
                   source_snapshot_digest,created_at,manifest_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "planning-v1-cutover",
                "sha256:planning-v1-manifest",
                "a" * 40,
                "2026-04-20T10:00:00Z",
                "2026-04-20",
                epoch,
                "planning-v1-current",
                "sha256:aggregate",
                "sha256:detail",
                1,
                "sha256:watermark",
                "sha256:mapping",
                "sha256:origins",
                "sha256:control",
                "sha256:non-target",
                "planning-v1-opening",
                "sha256:snapshot",
                NOW,
                "{}",
            ),
        )
        for order_id, nm_id, quantity in (
            (9101, first_nm_id, 8),
            (9102, second_nm_id, 1),
        ):
            conn.execute(
                f"""INSERT INTO {CURRENT_TABLE}(
                       cutover_id,order_id,state,episode_sequence,source_revision,status_digest,
                       supplier_status,wb_status,facility_id,pool,nm_id,quantity,frozen_wac_rub,
                       debit_event_id,updated_at
                   ) VALUES('planning-v1-cutover',?,'reserved',1,?,?,
                            'confirm','waiting','moscow','FBS',?,?,'0','',?)""",
                (
                    order_id,
                    f"sha256:order-{order_id}",
                    f"sha256:status-{order_id}",
                    nm_id,
                    quantity,
                    NOW,
                ),
            )
        conn.commit()


def _seed_exact_incident_evidence(db_path: Path, *, nm_ids: tuple[int, int]) -> None:
    first_nm_id, second_nm_id = nm_ids
    lines = [
        {"nm_id": first_nm_id, "incident_quantity": 4},
        {"nm_id": second_nm_id, "incident_quantity": 1},
    ]
    scope_material = json.dumps(
        sorted(nm_ids),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    scope_digest = "sha256:" + hashlib.sha256(scope_material.encode("utf-8")).hexdigest()
    with sqlite3.connect(db_path) as conn:
        revision = int(
            conn.execute(
                "SELECT MAX(revision) FROM sheet_vitrina_v1_wb_incident_policy_revisions WHERE seller_id='canonical'"
            ).fetchone()[0]
        )
        material = {
            "seller_id": "canonical",
            "policy_revision": revision,
            "wb_snapshot_id": "planning-v1-wb-current",
            "wb_snapshot_digest": "sha256:planning-v1-wb-current",
            "evidence_date": CURRENT_DATE,
            "sku_scope_digest": scope_digest,
            "lines": lines,
        }
        evidence_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        conn.execute(
            f"""INSERT INTO {INCIDENT_MANIFESTS_TABLE}(
                   evidence_id,seller_id,policy_revision,wb_snapshot_id,wb_snapshot_digest,
                   evidence_date,sku_scope_digest,evidence_digest,source,captured_at,metadata_json
               ) VALUES('planning-v1-incident','canonical',?,'planning-v1-wb-current',
                        'sha256:planning-v1-wb-current',?,?,?,?,?,'{{}}')""",
            (
                revision,
                CURRENT_DATE,
                scope_digest,
                evidence_digest,
                "canonical_incident_registry",
                "2026-04-21T12:30:00Z",
            ),
        )
        conn.executemany(
            f"INSERT INTO {INCIDENT_LINES_TABLE}(evidence_id,nm_id,incident_quantity,evidence_digest) VALUES('planning-v1-incident',?,?,?)",
            (
                (line["nm_id"], line["incident_quantity"], evidence_digest)
                for line in lines
            ),
        )
        conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())

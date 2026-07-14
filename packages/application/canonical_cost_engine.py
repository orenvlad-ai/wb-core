"""Unified cost, physical-stage and invested-capital projections.

The tables owned by this module are derived audit/projection tables.  Physical
quantity is always read from the supplier registry, FF ledger, persisted WB
supply evidence and the official WB stock snapshot.  Legacy module-40/45 rows
are publication targets only; they are never inputs to the engine.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)
from packages.application.our_wb_costs import _extract_snapshot_sku_metric


CUTOVER_DATE = "2026-07-01"
ONEC_FALLBACK_LAST_DATE = "2026-05-16"
PRIMARY_ACCEPTED_DATE_FROM = "2026-06-21"
PRIMARY_ACCEPTED_DATE_TO = "2026-06-24"
EXPECTED_PRIMARY_AVG_RUB = Decimal("111.181389")
PRIMARY_AVG_TOLERANCE_RUB = Decimal("0.01")
PRIMARY_MIN_QUANTITY = Decimal("100000")

ZERO = Decimal("0")
ONE = Decimal("1")

STAGE_PRODUCTION = "PRODUCTION"
STAGE_PRODUCTION_TO_FF = "PRODUCTION_TO_FF"
STAGE_FF = "FF"
STAGE_FF_TO_WB = "FF_TO_WB"
STAGE_WB = "WB"
STAGES = (
    STAGE_PRODUCTION,
    STAGE_PRODUCTION_TO_FF,
    STAGE_FF,
    STAGE_FF_TO_WB,
    STAGE_WB,
)

PROJECTION_RECOGNIZED = "recognized"
PROJECTION_PAID = "paid"

BASELINE_PRIMARY = "primary_supplier_shipment"
BASELINE_ONEC = "legacy_1c_fallback"
BASELINE_BUSINESS_APPROVED_PRIMARY_WAC = "business_approved_primary_wac_fallback"
BUSINESS_APPROVED_PRIMARY_WAC_NM_IDS = frozenset({497415593, 497416931})
BUSINESS_APPROVED_PRIMARY_WAC_DECISION_DATE = "2026-07-13"
BUSINESS_APPROVED_PRIMARY_WAC_METHOD = "primary shipment weighted FF cost"
BUSINESS_APPROVED_PRIMARY_WAC_REASON = (
    "discontinued_immaterial_sku_business_approved_estimate"
)

ONEC_FF_UNIT_COST_METRIC = "onec_FF_STOCK_unit_cost_rub"
OFFICIAL_WB_STOCK_METRIC = "stock_total"

CANONICAL_TABLE_PREFIX = "sheet_vitrina_v1_canonical_cost_"

TARGETED_PRE_ACTIVATION_REMEDIATION_REASON = "targeted_pre_activation_remediation"
POSTCUTOVER_NORMALIZATION_POLICY = "CUTOVER_POSTCUTOVER_SOURCE_NORMALIZATION_V1"
UNMATCHED_DOPRINATO_ABSORPTION_POLICY = (
    "CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V1"
)
UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION = (
    "unmatched_doprinato_absorbed_by_official_wb_stock"
)
UNMATCHED_DOPRINATO_ABSORPTION_SOURCE_QUALITY = (
    "exact_unmatched_doprinato_absorbed"
)
UNMATCHED_DOPRINATO_ABSORPTION_REASON = (
    "human_approved_exact_unmatched_doprinato_already_absorbed_by_official_wb_stock"
)
UNMATCHED_DOPRINATO_ABSORPTION_APPROVAL_DATE = "2026-07-13"
UNMATCHED_DOPRINATO_DIAGNOSTIC_FINGERPRINT = (
    "d03d574bec2ac1d0133736c5d9b5d3441f11fba0d4fb7d182645e711df10dfb5"
)
UNMATCHED_DOPRINATO_ABSORPTION_POLICY_V2 = (
    "CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V2"
)
UNMATCHED_DOPRINATO_ABSORPTION_REASON_V2 = (
    "human_approved_exact_remaining_unmatched_doprinato_already_absorbed_by_official_wb_stock"
)
UNMATCHED_DOPRINATO_ABSORPTION_APPROVAL_DATE_V2 = "2026-07-13"
UNMATCHED_DOPRINATO_DIAGNOSTIC_FINGERPRINT_V2 = (
    "99eca22fa972f0207b60cd4fb699b608d637ca4fcee41ca7fd273aa93863c2ec"
)
POSTCUTOVER_NORMALIZATION_MANIFEST: dict[str, dict[str, Any]] = {
    "ffso_14303efbdb04425baf54": {
        "operation_id": "ffso_14303efbdb04425baf54",
        "supply_id": "40436428",
        "source_key": "wb_supply_debit:supply:40436428",
        "business_date": "2026-07-03",
        "line_set_fingerprint": "sha256:8e721d589c7cc311901ba0aeee947978db56b4e458f2017a97ccd4e5edd51a6b",
        "accepted_line_set_fingerprint": "sha256:953d5243e63c4113aa4ab9f7a60bba0dfe1e5f1a5c60f68fa8056d2422f82d01",
        "evidence_fingerprint": "sha256:4b10ae816fc8a5022c53c71d5d4b635417d466b8b62841122ae7b2a030d56c7c",
    },
    "ffso_786f3d2533374015af12": {
        "operation_id": "ffso_786f3d2533374015af12",
        "supply_id": "40422317",
        "source_key": "wb_supply_debit:supply:40422317",
        "business_date": "2026-07-02",
        "line_set_fingerprint": "sha256:8c54f0550ff712b15a1600ee9117716d99f0b93d8b4b96b5f45f5c1026d85251",
        "accepted_line_set_fingerprint": "sha256:c2c963286fa1b585cee8c9261072facd893343d96ad05d847dd36eb7c0a5f739",
        "evidence_fingerprint": "sha256:cc785f5d378e1ba4bc2eb6d54900657c1727a471ab6fa014651e4d821f6de3c5",
    },
    "ffso_9c618c5b5e0d4957b7cf": {
        "operation_id": "ffso_9c618c5b5e0d4957b7cf",
        "supply_id": "40564048",
        "source_key": "wb_supply_debit:supply:40564048",
        "business_date": "2026-07-06",
        "line_set_fingerprint": "sha256:ec5d398d7f2e1b2555e675aeb8725a80558fc3777c15279bc6328ed98603bd9c",
        "accepted_line_set_fingerprint": "sha256:a49a76cb642f259573c117b65246d93a97cb9f30ab1bb32790c8de417ba7193e",
        "evidence_fingerprint": "sha256:6385b7004828d1afcc0a13fb38e517d6d5f72ac79afb74e697aaa821dee4f554",
    },
    "ffso_ceec1569093b40aa80d7": {
        "operation_id": "ffso_ceec1569093b40aa80d7",
        "supply_id": "40559839",
        "source_key": "wb_supply_debit:supply:40559839",
        "business_date": "2026-07-06",
        "line_set_fingerprint": "sha256:320d56dd3c4d13553dbc61e667f321b53af5d732719339e05f684def1fb8a50b",
        "accepted_line_set_fingerprint": "sha256:7add90d1d03f7f7e4048ebeac583aeb3a3febe16e26971a56e0e502f9a36a416",
        "evidence_fingerprint": "sha256:37f7d2ebd9a9c7097ac7f3aa568a0ea79ce3df836f43ffb5c2bb2293f426171e",
    },
}

# Exact source-evidence absorptions approved for the one-time canonical
# cutover.  These rows never become movement/capital events and are checked
# before direct/FIFO reconciliation.  Persisted route strings are deliberately
# pinned in their full source form rather than shortened UI labels.
UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST: dict[str, dict[str, Any]] = {
    "40517726": {
        "supply_id": "40517726",
        "business_date": "2026-07-01",
        "nm_id": 259466031,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "1",
        "source_identity": "supply:40517726",
        "raw_row_line_fingerprint": "sha256:b1863fe7bcd5d98d1320f8d56c4ff30ab9d3097e1324199ac66c1e7b99edd29c",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_FF,
        "recognized_reference_unit_cost_rub": "100.146048",
        "paid_reference_unit_cost_rub": "100.146048",
    },
    "40610543": {
        "supply_id": "40610543",
        "business_date": "2026-07-05",
        "nm_id": 210183919,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "1",
        "source_identity": "supply:40610543",
        "raw_row_line_fingerprint": "sha256:6028fd543f341d8dc5f231e1ef06b95ed266af788aea6cbd893a3455898ab60a",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_FF,
        "recognized_reference_unit_cost_rub": "93.547548",
        "paid_reference_unit_cost_rub": "93.547548",
    },
    "40654176": {
        "supply_id": "40654176",
        "business_date": "2026-07-07",
        "nm_id": 391663632,
        "warehouse": "Склад Шушары",
        "destination": "Склад Шушары",
        "quantity": "1",
        "source_identity": "supply:40654176",
        "raw_row_line_fingerprint": "sha256:e77faf4615529a4b999f46796338acdcf9bee9197421428f9afd598faf79a941",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_FF,
        "recognized_reference_unit_cost_rub": "119.941548",
        "paid_reference_unit_cost_rub": "119.941548",
    },
    "40712116": {
        "supply_id": "40712116",
        "business_date": "2026-07-09",
        "nm_id": 497417474,
        "warehouse": "Краснодар (Тихорецкая)",
        "destination": "Краснодар (Тихорецкая)",
        "quantity": "1",
        "source_identity": "supply:40712116",
        "raw_row_line_fingerprint": "sha256:2f7f8043d20035b655c7a5e9ee7c40d267876119b4ee7ab61722c99769f04f7f",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "114.442798",
        "paid_reference_unit_cost_rub": "114.442798",
    },
    "40739431": {
        "supply_id": "40739431",
        "business_date": "2026-07-10",
        "nm_id": 391659990,
        "warehouse": "Екатеринбург - Перспективная 14",
        "destination": "Екатеринбург - Перспективная 14",
        "quantity": "1",
        "source_identity": "supply:40739431",
        "raw_row_line_fingerprint": "sha256:c2715065c5d72657083e01f10217b0cf24f2cd0ea12865f6f3913e5b8929b4fd",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_PRODUCTION,
        "recognized_reference_unit_cost_rub": "119.941548",
        "paid_reference_unit_cost_rub": "80.358877",
    },
    "40739432": {
        "supply_id": "40739432",
        "business_date": "2026-07-10",
        "nm_id": 210183142,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "1",
        "source_identity": "supply:40739432",
        "raw_row_line_fingerprint": "sha256:88430e4065c9818b8ae5ad9223d1d4c0ff8c70d085a1e1a06e3189bbcfa079e0",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_PRODUCTION,
        "recognized_reference_unit_cost_rub": "93.547548",
        "paid_reference_unit_cost_rub": "54.304632",
    },
    "40765457": {
        "supply_id": "40765457",
        "business_date": "2026-07-11",
        "nm_id": 210183142,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "1",
        "source_identity": "supply:40765457",
        "raw_row_line_fingerprint": "sha256:b9d25d11d4154b47c1a08c8f1f1559cb9db85d183d351999ae01d21441188026",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_PRODUCTION,
        "recognized_reference_unit_cost_rub": "93.547548",
        "paid_reference_unit_cost_rub": "54.304632",
    },
    "40765458": {
        "supply_id": "40765458",
        "business_date": "2026-07-11",
        "nm_id": 391662410,
        "warehouse": "Екатеринбург - Перспективная 14",
        "destination": "Екатеринбург - Перспективная 14",
        "quantity": "2",
        "source_identity": "supply:40765458",
        "raw_row_line_fingerprint": "sha256:020b1b7f159cc34325c9086ad552b5dbc3715851760c4a829d3fe039291f59a3",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_PRODUCTION,
        "recognized_reference_unit_cost_rub": "116.642298",
        "paid_reference_unit_cost_rub": "76.863799",
    },
    "40778404": {
        "supply_id": "40778404",
        "business_date": "2026-07-12",
        "nm_id": 391659990,
        "warehouse": "Екатеринбург - Перспективная 14",
        "destination": "Екатеринбург - Перспективная 14",
        "quantity": "1",
        "source_identity": "supply:40778404",
        "raw_row_line_fingerprint": "sha256:13060d8db62c9470bf1e8fe88f035cb84759b381c34312bb26c83050ac59d36d",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_PRODUCTION,
        "recognized_reference_unit_cost_rub": "119.941548",
        "paid_reference_unit_cost_rub": "80.358877",
    },
    "40778405": {
        "supply_id": "40778405",
        "business_date": "2026-07-12",
        "nm_id": 259466031,
        "warehouse": "Склад Шушары",
        "destination": "Склад Шушары",
        "quantity": "1",
        "source_identity": "supply:40778405",
        "raw_row_line_fingerprint": "sha256:727b16abec9c3eab45c658a3b45bdc55e12d9b17dde79acc8f3414a3e313d808",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_FF,
        "recognized_reference_unit_cost_rub": "100.146048",
        "paid_reference_unit_cost_rub": "100.146048",
    },
}

# V1 above is immutable.  V2 is a distinct approval and is keyed by the exact
# supply/SKU identity because multiple approved source lines can share one
# persisted doprinato supply.  It has no wildcard or quantity tolerance.
UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2: dict[
    tuple[str, int], dict[str, Any]
] = {
    ("40610543", 391662410): {
        "supply_id": "40610543",
        "business_date": "2026-07-05",
        "nm_id": 391662410,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "2",
        "source_identity": "supply:40610543",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:9607cc6234a5b7866cbda1b0021bc9e7e4f315d8d41f738e10c4b883ec9d6c8b",
        "raw_source_line_fingerprint": "sha256:32b72a4189df276bf791072d15ad8135af35a941bf66b259d2319bbae705f302",
        "raw_row_line_fingerprint": "sha256:2b87137767b15d00203f39be531df3fce8ceee7ab5bb6942040edea05f41fada",
        "semantic_evidence_fingerprint": "sha256:c19db9557ae51c93ae164f51ee8d5d299e498fcaf3ab605707d658de1af914f5",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "116.642298",
        "paid_reference_unit_cost_rub": "116.642298",
    },
    ("40610543", 428855560): {
        "supply_id": "40610543",
        "business_date": "2026-07-05",
        "nm_id": 428855560,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "1",
        "source_identity": "supply:40610543",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:9607cc6234a5b7866cbda1b0021bc9e7e4f315d8d41f738e10c4b883ec9d6c8b",
        "raw_source_line_fingerprint": "sha256:15668716ec327a12123aceedf541d7557d6f05a824e48d259e36574f6a26dd5b",
        "raw_row_line_fingerprint": "sha256:fb356bdd64ccff27b85975b99b75db5e29cd9e7c37aa3c22a1c1768717779fa1",
        "semantic_evidence_fingerprint": "sha256:0ed7fc34f2ec995432f4f00b4c49a544c046726c840242ab8195e33b567bf905",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "111.143548",
        "paid_reference_unit_cost_rub": "111.143548",
    },
    ("40654176", 391660889): {
        "supply_id": "40654176",
        "business_date": "2026-07-07",
        "nm_id": 391660889,
        "warehouse": "Склад Шушары",
        "destination": "Склад Шушары",
        "quantity": "1",
        "source_identity": "supply:40654176",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:42b9584a0fdf0fa2632eeeecb0f8c8c507d9459b5f5f881fb33df59464e1146e",
        "raw_source_line_fingerprint": "sha256:31476c4814c960fb0325c0e90528dc949dd444438a4920589bb1e188dfc89e36",
        "raw_row_line_fingerprint": "sha256:b746f54b3b9308320e9fe0f9a0b3d963d99e824a24d497e577c7f3a0b74a877e",
        "semantic_evidence_fingerprint": "sha256:41d8d5b23c561122507bd3253b5809761660456fb890d434f223e69a1ad6bbed",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "123.240798",
        "paid_reference_unit_cost_rub": "123.240798",
    },
    ("40739431", 391662965): {
        "supply_id": "40739431",
        "business_date": "2026-07-10",
        "nm_id": 391662965,
        "warehouse": "Екатеринбург - Перспективная 14",
        "destination": "Екатеринбург - Перспективная 14",
        "quantity": "1",
        "source_identity": "supply:40739431",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:1a316aa794752e4e7aabb47acdaa68436e8fd9efd4685126efd4046c19990734",
        "raw_source_line_fingerprint": "sha256:855f92f1b24ad464b82f9c045e761efcebc1f0e4a363b5d2c03c1c611b560208",
        "raw_row_line_fingerprint": "sha256:013ae02c344ab95be24536684e7c9f4a35bcea689eed6ffcaf8ad5309944f73b",
        "semantic_evidence_fingerprint": "sha256:6d53be9b1c069839d4c5934d004231fec7e4773102e223ebc394b4d8e4303325",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "119.941548",
        "paid_reference_unit_cost_rub": "119.941548",
    },
    ("40765457", 391661710): {
        "supply_id": "40765457",
        "business_date": "2026-07-11",
        "nm_id": 391661710,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "1",
        "source_identity": "supply:40765457",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:5a3c2002d5fac188e34da8cd6e72511e5d1e8971ea35f4e5c8adcaf1c3ae1e5e",
        "raw_source_line_fingerprint": "sha256:d5bc78fb3fb0b83ccd881add4181450fd3f8aac724085b030d8a372628a7d0cc",
        "raw_row_line_fingerprint": "sha256:70fe1e98a3d7b52dcf8f5b1607c1f9615bfa534c570c2b211cf8cf223b4138e2",
        "semantic_evidence_fingerprint": "sha256:54dcddd1ee9ddbb2a22fee2cf782380eaa5d38c4123a52f5cb0546c037075d93",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_FF,
        "recognized_reference_unit_cost_rub": "123.240798",
        "paid_reference_unit_cost_rub": "123.240798",
    },
    ("40765457", 391662410): {
        "supply_id": "40765457",
        "business_date": "2026-07-11",
        "nm_id": 391662410,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "1",
        "source_identity": "supply:40765457",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:5a3c2002d5fac188e34da8cd6e72511e5d1e8971ea35f4e5c8adcaf1c3ae1e5e",
        "raw_source_line_fingerprint": "sha256:b87138b129aa87b7e8825499cdbf868fbf5e33b0cbf115191948cede3e020a03",
        "raw_row_line_fingerprint": "sha256:c14e8c3b20911e9a0eddbdfb06d1eb68a82742d0bba50c73e9f8710aac90f739",
        "semantic_evidence_fingerprint": "sha256:f277d64e85a14b3ee706ae9b3c7ea1341000c43e65196a268b4f5bb888c47cb9",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "116.642298",
        "paid_reference_unit_cost_rub": "116.642298",
    },
    ("40765457", 428854140): {
        "supply_id": "40765457",
        "business_date": "2026-07-11",
        "nm_id": 428854140,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "3",
        "source_identity": "supply:40765457",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:5a3c2002d5fac188e34da8cd6e72511e5d1e8971ea35f4e5c8adcaf1c3ae1e5e",
        "raw_source_line_fingerprint": "sha256:7d63b9e4f541081ccad25477a959d7c5613682f5f2f731d528274daac347755d",
        "raw_row_line_fingerprint": "sha256:05929e4ed0b956599c0f4b43cef6294a44ff1de7933ef6d89ef317560fc6c58d",
        "semantic_evidence_fingerprint": "sha256:e8e18fbb60a55fe188f3626c4a6f7afa5d3f3647cbf65af7b60cbc9598ca72fa",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "114.442798",
        "paid_reference_unit_cost_rub": "114.442798",
    },
    ("40765457", 428854299): {
        "supply_id": "40765457",
        "business_date": "2026-07-11",
        "nm_id": 428854299,
        "warehouse": "Электросталь",
        "destination": "Электросталь",
        "quantity": "1",
        "source_identity": "supply:40765457",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:5a3c2002d5fac188e34da8cd6e72511e5d1e8971ea35f4e5c8adcaf1c3ae1e5e",
        "raw_source_line_fingerprint": "sha256:a78b211cbea97323fb55455e50a75f8e3b4ba167175c1a05309a75b70563f3f5",
        "raw_row_line_fingerprint": "sha256:e6ed64bc90a7402796b92c299e0b6bdbd525f30b52977fa578018402098689c2",
        "semantic_evidence_fingerprint": "sha256:ee245add766c119f42f796d27790ac2d422733afb7874fb91d3954bad9df8b59",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "114.442798",
        "paid_reference_unit_cost_rub": "114.442798",
    },
    ("40765458", 497414624): {
        "supply_id": "40765458",
        "business_date": "2026-07-11",
        "nm_id": 497414624,
        "warehouse": "Екатеринбург - Перспективная 14",
        "destination": "Екатеринбург - Перспективная 14",
        "quantity": "1",
        "source_identity": "supply:40765458",
        "original_supply_id": "",
        "raw_source_row_fingerprint": "sha256:68d7558524b2181e946d105c4cdb88edaf108167b42008c92eab0454e9b9e7e4",
        "raw_source_line_fingerprint": "sha256:bd4f37de2c9e9d8cf1991f280473521e17d74dc1f39c8177b3eafcf0130e5aef",
        "raw_row_line_fingerprint": "sha256:5e361c407f7c3d6520bcb6cfa8d8a440211a7663571ff7bbaabcede83a732f3a",
        "semantic_evidence_fingerprint": "sha256:f96a323ba42bbabdf779811ef7fd73017fd775c7b2eef15eb59391f438819847",
        "status": "final-accepted",
        "cost_reference_stage": STAGE_WB,
        "recognized_reference_unit_cost_rub": "100.146048",
        "paid_reference_unit_cost_rub": "100.146048",
    },
}


class CanonicalCostBlocked(ValueError):
    """A fail-closed source, cost-coverage or reconciliation failure."""

    def __init__(self, code: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(f"{code}: {_json_dumps(self.details)}")


@dataclass(frozen=True)
class CanonicalRebuildResult:
    cutover_date: str
    date_from: str
    date_to: str
    baseline_fingerprint: str
    component_rows_changed: int
    movement_rows_changed: int
    outstanding_rows_changed: int
    daily_rows_changed: int
    invalidated_from: str | None
    fingerprint: str


@dataclass(frozen=True)
class FfOperationDateResolution:
    """Deterministic business date plus immutable source provenance."""

    effective_date: str
    provenance: dict[str, Any]


class CanonicalCostEngine:
    """Build both paid-capital and recognized-cost views from one source graph."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _now
        # Diagnostic collectors may quarantine one exact source line in
        # memory so another blocker in the same persisted supply is still
        # visited.  The strict runtime never populates this set and therefore
        # remains fail-closed for every non-manifest row.
        self._diagnostic_quarantined_doprinato_keys: set[
            tuple[str, int]
        ] = set()
        self.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            ensure_canonical_cost_schema(conn)

    def discover_primary_baseline_shipment(self) -> dict[str, Any]:
        """Find the one persisted fully calculated large June FF receipt."""
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            ensure_canonical_cost_schema(conn)
            candidates = conn.execute(
                """
                SELECT shipment.shipment_id, shipment.actual_ff_acceptance_date,
                       shipment.product_qty_total, shipment.match_status,
                       shipment.expenses_complete, layer.layer_id,
                       layer.status AS layer_status, layer.product_qty_total AS layer_qty,
                       layer.weighted_avg_ff_unit_cost_rub, layer.reconciliation_status,
                       layer.inputs_hash
                FROM sheet_vitrina_v1_supplier_shipments AS shipment
                JOIN sheet_vitrina_v1_supplier_ff_cost_layers AS layer
                  ON layer.supplier_shipment_id = shipment.shipment_id
                 AND layer.is_current = 1
                WHERE shipment.order_status = 'accepted_ff'
                  AND shipment.actual_ff_acceptance_date BETWEEN ? AND ?
                  AND COALESCE(shipment.product_qty_total, 0) >= ?
                ORDER BY shipment.product_qty_total DESC, shipment.shipment_id
                """,
                (
                    PRIMARY_ACCEPTED_DATE_FROM,
                    PRIMARY_ACCEPTED_DATE_TO,
                    float(PRIMARY_MIN_QUANTITY),
                ),
            ).fetchall()
            valid: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for row in candidates:
                shipment_id = str(row["shipment_id"])
                line_counts = conn.execute(
                    """
                    SELECT COUNT(*) AS product_count,
                           SUM(CASE WHEN internal_nm_id IS NOT NULL
                                         AND match_status IN ('matched','matched_by_barcode','matched_by_compatibility')
                                    THEN 1 ELSE 0 END) AS matched_count,
                           COUNT(DISTINCT internal_nm_id) AS sku_count
                    FROM sheet_vitrina_v1_supplier_shipment_lines
                    WHERE shipment_id = ? AND line_type = 'product'
                    """,
                    (shipment_id,),
                ).fetchone()
                ff_line_counts = conn.execute(
                    """
                    SELECT COUNT(*) line_count,
                           SUM(CASE WHEN nm_id IS NOT NULL AND sku_ff_unit_cost_rub>0
                                         AND source_status='confirmed'
                                    THEN 1 ELSE 0 END) confirmed_count
                    FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines
                    WHERE layer_id=?
                    """,
                    (str(row["layer_id"]),),
                ).fetchone()
                avg = _decimal(row["weighted_avg_ff_unit_cost_rub"])
                reasons: list[str] = []
                if int(row["expenses_complete"] or 0) != 1:
                    reasons.append("expenses_not_certified")
                if str(row["layer_status"] or "") != "confirmed":
                    reasons.append("ff_layer_not_confirmed")
                if str(row["reconciliation_status"] or "") != "ok":
                    reasons.append("ff_layer_reconciliation_not_ok")
                if int(line_counts["product_count"] or 0) == 0:
                    reasons.append("no_product_lines")
                if int(line_counts["matched_count"] or 0) != int(line_counts["product_count"] or 0):
                    reasons.append("sku_matching_incomplete")
                if int(ff_line_counts["line_count"] or 0) != int(line_counts["product_count"] or 0):
                    reasons.append("ff_cost_line_count_mismatch")
                if int(ff_line_counts["confirmed_count"] or 0) != int(ff_line_counts["line_count"] or 0):
                    reasons.append("ff_cost_lines_not_fully_confirmed")
                if avg <= ZERO or abs(avg - EXPECTED_PRIMARY_AVG_RUB) > PRIMARY_AVG_TOLERANCE_RUB:
                    reasons.append("weighted_average_outside_expected_tolerance")
                item = {
                    "shipment_id": shipment_id,
                    "accepted_ff_date": str(row["actual_ff_acceptance_date"]),
                    "quantity": _text(_decimal(row["product_qty_total"])),
                    "sku_count": int(line_counts["sku_count"] or 0),
                    "product_line_count": int(line_counts["product_count"] or 0),
                    "ff_cost_layer_id": str(row["layer_id"]),
                    "weighted_ff_unit_cost_rub": _text(avg),
                    "ff_cost_inputs_hash": str(row["inputs_hash"]),
                }
                if reasons:
                    rejected.append({**item, "reasons": reasons})
                else:
                    valid.append(item)
        if len(valid) != 1:
            raise CanonicalCostBlocked(
                "primary_baseline_shipment_not_unique",
                {"valid": valid, "rejected": rejected},
            )
        return {**valid[0], "rejected_candidate_count": len(rejected)}

    def build_baseline_plan(
        self,
        *,
        cutover_date: str = CUTOVER_DATE,
        diagnostic: bool = False,
    ) -> dict[str, Any]:
        if cutover_date != CUTOVER_DATE:
            raise CanonicalCostBlocked("unsupported_cutover_date", {"cutover_date": cutover_date})
        primary = self.discover_primary_baseline_shipment()
        physical = self.physical_quantities_as_of(
            cutover_date, enforce_source_preflight=not diagnostic
        )
        owned_nm_ids = sorted(
            nm_id
            for nm_id, stages in physical.items()
            if sum((stages.get(stage, ZERO) for stage in STAGES), ZERO) > ZERO
        )
        with _connect(self.runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            primary_rows = conn.execute(
                """
                SELECT line.nm_id, line.sku, line.display_name, line.sku_ff_unit_cost_rub,
                       line.layer_line_id, line.source_status
                FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines AS line
                WHERE line.layer_id = ? AND line.nm_id IS NOT NULL
                ORDER BY line.nm_id
                """,
                (primary["ff_cost_layer_id"],),
            ).fetchall()
            primary_by_nm = {int(row["nm_id"]): dict(row) for row in primary_rows}
        fallbacks = self._nearest_onec_ff_fallbacks(
            nm_ids=[
                nm for nm in owned_nm_ids
                if nm not in primary_by_nm
                and nm not in BUSINESS_APPROVED_PRIMARY_WAC_NM_IDS
            ]
        )
        business_approved_fallbacks = {
            nm_id: {
                "nm_id": nm_id,
                "unit_cost_rub": primary["weighted_ff_unit_cost_rub"],
                "source_type": BASELINE_BUSINESS_APPROVED_PRIMARY_WAC,
                "source_identity": primary["shipment_id"],
                "source_date": BUSINESS_APPROVED_PRIMARY_WAC_DECISION_DATE,
                "provenance": {
                    "approved_nm_ids": sorted(BUSINESS_APPROVED_PRIMARY_WAC_NM_IDS),
                    "nm_id": nm_id,
                    "primary_shipment_id": primary["shipment_id"],
                    "ff_cost_layer_id": primary["ff_cost_layer_id"],
                    "primary_weighted_ff_unit_cost_rub": primary["weighted_ff_unit_cost_rub"],
                    "business_decision_date": BUSINESS_APPROVED_PRIMARY_WAC_DECISION_DATE,
                    "method": BUSINESS_APPROVED_PRIMARY_WAC_METHOD,
                    "reason": BUSINESS_APPROVED_PRIMARY_WAC_REASON,
                },
            }
            for nm_id in owned_nm_ids
            if nm_id in BUSINESS_APPROVED_PRIMARY_WAC_NM_IDS
            and nm_id not in primary_by_nm
            and nm_id not in fallbacks
        }
        supplier_paid = self._supplier_payment_projection_as_of(cutover_date)
        missing = [
            nm for nm in owned_nm_ids
            if nm not in primary_by_nm
            and nm not in fallbacks
            and nm not in business_approved_fallbacks
        ]
        conflicting = [
            nm for nm, row in primary_by_nm.items()
            if nm in owned_nm_ids and _decimal(row.get("sku_ff_unit_cost_rub")) <= ZERO
        ]
        if missing or conflicting:
            covered_nm_ids = (
                set(primary_by_nm) | set(fallbacks) | set(business_approved_fallbacks)
            )
            total_quantity = sum(
                (
                    sum((stages.get(stage, ZERO) for stage in STAGES), ZERO)
                    for stages in physical.values()
                ),
                ZERO,
            )
            covered_quantity = sum(
                (
                    sum((physical[nm_id].get(stage, ZERO) for stage in STAGES), ZERO)
                    for nm_id in owned_nm_ids if nm_id in covered_nm_ids
                ),
                ZERO,
            )
            raise CanonicalCostBlocked(
                "baseline_cost_coverage_incomplete",
                {
                    "cutover_date": cutover_date,
                    "primary_shipment": primary,
                    "primary_sku_count": len(set(primary_by_nm) & set(owned_nm_ids)),
                    "primary_shipment_sku_count": len(primary_by_nm),
                    "fallbacks": [fallbacks[nm] for nm in sorted(fallbacks)],
                    "fallback_sku_count": len(fallbacks),
                    "business_approved_fallbacks": [
                        business_approved_fallbacks[nm]
                        for nm in sorted(business_approved_fallbacks)
                    ],
                    "business_approved_sku_count": len(business_approved_fallbacks),
                    "missing_nm_ids": missing,
                    "missing_sku_count": len(missing),
                    "conflicting_nm_ids": conflicting,
                    "physical": _json_safe_physical(physical),
                    "stage_physical_quantities": {
                        stage: _text(sum(
                            (stages.get(stage, ZERO) for stages in physical.values()), ZERO
                        ))
                        for stage in STAGES
                    },
                    "physical_quantity": _text(total_quantity),
                    "cost_covered_quantity": _text(covered_quantity),
                    "cost_coverage": _text(_safe_ratio(covered_quantity, total_quantity)),
                },
            )
        lines: list[dict[str, Any]] = []
        for nm_id in owned_nm_ids:
            stages = physical[nm_id]
            if nm_id in primary_by_nm:
                source = primary_by_nm[nm_id]
                unit_cost = _decimal(source["sku_ff_unit_cost_rub"])
                source_type = BASELINE_PRIMARY
                source_identity = primary["shipment_id"]
                source_date = primary["accepted_ff_date"]
                provenance = {
                    "shipment_id": primary["shipment_id"],
                    "ff_cost_layer_id": primary["ff_cost_layer_id"],
                    "ff_cost_layer_line_id": str(source["layer_line_id"]),
                }
                confirmation = ONE
            elif nm_id in fallbacks:
                source = fallbacks[nm_id]
                unit_cost = _decimal(source["unit_cost_rub"])
                source_type = BASELINE_ONEC
                source_identity = str(source["bundle_version"])
                source_date = str(source["as_of_date"])
                provenance = dict(source)
                confirmation = ZERO
            else:
                source = business_approved_fallbacks[nm_id]
                unit_cost = _decimal(source["unit_cost_rub"])
                source_type = BASELINE_BUSINESS_APPROVED_PRIMARY_WAC
                source_identity = str(source["source_identity"])
                source_date = str(source["source_date"])
                provenance = dict(source["provenance"])
                confirmation = ZERO
            if source_date > ONEC_FALLBACK_LAST_DATE and source_type == BASELINE_ONEC:
                raise CanonicalCostBlocked(
                    "onec_fallback_after_cutoff",
                    {"nm_id": nm_id, "source_date": source_date},
                )
            if unit_cost <= ZERO:
                raise CanonicalCostBlocked("baseline_zero_cost_forbidden", {"nm_id": nm_id})
            for stage in STAGES:
                qty = stages.get(stage, ZERO)
                if qty <= ZERO:
                    continue
                paid_equivalent = qty
                paid_capital = qty * unit_cost
                paid_unit = unit_cost
                if stage in {STAGE_PRODUCTION, STAGE_PRODUCTION_TO_FF}:
                    payment = supplier_paid.get((nm_id, stage), {})
                    paid_equivalent = min(
                        _decimal(payment.get("paid_equivalent_quantity")), qty
                    )
                    paid_capital = _decimal(payment.get("paid_capital_rub"))
                    paid_unit = (
                        _safe_ratio(paid_capital, paid_equivalent)
                        if paid_equivalent > ZERO else ZERO
                    )
                lines.append(
                    {
                        "nm_id": nm_id,
                        "stage": stage,
                        "physical_quantity": _text(qty),
                        "paid_equivalent_quantity": _text(paid_equivalent),
                        "recognized_unit_cost_rub": _text(unit_cost),
                        "paid_unit_cost_rub": _text(paid_unit),
                        "recognized_capital_rub": _text(qty * unit_cost),
                        "paid_capital_rub": _text(paid_capital),
                        "cost_covered_quantity": _text(qty),
                        "confirmed_quantity": _text(qty * confirmation),
                        "source_type": source_type,
                        "source_identity": source_identity,
                        "source_date": source_date,
                        "provenance": provenance,
                    }
                )
        quantity = sum((_decimal(item["physical_quantity"]) for item in lines), ZERO)
        covered = sum((_decimal(item["cost_covered_quantity"]) for item in lines), ZERO)
        if quantity > ZERO and covered != quantity:
            raise CanonicalCostBlocked(
                "baseline_cost_coverage_not_100_pct",
                {"quantity": _text(quantity), "covered": _text(covered)},
            )
        stage_summary: dict[str, dict[str, str | None]] = {}
        for stage in STAGES:
            stage_lines = [item for item in lines if item["stage"] == stage]
            stage_qty = sum((_decimal(item["physical_quantity"]) for item in stage_lines), ZERO)
            stage_paid_equivalent = sum(
                (_decimal(item["paid_equivalent_quantity"]) for item in stage_lines), ZERO
            )
            stage_recognized = sum((_decimal(item["recognized_capital_rub"]) for item in stage_lines), ZERO)
            stage_paid = sum((_decimal(item["paid_capital_rub"]) for item in stage_lines), ZERO)
            stage_covered = sum((_decimal(item["cost_covered_quantity"]) for item in stage_lines), ZERO)
            stage_confirmed = sum((_decimal(item["confirmed_quantity"]) for item in stage_lines), ZERO)
            stage_summary[stage] = {
                "physical_quantity": _text(stage_qty),
                "paid_equivalent_quantity": _text(stage_paid_equivalent),
                "recognized_capital_rub": _text(stage_recognized),
                "paid_capital_rub": _text(stage_paid),
                "recognized_unit_cost_rub": _text(_safe_ratio(stage_recognized, stage_qty)) if stage_qty > ZERO else None,
                "paid_unit_cost_rub": (
                    _text(_safe_ratio(stage_paid, stage_paid_equivalent))
                    if stage_paid_equivalent > ZERO else None
                ),
                "cost_coverage": _text(_safe_ratio(stage_covered, stage_qty)) if stage_qty > ZERO else None,
                "confirmation_share": _text(_safe_ratio(stage_confirmed, stage_qty)) if stage_qty > ZERO else None,
            }
        payload = {
            "contract": "canonical_cost_baseline_v1",
            "cutover_date": cutover_date,
            "primary_shipment": primary,
            "primary_sku_ids": sorted(primary_by_nm),
            "primary_used_sku_ids": sorted(set(primary_by_nm) & set(owned_nm_ids)),
            "fallbacks": [fallbacks[nm] for nm in sorted(fallbacks)],
            "business_approved_fallbacks": [
                business_approved_fallbacks[nm]
                for nm in sorted(business_approved_fallbacks)
            ],
            "missing_nm_ids": missing,
            "conflicting_nm_ids": conflicting,
            "physical": _json_safe_physical(physical),
            "stage_summary": stage_summary,
            "lines": lines,
        }
        fingerprint = _stable_hash(payload)
        return {
            **payload,
            "primary_sku_count": len(set(primary_by_nm) & set(owned_nm_ids)),
            "primary_shipment_sku_count": len(primary_by_nm),
            "fallback_sku_count": len(fallbacks),
            "business_approved_sku_count": len(business_approved_fallbacks),
            "missing_sku_count": len(missing),
            "physical_quantity": _text(quantity),
            "recognized_capital_rub": _text(
                sum((_decimal(item["recognized_capital_rub"]) for item in lines), ZERO)
            ),
            "paid_capital_rub": _text(
                sum((_decimal(item["paid_capital_rub"]) for item in lines), ZERO)
            ),
            "cost_coverage": "1",
            "fingerprint": fingerprint,
        }

    def materialize_baseline_plan(self, plan: Mapping[str, Any]) -> str:
        fingerprint = str(plan.get("fingerprint") or "")
        if not fingerprint or fingerprint != _stable_hash(
            {key: value for key, value in plan.items() if key not in {
                "fingerprint", "primary_sku_count", "primary_shipment_sku_count", "fallback_sku_count",
                "business_approved_sku_count",
                "missing_sku_count", "physical_quantity", "recognized_capital_rub",
                "paid_capital_rub", "cost_coverage",
            }}
        ):
            raise CanonicalCostBlocked("baseline_fingerprint_invalid")
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            ensure_canonical_cost_schema(conn)
            existing = conn.execute(
                "SELECT fingerprint FROM sheet_vitrina_v1_canonical_cost_baseline_versions WHERE is_current=1"
            ).fetchone()
            if existing is not None and str(existing["fingerprint"]) == fingerprint:
                return fingerprint
            conn.execute(
                "UPDATE sheet_vitrina_v1_canonical_cost_baseline_versions SET is_current=0, superseded_at=? WHERE is_current=1",
                (now,),
            )
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM sheet_vitrina_v1_canonical_cost_baseline_versions"
            ).fetchone()[0])
            baseline_id = f"canonical_baseline_{version}_{fingerprint[:12]}"
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions(
                    baseline_id, version, cutover_date, primary_shipment_id,
                    primary_accepted_ff_date, primary_quantity, primary_sku_count,
                    weighted_ff_unit_cost_rub, fallback_sku_count,
                    business_approved_sku_count, fingerprint,
                    report_json, is_current, created_at, superseded_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,NULL)
                """,
                (
                    baseline_id, version, plan["cutover_date"],
                    plan["primary_shipment"]["shipment_id"],
                    plan["primary_shipment"]["accepted_ff_date"],
                    plan["primary_shipment"]["quantity"], plan["primary_sku_count"],
                    plan["primary_shipment"]["weighted_ff_unit_cost_rub"],
                    plan["fallback_sku_count"], plan["business_approved_sku_count"],
                    fingerprint, _json_dumps(plan), now,
                ),
            )
            for item in plan["lines"]:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_lines(
                        baseline_id,nm_id,stage,physical_quantity,paid_equivalent_quantity,
                        recognized_unit_cost_rub,paid_unit_cost_rub,
                        recognized_capital_rub,paid_capital_rub,cost_covered_quantity,
                        confirmed_quantity,source_type,source_identity,source_date,
                        provenance_json,line_fingerprint
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        baseline_id, item["nm_id"], item["stage"],
                        item["physical_quantity"], item["paid_equivalent_quantity"],
                        item["recognized_unit_cost_rub"], item["paid_unit_cost_rub"],
                        item["recognized_capital_rub"], item["paid_capital_rub"],
                        item["cost_covered_quantity"], item["confirmed_quantity"],
                        item["source_type"], item["source_identity"], item["source_date"],
                        _json_dumps(item["provenance"]), _stable_hash(item),
                    ),
                )
            conn.commit()
        return fingerprint

    def current_baseline_report(self) -> dict[str, Any] | None:
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            row = conn.execute(
                "SELECT report_json FROM sheet_vitrina_v1_canonical_cost_baseline_versions WHERE is_current=1"
            ).fetchone()
        return _json_loads(row["report_json"]) if row is not None else None

    def ff_operation_date_audit(
        self, *, cutover_date: str = CUTOVER_DATE
    ) -> dict[str, Any]:
        """Audit every legacy WB writeoff missing the ordinary source timestamp."""

        cutover = _iso_date(cutover_date)
        rows: list[dict[str, Any]] = []
        with _connect(self.runtime.db_path) as conn:
            checkpoint = conn.execute(
                """
                SELECT baseline_cache_keys_json,baseline_source_keys_json,
                       baseline_supply_ids_json
                FROM sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint
                WHERE slot='current'
                """
            ).fetchone()
            checkpoint_cache_keys = set(
                _json_loads(checkpoint["baseline_cache_keys_json"])
                if checkpoint is not None else []
            )
            checkpoint_source_keys = set(
                _json_loads(checkpoint["baseline_source_keys_json"])
                if checkpoint is not None else []
            )
            checkpoint_supply_ids = set(
                _json_loads(checkpoint["baseline_supply_ids_json"])
                if checkpoint is not None else []
            )
            operations = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_ff_stock_operations
                WHERE operation_type='auto_writeoff' AND source_type='wb_supply'
                ORDER BY created_at,operation_id
                """
            ).fetchall()
            for raw_operation in operations:
                operation = dict(raw_operation)
                diagnostics = _json_loads(operation.get("diagnostics_json"))
                if str(diagnostics.get("source_timestamp") or "").strip():
                    continue
                resolution = resolve_ff_operation_effective_date(conn, operation)
                supply = _load_exact_wb_supply_for_operation(conn, operation)
                normalized = _json_loads(supply.get("normalized_row_json"))
                accepted = sum(
                    (
                        _decimal(
                            item.get("acceptedQuantity")
                            or item.get("accepted_quantity")
                            or 0
                        )
                        for item in _goods(supply.get("raw_goods_json"))
                    ),
                    ZERO,
                )
                if not _goods(supply.get("raw_goods_json")):
                    accepted = _decimal(normalized.get("accepted_quantity"))
                line_rows = conn.execute(
                    """
                    SELECT line_no,nm_id,quantity_delta
                    FROM sheet_vitrina_v1_ff_stock_operation_lines
                    WHERE operation_id=? ORDER BY line_no
                    """,
                    (str(operation.get("operation_id") or ""),),
                ).fetchall()
                line_set = sorted(
                    (
                        {
                            "line_no": int(line["line_no"] or 0),
                            "nm_id": int(line["nm_id"] or 0),
                            "quantity_delta": str(float(line["quantity_delta"] or 0)),
                        }
                        for line in line_rows
                    ),
                    key=lambda item: (
                        item["nm_id"], item["line_no"], item["quantity_delta"]
                    ),
                )
                supply_id = str(supply.get("supply_id") or "")
                cache_key = str(supply.get("cache_key") or "")
                source_key = str(operation.get("source_key") or "")
                rows.append(
                    {
                        "operation_id": str(operation.get("operation_id") or ""),
                        "supply_id": supply_id,
                        "source_key": source_key,
                        "created_at": str(operation.get("created_at") or ""),
                        "resolved_business_date": resolution.effective_date,
                        "date_provenance": resolution.provenance,
                        "checkpoint_membership": {
                            "cache_key": cache_key in checkpoint_cache_keys,
                            "source_key": source_key in checkpoint_source_keys,
                            "supply_id": supply_id in checkpoint_supply_ids,
                        },
                        "sent_quantity": _text(
                            _decimal(operation.get("total_quantity_abs"))
                        ),
                        "accepted_quantity": _text(accepted),
                        "classification": (
                            "pre_cutover"
                            if resolution.effective_date < cutover
                            else "cutover_or_post"
                        ),
                        "line_count": len(line_set),
                        "line_set_fingerprint": "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                line_set,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                    }
                )
        return {
            "source_type": "wb_supply",
            "operation_type": "auto_writeoff",
            "missing_diagnostics_source_timestamp": True,
            "operation_count": len(rows),
            "pre_cutover_count": sum(
                item["classification"] == "pre_cutover" for item in rows
            ),
            "cutover_or_post_count": sum(
                item["classification"] == "cutover_or_post" for item in rows
            ),
            "fully_checkpoint_matched_count": sum(
                all(item["checkpoint_membership"].values()) for item in rows
            ),
            "operations": rows,
        }

    def source_anomaly_preflight(
        self, *, date_to: str | None = None
    ) -> dict[str, Any]:
        """Exhaustively classify source anomalies before the candidate rebuild.

        This is intentionally read-only and non-fail-fast.  Legacy operations
        are audit-only; every new-contour exception must match the exact
        versioned post-cutover manifest and therefore cannot become an
        implicit or future clamp.
        """

        end = _iso_date(date_to or date.today().isoformat())
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            nm_ids = _wb_operation_and_acceptance_nm_ids(conn, date_to=end)
        costs = self._baseline_candidate_costs(nm_ids)
        cost_references = self._baseline_candidate_stage_cost_references(costs)
        with _connect(self.runtime.db_path) as conn:
            report = _source_anomaly_preflight_conn(
                conn,
                date_to=end,
                baseline_costs=costs,
                baseline_cost_references=cost_references,
                diagnostic_quarantined_doprinato_keys=(
                    self._diagnostic_quarantined_doprinato_keys
                ),
            )
        payload = {
            "contract_name": "canonical_cost_source_anomaly_preflight_v1",
            "policy": POSTCUTOVER_NORMALIZATION_POLICY,
            "policies": {
                "accepted_composition": POSTCUTOVER_NORMALIZATION_POLICY,
                "unmatched_doprinato_absorption": [
                    UNMATCHED_DOPRINATO_ABSORPTION_POLICY,
                    UNMATCHED_DOPRINATO_ABSORPTION_POLICY_V2,
                ],
            },
            "cutover_date": CUTOVER_DATE,
            "date_to": end,
            **report,
        }
        return {**payload, "fingerprint": _stable_hash(payload)}

    def _baseline_candidate_costs(
        self, nm_ids: Iterable[int]
    ) -> dict[int, dict[str, Any]]:
        """Resolve only permitted baseline sources without materializing rows."""

        requested = {int(nm_id) for nm_id in nm_ids if int(nm_id) > 0}
        if not requested:
            return {}
        primary = self.discover_primary_baseline_shipment()
        with _connect(self.runtime.db_path) as conn:
            primary_rows = conn.execute(
                """
                SELECT nm_id,sku_ff_unit_cost_rub,source_status,layer_line_id
                FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines
                WHERE layer_id=? AND nm_id IS NOT NULL
                """,
                (primary["ff_cost_layer_id"],),
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in primary_rows:
            nm_id = int(row["nm_id"])
            unit = _decimal(row["sku_ff_unit_cost_rub"])
            if nm_id in requested and unit > ZERO:
                result[nm_id] = {
                    "recognized_unit_cost_rub": unit,
                    "paid_unit_cost_rub": unit,
                    "confirmation_share": (
                        ONE if str(row["source_status"]) == "confirmed" else ZERO
                    ),
                    "source_type": BASELINE_PRIMARY,
                    "source_identity": str(row["layer_line_id"]),
                    "source_date": primary["accepted_ff_date"],
                }
        onec = self._nearest_onec_ff_fallbacks(
            nm_ids=requested - set(result) - BUSINESS_APPROVED_PRIMARY_WAC_NM_IDS
        )
        for nm_id, row in onec.items():
            unit = _decimal(row["unit_cost_rub"])
            if unit > ZERO:
                result[nm_id] = {
                    "recognized_unit_cost_rub": unit,
                    "paid_unit_cost_rub": unit,
                    "confirmation_share": ZERO,
                    "source_type": BASELINE_ONEC,
                    "source_identity": str(row["bundle_version"]),
                    "source_date": str(row["as_of_date"]),
                }
        primary_wac = _decimal(primary["weighted_ff_unit_cost_rub"])
        for nm_id in sorted(requested & BUSINESS_APPROVED_PRIMARY_WAC_NM_IDS):
            if nm_id not in result and primary_wac > ZERO:
                result[nm_id] = {
                    "recognized_unit_cost_rub": primary_wac,
                    "paid_unit_cost_rub": primary_wac,
                    "confirmation_share": ZERO,
                    "source_type": BASELINE_BUSINESS_APPROVED_PRIMARY_WAC,
                    "source_identity": str(primary["ff_cost_layer_id"]),
                    "source_date": BUSINESS_APPROVED_PRIMARY_WAC_DECISION_DATE,
                }
        return result

    def _baseline_candidate_stage_cost_references(
        self, baseline_costs: Mapping[int, Mapping[str, Any]]
    ) -> dict[tuple[int, str], dict[str, Any]]:
        """Build stage paid/recognized references before baseline targets exist."""

        supplier_paid = self._supplier_payment_projection_as_of(CUTOVER_DATE)
        result: dict[tuple[int, str], dict[str, Any]] = {}
        for manifest_entry in _unmatched_doprinato_manifest_entries():
            expected = manifest_entry["expected"]
            nm_id = int(expected["nm_id"])
            stage = str(expected["cost_reference_stage"])
            source = baseline_costs.get(nm_id)
            if source is None:
                continue
            recognized = _decimal(source.get("recognized_unit_cost_rub"))
            paid = _decimal(source.get("paid_unit_cost_rub"))
            payment: Mapping[str, Any] = {}
            if stage in {STAGE_PRODUCTION, STAGE_PRODUCTION_TO_FF}:
                payment = supplier_paid.get((nm_id, stage), {})
                paid_equivalent = _decimal(
                    payment.get("paid_equivalent_quantity")
                )
                paid = (
                    _safe_ratio(
                        _decimal(payment.get("paid_capital_rub")),
                        paid_equivalent,
                    )
                    if paid_equivalent > ZERO else ZERO
                )
            reference = {
                "stage": stage,
                "recognized_unit_cost_rub": _text(recognized),
                "paid_unit_cost_rub": _text(paid),
                "source_type": str(source.get("source_type") or ""),
                "source_identity": str(source.get("source_identity") or ""),
                "source_date": str(source.get("source_date") or ""),
                "payment_reference": {
                    key: _text(value) if isinstance(value, Decimal) else value
                    for key, value in payment.items()
                },
            }
            reference["line_fingerprint"] = "sha256:" + _stable_hash(reference)
            reference["baseline_fingerprint"] = "pre_materialization_source_graph"
            result[(nm_id, stage)] = reference
        return result

    def physical_quantities_as_of(
        self,
        as_of_date: str,
        *,
        enforce_source_preflight: bool = True,
    ) -> dict[int, dict[str, Decimal]]:
        as_of_date = _iso_date(as_of_date)
        anomaly_report = self.source_anomaly_preflight(date_to=as_of_date)
        if enforce_source_preflight and anomaly_report["status"] != "ok":
            raise CanonicalCostBlocked(
                "cutover_source_anomaly_preflight_blocked",
                {
                    "fingerprint": anomaly_report["fingerprint"],
                    "unresolved_anomalies": anomaly_report["unresolved_anomalies"],
                },
            )
        result: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            supplier_rows = conn.execute(
                """
                SELECT shipment.shipment_id, shipment.created_at, shipment.shipment_date,
                       shipment.actual_shipment_date, shipment.actual_ff_acceptance_date,
                       line.internal_nm_id, line.qty
                FROM sheet_vitrina_v1_supplier_shipments AS shipment
                JOIN sheet_vitrina_v1_supplier_shipment_lines AS line
                  ON line.shipment_id=shipment.shipment_id AND line.line_type='product'
                WHERE line.internal_nm_id IS NOT NULL AND COALESCE(line.qty,0)>0
                ORDER BY shipment.shipment_id,line.sort_order
                """
            ).fetchall()
            for row in supplier_rows:
                registered = min(
                    value for value in (
                        str(row["shipment_date"] or "")[:10],
                        str(row["created_at"] or "")[:10],
                    ) if value
                )
                if registered > as_of_date:
                    continue
                shipped = str(row["actual_shipment_date"] or "")[:10]
                accepted = str(row["actual_ff_acceptance_date"] or "")[:10]
                if accepted and accepted <= as_of_date:
                    continue
                stage = (
                    STAGE_PRODUCTION_TO_FF
                    if shipped and shipped <= as_of_date
                    else STAGE_PRODUCTION
                )
                result[int(row["internal_nm_id"])][stage] += _decimal(row["qty"])

            operations = _ff_operation_rows(conn)
            boundary = _ff_opening_boundary_context(conn)
            for operation in operations:
                effective = _canonical_ff_operation_effective_date(
                    conn, operation, boundary=boundary
                )
                if not effective or effective > as_of_date:
                    continue
                lines = conn.execute(
                    "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=?",
                    (operation["operation_id"],),
                ).fetchall()
                for line in lines:
                    result[int(line["nm_id"])][STAGE_FF] += _decimal(line["quantity_delta"])

            for movement in _wb_movement_evidence(
                conn, as_of_date=as_of_date, anomaly_report=anomaly_report
            ):
                result[movement["nm_id"]][STAGE_FF_TO_WB] += movement["open_quantity"]

        wb_stock = self._snapshot_metric(as_of_date, OFFICIAL_WB_STOCK_METRIC)
        for nm_id, qty in wb_stock.items():
            result[nm_id][STAGE_WB] = max(_decimal(qty), ZERO)
        for nm_id in list(result):
            for stage in STAGES:
                value = result[nm_id].get(stage, ZERO)
                if value < ZERO:
                    raise CanonicalCostBlocked(
                        "negative_physical_quantity", {"nm_id": nm_id, "stage": stage, "quantity": _text(value)}
                    )
                result[nm_id][stage] = value
        return {nm: dict(stages) for nm, stages in result.items()}

    def rebuild(
        self,
        *,
        date_from: str = CUTOVER_DATE,
        date_to: str | None = None,
    ) -> CanonicalRebuildResult:
        start = _iso_date(date_from)
        end = _iso_date(date_to or date.today().isoformat())
        if start < CUTOVER_DATE:
            raise CanonicalCostBlocked("legacy_history_is_immutable", {"date_from": start})
        if end < start:
            raise ValueError("date_to must be on or after date_from")
        baseline = self.current_baseline_report()
        if baseline is None:
            raise CanonicalCostBlocked("canonical_baseline_not_materialized")
        baseline_fingerprint = str(baseline["fingerprint"])
        component_changed, invalidated = self._materialize_components(end)
        movement_changed = self._materialize_movement_cost_layers(end)
        outstanding_changed = self._materialize_outstanding_layers(end)
        daily_changed = self._materialize_daily_state(start, end)
        fingerprint = self._projection_fingerprint(start, end)
        return CanonicalRebuildResult(
            cutover_date=CUTOVER_DATE,
            date_from=start,
            date_to=end,
            baseline_fingerprint=baseline_fingerprint,
            component_rows_changed=component_changed,
            movement_rows_changed=movement_changed,
            outstanding_rows_changed=outstanding_changed,
            daily_rows_changed=daily_changed,
            invalidated_from=invalidated,
            fingerprint=fingerprint,
        )

    def load_daily_metric_lookup(self, as_of_date: str) -> dict[int, dict[str, Any]]:
        as_of_date = _iso_date(as_of_date)
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            rows = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE as_of_date=? ORDER BY nm_id,stage",
                (as_of_date,),
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            target = result.setdefault(int(row["nm_id"]), {"stages": {}})
            target["stages"][str(row["stage"])] = dict(row)
        return result

    def status(self) -> dict[str, Any]:
        baseline = self.current_baseline_report()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            latest = conn.execute(
                """
                SELECT as_of_date,SUM(physical_quantity+0) physical_qty,
                       SUM(paid_capital_rub+0) paid_capital,
                       SUM(recognized_capital_rub+0) recognized_capital,
                       SUM(cost_covered_quantity+0) covered_qty,
                       SUM(confirmed_quantity+0) confirmed_qty
                FROM sheet_vitrina_v1_canonical_cost_daily_state
                GROUP BY as_of_date ORDER BY as_of_date DESC LIMIT 1
                """
            ).fetchone()
            outstanding = conn.execute(
                """
                SELECT SUM(open_quantity+0) qty,
                       SUM((open_quantity+0)*(cost_coverage_share+0)*(recognized_unit_cost_rub+0)) recognized,
                       SUM((paid_equivalent_quantity+0)*(paid_unit_cost_rub+0)) paid
                FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers
                WHERE is_current=1
                """
            ).fetchone()
        out_qty = _decimal(outstanding["qty"]) if outstanding else ZERO
        return {
            "contract_name": "canonical_cost_engine_v1",
            "cutover_date": CUTOVER_DATE,
            "baseline": baseline,
            "latest": dict(latest) if latest else None,
            "underaccepted_wb": {
                "quantity": float(out_qty),
                "recognized_weighted_unit_cost_rub": (
                    float(_decimal(outstanding["recognized"]) / out_qty) if out_qty > ZERO else None
                ),
                "paid_weighted_unit_cost_rub": (
                    float(_decimal(outstanding["paid"]) / out_qty) if out_qty > ZERO else None
                ),
            },
        }

    def _nearest_onec_ff_fallbacks(self, *, nm_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        missing = set(int(item) for item in nm_ids)
        result: dict[int, dict[str, Any]] = {}
        if not missing:
            return result
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT bundle_version,as_of_date,plan_json
                FROM sheet_vitrina_v1_ready_snapshots
                WHERE as_of_date <= ?
                ORDER BY as_of_date DESC,activated_at DESC,refreshed_at DESC,bundle_version DESC
                """,
                (ONEC_FALLBACK_LAST_DATE,),
            ).fetchall()
        seen_dates: set[str] = set()
        for row in rows:
            day = str(row["as_of_date"])
            # Only the newest persisted bundle for a given date participates.
            if day in seen_dates:
                continue
            seen_dates.add(day)
            try:
                snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=day)
            except Exception:
                continue
            values = _extract_snapshot_sku_metric(
                snapshot, column_date=day, metric_key=ONEC_FF_UNIT_COST_METRIC
            )
            for nm_id in sorted(missing):
                value = _decimal(values.get(nm_id))
                if value <= ZERO:
                    continue
                result[nm_id] = {
                    "nm_id": nm_id,
                    "unit_cost_rub": _text(value),
                    "as_of_date": day,
                    "bundle_version": str(row["bundle_version"]),
                    "metric_key": ONEC_FF_UNIT_COST_METRIC,
                    "source_type": BASELINE_ONEC,
                }
            missing -= set(result)
            if not missing:
                break
        return result

    def _snapshot_metric(self, as_of_date: str, metric_key: str) -> dict[int, float]:
        snapshot_date = as_of_date
        try:
            snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=snapshot_date)
        except Exception:
            candidates = self.runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
                date_to=as_of_date, descending=True
            )
            if not candidates:
                return {}
            snapshot_date = candidates[0]
            try:
                snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(
                    as_of_date=snapshot_date
                )
            except Exception:
                return {}
        return _extract_snapshot_sku_metric(
            snapshot, column_date=snapshot_date, metric_key=metric_key
        )

    def _supplier_payment_projection_as_of(
        self, as_of_date: str
    ) -> dict[tuple[int, str], dict[str, Decimal]]:
        """Allocate factual CNY payments over every matched line, never selected SKUs."""
        result: dict[tuple[int, str], dict[str, Decimal]] = {}
        with _connect(self.runtime.db_path) as conn:
            shipments = conn.execute(
                """
                SELECT shipment_id,created_at,shipment_date,actual_shipment_date,
                       actual_ff_acceptance_date,invoice_amount_total,product_amount_total
                FROM sheet_vitrina_v1_supplier_shipments
                ORDER BY shipment_id
                """
            ).fetchall()
            for shipment in shipments:
                registered = min(
                    value for value in (
                        str(shipment["shipment_date"] or "")[:10],
                        str(shipment["created_at"] or "")[:10],
                    ) if value
                )
                if registered > as_of_date:
                    continue
                accepted = str(shipment["actual_ff_acceptance_date"] or "")[:10]
                if accepted and accepted <= as_of_date:
                    continue
                shipped = str(shipment["actual_shipment_date"] or "")[:10]
                stage = (
                    STAGE_PRODUCTION_TO_FF
                    if shipped and shipped <= as_of_date else STAGE_PRODUCTION
                )
                payments = conn.execute(
                    """
                    SELECT cny_delta,rub_value_delta
                    FROM sheet_vitrina_v1_cny_ledger_operations
                    WHERE source_order_id=? AND operation_type='supplier_payment_out'
                      AND status='posted' AND operation_date<=?
                    """,
                    (shipment["shipment_id"], as_of_date),
                ).fetchall()
                paid_cny = sum(
                    (abs(_decimal(item["cny_delta"])) for item in payments), ZERO
                )
                paid_rub = sum(
                    (abs(_decimal(item["rub_value_delta"])) for item in payments), ZERO
                )
                invoice_total = _decimal(shipment["invoice_amount_total"])
                product_total = _decimal(shipment["product_amount_total"])
                paid_share = min(_safe_ratio(paid_cny, invoice_total), ONE)
                for line in conn.execute(
                    """
                    SELECT internal_nm_id,qty,amount
                    FROM sheet_vitrina_v1_supplier_shipment_lines
                    WHERE shipment_id=? AND line_type='product'
                      AND internal_nm_id IS NOT NULL AND COALESCE(qty,0)>0
                    ORDER BY sort_order
                    """,
                    (shipment["shipment_id"],),
                ).fetchall():
                    key = (int(line["internal_nm_id"]), stage)
                    bucket = result.setdefault(
                        key,
                        {"paid_equivalent_quantity": ZERO, "paid_capital_rub": ZERO},
                    )
                    bucket["paid_equivalent_quantity"] += (
                        _decimal(line["qty"]) * paid_share
                    )
                    bucket["paid_capital_rub"] += paid_rub * _safe_ratio(
                        _decimal(line["amount"]), product_total
                    )
        return result

    def _baseline_costs(self) -> dict[int, dict[str, Decimal]]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT line.nm_id,line.recognized_unit_cost_rub,line.paid_unit_cost_rub,
                       line.confirmed_quantity,line.physical_quantity
                FROM sheet_vitrina_v1_canonical_cost_baseline_lines line
                JOIN sheet_vitrina_v1_canonical_cost_baseline_versions version
                  ON version.baseline_id=line.baseline_id AND version.is_current=1
                ORDER BY line.nm_id,line.stage
                """
            ).fetchall()
        result: dict[int, dict[str, Decimal]] = {}
        for row in rows:
            nm_id = int(row["nm_id"])
            result.setdefault(
                nm_id,
                {
                    "recognized": _decimal(row["recognized_unit_cost_rub"]),
                    "paid": _decimal(row["paid_unit_cost_rub"]),
                    "confirmation": ZERO,
                },
            )
            result[nm_id]["confirmation"] = max(
                result[nm_id]["confirmation"],
                _safe_ratio(_decimal(row["confirmed_quantity"]), _decimal(row["physical_quantity"])),
            )
        return result

    def _materialize_components(self, date_to: str) -> tuple[int, str | None]:
        """Version per-SKU recognized/paid components with factual effective dates."""
        plans: list[dict[str, Any]] = []
        baseline = self._baseline_costs()
        with _connect(self.runtime.db_path) as conn:
            shipments = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_supplier_shipments
                WHERE shipment_date <= ?
                  AND (
                    shipment_date > ?
                    OR actual_ff_acceptance_date IS NULL
                    OR actual_ff_acceptance_date > ?
                  )
                ORDER BY shipment_id
                """,
                (date_to, CUTOVER_DATE, CUTOVER_DATE),
            ).fetchall()
            for shipment in shipments:
                shipment_id = str(shipment["shipment_id"])
                opening_carry = str(shipment["shipment_date"] or "")[:10] <= CUTOVER_DATE
                lines = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines
                    WHERE shipment_id=? AND line_type='product' ORDER BY sort_order
                    """,
                    (shipment_id,),
                ).fetchall()
                if not lines:
                    continue
                invoice_total = _decimal(shipment["invoice_amount_total"])
                accepted_date = str(shipment["actual_ff_acceptance_date"] or "")[:10]
                ff_costs = {
                    int(row["nm_id"]): dict(row)
                    for row in conn.execute(
                        """
                        SELECT line.* FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines line
                        JOIN sheet_vitrina_v1_supplier_ff_cost_layers layer ON layer.layer_id=line.layer_id
                        WHERE layer.supplier_shipment_id=? AND layer.is_current=1 AND line.nm_id IS NOT NULL
                        """,
                        (shipment_id,),
                    ).fetchall()
                }
                payments = conn.execute(
                    """
                    SELECT operation_id,operation_date,cny_delta,rub_value_delta,source_document_id
                    FROM sheet_vitrina_v1_cny_ledger_operations
                    WHERE source_order_id=? AND operation_type='supplier_payment_out' AND status='posted'
                    ORDER BY sequence_key,operation_id
                    """,
                    (shipment_id,),
                ).fetchall()
                payment_rub = sum((abs(_decimal(row["rub_value_delta"])) for row in payments), ZERO)
                payment_cny = sum((abs(_decimal(row["cny_delta"])) for row in payments), ZERO)
                product_value_total = sum((_decimal(line["amount"]) for line in lines), ZERO)
                product_qty_total = sum((_decimal(line["qty"]) for line in lines), ZERO)
                expenses = conn.execute(
                    """
                    SELECT expense.line_id,expense.financial_document_id,expense.category,
                           expense.amount_rub,document.document_type,document.document_date,
                           document.parse_status,document.file_sha256
                    FROM sheet_vitrina_v1_supplier_financial_expense_lines expense
                    JOIN sheet_vitrina_v1_supplier_financial_documents document
                      ON document.document_id=expense.financial_document_id
                    WHERE expense.supplier_order_id=? AND COALESCE(expense.amount_rub,0)>0
                      AND document.parse_status='confirmed'
                    ORDER BY document.document_date,document.document_id,expense.sort_order
                    """,
                    (shipment_id,),
                ).fetchall()
                for line in lines:
                    nm_id = int(line["internal_nm_id"] or 0)
                    qty = _decimal(line["qty"])
                    ff = ff_costs.get(nm_id)
                    if nm_id <= 0 or qty <= ZERO:
                        continue
                    recognized_unit = _decimal((ff or {}).get("sku_ff_unit_cost_rub"))
                    baseline_cost = baseline.get(nm_id)
                    if opening_carry and baseline_cost is not None:
                        recognized_unit = _decimal(baseline_cost["recognized"])
                    elif recognized_unit <= ZERO:
                        recognized_unit = _decimal(line["unit_price"]) * _safe_ratio(
                            payment_rub, payment_cny
                        )
                    recognized_total = recognized_unit * qty
                    expense_allocations: list[tuple[sqlite3.Row, Decimal]] = [
                        (expense, _decimal(expense["amount_rub"]) * _safe_ratio(qty, product_qty_total))
                        for expense in expenses
                        if not opening_carry
                        or str(expense["document_date"] or "")[:10] > CUTOVER_DATE
                    ]
                    recognized_expenses = sum((amount for _, amount in expense_allocations), ZERO)
                    invoice_recognized = (
                        recognized_total
                        if opening_carry
                        else max(recognized_total - recognized_expenses, ZERO)
                    )
                    plans.append(
                        {
                            "component_type": "supplier_invoice_and_cny_payment",
                            "shipment_id": shipment_id,
                            "supply_id": "",
                            "nm_id": nm_id,
                            "quantity": _text(qty),
                            "recognized_amount_rub": _text(invoice_recognized),
                            "recognized_date": (
                                CUTOVER_DATE if opening_carry
                                else str(shipment["invoice_date"] or accepted_date)[:10]
                            ),
                            "paid_amount_rub": "0",
                            "paid_equivalent_quantity": "0",
                            "paid_date": None,
                            "allocation_method": "supplier_line_invoice_value_plus_invoice_common_pool",
                            "source_document_id": str(shipment["invoice_document_id"] or ""),
                            "source_line_id": str(line["line_id"]),
                            "evidence": {
                                "ff_cost_layer_line_id": str((ff or {}).get("layer_line_id") or ""),
                                "payment_operation_ids": [str(row["operation_id"]) for row in payments],
                            },
                            "confirmation_status": (
                                "confirmed"
                                if opening_carry and baseline_cost is not None
                                and _decimal(baseline_cost["confirmation"]) == ONE
                                else str((ff or {}).get("source_status") or "needs_review")
                            ),
                        }
                    )
                    for payment in payments:
                        operation_cny = abs(_decimal(payment["cny_delta"]))
                        operation_rub = abs(_decimal(payment["rub_value_delta"]))
                        operation_date = str(payment["operation_date"] or "")[:10]
                        if operation_cny <= ZERO or operation_rub <= ZERO or not operation_date:
                            continue
                        plans.append(
                            {
                                "component_type": "supplier_invoice_payment",
                                "shipment_id": shipment_id,
                                "supply_id": "",
                                "nm_id": nm_id,
                                "quantity": _text(qty),
                                "recognized_amount_rub": "0",
                                "recognized_date": operation_date,
                                "paid_amount_rub": _text(
                                    operation_rub
                                    * _safe_ratio(_decimal(line["amount"]), product_value_total)
                                ),
                                "paid_equivalent_quantity": _text(
                                    qty * min(_safe_ratio(operation_cny, invoice_total), ONE)
                                ),
                                "paid_date": operation_date,
                                "allocation_method": "supplier_line_invoice_value_proportional",
                                "source_document_id": str(payment["source_document_id"] or payment["operation_id"]),
                                "source_line_id": f"{line['line_id']}:{payment['operation_id']}",
                                "evidence": {"cny_ledger_operation_id": str(payment["operation_id"])},
                                "confirmation_status": "confirmed",
                            }
                        )
                    for expense, allocated in expense_allocations:
                        plans.append(
                            {
                                "component_type": str(expense["document_type"] or expense["category"] or "factual_expense"),
                                "shipment_id": shipment_id,
                                "supply_id": "",
                                "nm_id": nm_id,
                                "quantity": _text(qty),
                                "recognized_amount_rub": _text(allocated),
                                "recognized_date": str(expense["document_date"] or accepted_date)[:10],
                                "paid_amount_rub": "0",
                                "paid_equivalent_quantity": "0",
                                "paid_date": None,
                                "allocation_method": "shipment_product_quantity_proportional",
                                "source_document_id": str(expense["financial_document_id"]),
                                "source_line_id": str(expense["line_id"]),
                                "evidence": {
                                    "category": str(expense["category"]),
                                    "file_sha256": str(expense["file_sha256"] or ""),
                                    "ff_cost_layer_line_id": str((ff or {}).get("layer_line_id") or ""),
                                },
                                "confirmation_status": "confirmed",
                            }
                        )
            wb_components = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE is_current=1 AND COALESCE(accepted_date,supply_date,'')>?
                  AND COALESCE(accepted_date,supply_date,'')<=?
                ORDER BY wb_supply_id,nm_id
                """,
                (CUTOVER_DATE, date_to),
            ).fetchall()
            for row in wb_components:
                qty = _decimal(row["accepted_qty"])
                if qty <= ZERO:
                    continue
                for component_type, per_unit_field, source_document in (
                    ("wb_transit", "transit_per_unit_rub", ""),
                    ("ff_services", "ff_services_per_unit_rub", str(row["ff_upload_id"] or "")),
                    ("ff_storage", "ff_storage_per_unit_rub", str(row["ff_upload_id"] or "")),
                ):
                    per_unit = _decimal(row[per_unit_field])
                    status = (
                        str(row["transit_cost_status"])
                        if component_type == "wb_transit"
                        else ("confirmed" if source_document else "missing_or_zero")
                    )
                    if per_unit <= ZERO and status not in {"direct_zero_confirmed", "confirmed"}:
                        continue
                    plans.append(
                        {
                            "component_type": component_type,
                            "shipment_id": "",
                            "supply_id": str(row["wb_supply_id"]),
                            "nm_id": int(row["nm_id"]),
                            "quantity": _text(qty),
                            "recognized_amount_rub": _text(qty * per_unit),
                            "recognized_date": str(row["accepted_date"] or row["supply_date"] or "")[:10],
                            "paid_amount_rub": "0",
                            "paid_equivalent_quantity": "0",
                            "paid_date": None,
                            "allocation_method": "wb_supply_accepted_quantity",
                            "source_document_id": source_document,
                            "source_line_id": str(row["wb_supply_cost_layer_id"]),
                            "evidence": {
                                "legacy_component_layer": str(row["wb_supply_cost_layer_id"]),
                                "status": status,
                            },
                            "confirmation_status": status,
                        }
                    )
        changed = 0
        invalidated: str | None = None
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            had_components = conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_canonical_cost_components LIMIT 1"
            ).fetchone() is not None
            for plan in plans:
                identity = _stable_hash({
                    key: plan[key] for key in (
                        "component_type", "shipment_id", "supply_id", "nm_id",
                        "source_document_id", "source_line_id",
                    )
                })
                fingerprint = _stable_hash(plan)
                existing = conn.execute(
                    """
                    SELECT component_id,fingerprint,version,recognized_date,paid_date
                    FROM sheet_vitrina_v1_canonical_cost_components
                    WHERE component_identity=? AND is_current=1
                    """,
                    (identity,),
                ).fetchone()
                if existing is not None and str(existing["fingerprint"]) == fingerprint:
                    continue
                version = int(existing["version"] or 0) + 1 if existing else 1
                if existing is not None:
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_canonical_cost_components SET is_current=0,superseded_at=? WHERE component_id=?",
                        (now, existing["component_id"]),
                    )
                    candidates = [
                        value for value in (
                            str(existing["recognized_date"] or ""), str(existing["paid_date"] or ""),
                            plan["recognized_date"], plan["paid_date"],
                        ) if value
                    ]
                    if candidates:
                        changed_from = min(candidates)
                        invalidated = min(invalidated, changed_from) if invalidated else changed_from
                elif had_components:
                    candidates = [
                        value for value in (plan["recognized_date"], plan["paid_date"])
                        if value
                    ]
                    if candidates:
                        changed_from = min(candidates)
                        invalidated = min(invalidated, changed_from) if invalidated else changed_from
                component_id = f"ccc_{identity[:16]}_{version}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_canonical_cost_components(
                        component_id,component_identity,component_type,shipment_id,supply_id,nm_id,
                        quantity,recognized_amount_rub,recognized_date,paid_amount_rub,paid_date,
                        paid_equivalent_quantity,
                        allocation_method,source_document_id,source_line_id,evidence_json,
                        confirmation_status,fingerprint,version,is_current,supersedes_id,
                        created_at,superseded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        component_id, identity, plan["component_type"], plan["shipment_id"],
                        plan["supply_id"], plan["nm_id"], plan["quantity"],
                        plan["recognized_amount_rub"], plan["recognized_date"],
                        plan["paid_amount_rub"], plan["paid_date"], plan["paid_equivalent_quantity"],
                        plan["allocation_method"],
                        plan["source_document_id"], plan["source_line_id"],
                        _json_dumps(plan["evidence"]), plan["confirmation_status"], fingerprint,
                        version, 1, str(existing["component_id"]) if existing else None, now, None,
                    ),
                )
                changed += 1
            conn.commit()
        return changed, invalidated

    def _materialize_movement_cost_layers(self, date_to: str) -> int:
        baseline = self._baseline_costs()
        anomaly_report = self.source_anomaly_preflight(date_to=date_to)
        if anomaly_report["status"] != "ok":
            raise CanonicalCostBlocked(
                "cutover_source_anomaly_preflight_blocked",
                {
                    "fingerprint": anomaly_report["fingerprint"],
                    "unresolved_anomalies": anomaly_report["unresolved_anomalies"],
                },
            )
        # physical quantity, recognized capital, cost-covered quantity,
        # primary-confirmed quantity
        recognized_wac: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        paid_wac: dict[int, tuple[Decimal, Decimal]] = {}
        physical_opening = self.physical_quantities_as_of(CUTOVER_DATE)
        for nm_id, costs in baseline.items():
            qty = physical_opening.get(nm_id, {}).get(STAGE_FF, ZERO)
            recognized_wac[nm_id] = (
                qty,
                qty * costs["recognized"],
                qty,
                qty * costs["confirmation"],
            )
            paid_wac[nm_id] = (qty, qty * costs["paid"])
        plans: list[dict[str, Any]] = []
        with _connect(self.runtime.db_path) as conn:
            boundary = _ff_opening_boundary_context(conn)
            baseline_open = {
                (item["supply_id"], item["nm_id"]): item
                for item in _wb_movement_evidence(
                    conn, as_of_date=CUTOVER_DATE, anomaly_report=anomaly_report
                )
                if _decimal(item["open_quantity"]) > ZERO
            }
            for operation in _ff_operation_rows(conn):
                date_resolution = resolve_ff_operation_effective_date(conn, operation)
                effective = date_resolution.effective_date
                if str(operation["operation_type"]) != "auto_writeoff" or not effective or effective > CUTOVER_DATE:
                    continue
                supply_id = str(operation["source_object_id"] or "")
                for line in conn.execute(
                    "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=?",
                    (operation["operation_id"],),
                ).fetchall():
                    nm_id = int(line["nm_id"])
                    sent = abs(min(_decimal(line["quantity_delta"]), ZERO))
                    if sent <= ZERO or (supply_id, nm_id) not in baseline_open:
                        continue
                    costs = baseline.get(nm_id)
                    if costs is None:
                        raise CanonicalCostBlocked(
                            "baseline_transit_cost_missing",
                            {"supply_id": supply_id, "nm_id": nm_id},
                        )
                    plans.append({
                        "operation_id": str(operation["operation_id"]),
                        "supply_id": supply_id,
                        "nm_id": nm_id,
                        "effective_date": CUTOVER_DATE,
                        "sent_quantity": _text(sent),
                        "paid_equivalent_quantity": _text(sent),
                        "cost_coverage_share": "1",
                        "confirmation_share": _text(costs["confirmation"]),
                        "recognized_unit_cost_rub": _text(costs["recognized"]),
                        "paid_unit_cost_rub": _text(costs["paid"]),
                        "recognized_capital_rub": _text(sent * costs["recognized"]),
                        "paid_capital_rub": _text(sent * costs["paid"]),
                        "ff_wac_quantity_before": "baseline",
                        "source_operation_key": str(operation["source_key"]),
                        "effective_date_provenance": date_resolution.provenance,
                    })
            for operation in _ff_operation_rows(conn):
                date_resolution = resolve_ff_operation_effective_date(conn, operation)
                effective = _canonical_ff_operation_effective_date(
                    conn, operation, boundary=boundary
                )
                if not effective or effective <= CUTOVER_DATE or effective > date_to:
                    continue
                lines = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
                    (operation["operation_id"],),
                ).fetchall()
                positive_lines = [line for line in lines if _decimal(line["quantity_delta"]) > ZERO]
                if positive_lines:
                    shipment_id = str(operation["source_object_id"] or "") if str(operation["source_type"]) == "supplier_shipment" else ""
                    component_costs: dict[int, dict[str, Decimal]] = {}
                    for row in conn.execute(
                            """
                            SELECT nm_id,quantity,recognized_amount_rub,paid_amount_rub,
                                   recognized_date,paid_date,paid_equivalent_quantity,
                                   confirmation_status
                            FROM sheet_vitrina_v1_canonical_cost_components
                            WHERE shipment_id=? AND is_current=1
                            """,
                            (shipment_id,),
                        ).fetchall():
                        nm_id = int(row["nm_id"])
                        bucket = component_costs.setdefault(
                            nm_id,
                            {"recognized_amount": ZERO, "paid_amount": ZERO,
                             "quantity": _decimal(row["quantity"]), "paid_quantity": ZERO,
                             "confirmed": ONE},
                        )
                        recognized_applicable = (
                            str(row["recognized_date"] or "") <= effective
                        )
                        paid_applicable = bool(row["paid_date"]) and (
                            str(row["paid_date"]) <= effective
                        )
                        if recognized_applicable:
                            bucket["recognized_amount"] += _decimal(row["recognized_amount_rub"])
                        if paid_applicable:
                            bucket["paid_amount"] += _decimal(row["paid_amount_rub"])
                            bucket["paid_quantity"] += _decimal(row["paid_equivalent_quantity"])
                        if recognized_applicable and str(row["confirmation_status"]) != "confirmed":
                            bucket["confirmed"] = ZERO
                    component_costs = {
                        nm_id: {
                            "recognized": _safe_ratio(costs["recognized_amount"], costs["quantity"]),
                            "paid": _safe_ratio(costs["paid_amount"], costs["paid_quantity"]),
                            "paid_quantity": costs["paid_quantity"],
                            "confirmed": costs["confirmed"],
                        }
                        for nm_id, costs in component_costs.items()
                    }
                    for line in positive_lines:
                        nm_id = int(line["nm_id"])
                        qty = _decimal(line["quantity_delta"])
                        costs = component_costs.get(nm_id)
                        rq, rc, covered, confirmed = recognized_wac.get(
                            nm_id, (ZERO, ZERO, ZERO, ZERO)
                        )
                        pq, pc = paid_wac.get(nm_id, (ZERO, ZERO))
                        if costs is None or costs["recognized"] <= ZERO:
                            recognized_wac[nm_id] = (rq + qty, rc, covered, confirmed)
                            continue
                        recognized_wac[nm_id] = (
                            rq + qty,
                            rc + qty * costs["recognized"],
                            covered + qty,
                            confirmed + qty * costs["confirmed"],
                        )
                        if costs["paid"] > ZERO and costs["paid_quantity"] > ZERO:
                            receipt_paid_qty = min(costs["paid_quantity"], qty)
                            paid_wac[nm_id] = (
                                pq + receipt_paid_qty,
                                pc + receipt_paid_qty * costs["paid"],
                            )
                for line in (line for line in lines if _decimal(line["quantity_delta"]) < ZERO):
                    nm_id = int(line["nm_id"])
                    sent = abs(min(_decimal(line["quantity_delta"]), ZERO))
                    rq, rc, covered, confirmed = recognized_wac.get(
                        nm_id, (ZERO, ZERO, ZERO, ZERO)
                    )
                    pq, pc = paid_wac.get(nm_id, (ZERO, ZERO))
                    if rq < sent:
                        raise CanonicalCostBlocked(
                            "ff_writeoff_exceeds_cost_inventory",
                            {"operation_id": operation["operation_id"], "nm_id": nm_id, "sent": _text(sent), "available": _text(rq)},
                        )
                    coverage_share = min(_safe_ratio(covered, rq), ONE)
                    confirmation_share = min(_safe_ratio(confirmed, rq), ONE)
                    covered_sent = sent * coverage_share
                    recognized_unit = _safe_ratio(rc, covered)
                    paid_unit = _safe_ratio(pc, pq) if pq > ZERO else ZERO
                    recognized_removed = covered_sent * recognized_unit
                    paid_share = min(_safe_ratio(pq, rq), ONE)
                    paid_equivalent_sent = sent * paid_share
                    paid_removed = paid_equivalent_sent * paid_unit
                    if str(operation["operation_type"]) == "auto_writeoff":
                        supply_cost_status = conn.execute(
                            """
                            SELECT source_status FROM sheet_vitrina_v1_wb_supply_cost_layers
                            WHERE wb_supply_id=? AND nm_id=? AND is_current=1
                            """,
                            (str(operation["source_object_id"] or ""), nm_id),
                        ).fetchone()
                        movement_confirmation_share = (
                            confirmation_share
                            if supply_cost_status is not None
                            and str(supply_cost_status["source_status"]) == "confirmed"
                            else ZERO
                        )
                        addons = conn.execute(
                            """
                            SELECT quantity,recognized_amount_rub,paid_amount_rub
                            FROM sheet_vitrina_v1_canonical_cost_components
                            WHERE supply_id=? AND nm_id=? AND is_current=1
                            """,
                            (str(operation["source_object_id"] or ""), nm_id),
                        ).fetchall()
                        addon_recognized = ZERO
                        addon_paid = ZERO
                        for addon in addons:
                            addon_qty = _decimal(addon["quantity"])
                            addon_recognized += sent * _safe_ratio(
                                _decimal(addon["recognized_amount_rub"]), addon_qty
                            )
                            addon_paid += sent * _safe_ratio(
                                _decimal(addon["paid_amount_rub"]), addon_qty
                            )
                        supply_id = str(operation["source_object_id"] or "")
                        movement_recognized_capital = recognized_removed + addon_recognized
                        movement_paid_capital = paid_removed + addon_paid
                        plans.append({
                            "operation_id": str(operation["operation_id"]),
                            "supply_id": supply_id,
                            "nm_id": nm_id,
                            "effective_date": effective,
                            "sent_quantity": _text(sent),
                            "paid_equivalent_quantity": _text(paid_equivalent_sent),
                            "cost_coverage_share": _text(coverage_share),
                            "confirmation_share": _text(movement_confirmation_share),
                            "recognized_unit_cost_rub": _text(
                                _safe_ratio(movement_recognized_capital, covered_sent)
                            ),
                            "paid_unit_cost_rub": _text(
                                _safe_ratio(movement_paid_capital, paid_equivalent_sent)
                            ),
                            "recognized_capital_rub": _text(movement_recognized_capital),
                            "paid_capital_rub": _text(movement_paid_capital),
                            "ff_wac_quantity_before": _text(rq),
                            "source_operation_key": str(operation["source_key"]),
                            "effective_date_provenance": date_resolution.provenance,
                        })
                    recognized_wac[nm_id] = (
                        rq - sent,
                        rc - recognized_removed,
                        covered - covered_sent,
                        max(confirmed - sent * confirmation_share, ZERO),
                    )
                    if pq > ZERO:
                        paid_wac[nm_id] = (
                            pq - paid_equivalent_sent,
                            pc - paid_removed,
                        )
        return self._replace_versioned_movement_plans(plans)

    def _replace_versioned_movement_plans(self, plans: Iterable[Mapping[str, Any]]) -> int:
        changed = 0
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            for plan in plans:
                identity = f"{plan['operation_id']}:{plan['nm_id']}"
                fingerprint = _stable_hash(plan)
                row = conn.execute(
                    "SELECT movement_layer_id,fingerprint,version FROM sheet_vitrina_v1_canonical_cost_movement_layers WHERE movement_identity=? AND is_current=1",
                    (identity,),
                ).fetchone()
                if row is not None and str(row["fingerprint"]) == fingerprint:
                    continue
                version = int(row["version"] or 0) + 1 if row else 1
                if row:
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_canonical_cost_movement_layers SET is_current=0,superseded_at=? WHERE movement_layer_id=?",
                        (now, row["movement_layer_id"]),
                    )
                layer_id = f"ccm_{_stable_hash(identity)[:16]}_{version}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_canonical_cost_movement_layers(
                        movement_layer_id,movement_identity,operation_id,supply_id,nm_id,effective_date,
                        sent_quantity,paid_equivalent_quantity,cost_coverage_share,confirmation_share,
                        recognized_unit_cost_rub,paid_unit_cost_rub,
                        recognized_capital_rub,paid_capital_rub,ff_wac_quantity_before,
                        source_operation_key,fingerprint,version,is_current,supersedes_id,created_at,superseded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        layer_id, identity, plan["operation_id"], plan["supply_id"], plan["nm_id"],
                        plan["effective_date"], plan["sent_quantity"], plan["paid_equivalent_quantity"],
                        plan["cost_coverage_share"], plan["confirmation_share"],
                        plan["recognized_unit_cost_rub"], plan["paid_unit_cost_rub"], plan["recognized_capital_rub"],
                        plan["paid_capital_rub"], plan["ff_wac_quantity_before"],
                        plan["source_operation_key"], fingerprint, version, 1,
                        str(row["movement_layer_id"]) if row else None, now, None,
                    ),
                )
                changed += 1
            conn.commit()
        return changed

    def _materialize_outstanding_layers(self, date_to: str) -> int:
        anomaly_report = self.source_anomaly_preflight(date_to=date_to)
        if anomaly_report["status"] != "ok":
            raise CanonicalCostBlocked(
                "cutover_source_anomaly_preflight_blocked",
                {
                    "fingerprint": anomaly_report["fingerprint"],
                    "unresolved_anomalies": anomaly_report["unresolved_anomalies"],
                },
            )
        with _connect(self.runtime.db_path) as conn:
            movements = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_canonical_cost_movement_layers WHERE is_current=1 AND effective_date<=? ORDER BY effective_date,supply_id,nm_id",
                (date_to,),
            ).fetchall()]
            supply_evidence = _wb_supply_cache_evidence(conn, date_to=date_to)
            movement_evidence = _wb_movement_evidence(
                conn,
                as_of_date=date_to,
                anomaly_report=anomaly_report,
            )
            movement_cost_pools = _movement_cost_pools(movements)
            operation_date_provenance = {}
            for movement in movements:
                operation = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id=?",
                    (str(movement["operation_id"]),),
                ).fetchone()
                if operation is not None:
                    operation_date_provenance[str(movement["operation_id"])] = (
                        resolve_ff_operation_effective_date(
                            conn, dict(operation)
                        ).provenance
                    )
        accepted = {
            (item["supply_id"], item["nm_id"]): item
            for item in movement_evidence
        }
        absorbed_doprinato = {
            (str(item["supply_id"]), int(item["nm_id"]))
            for item in anomaly_report.get("anomalies") or []
            if bool(item.get("eligible"))
            and item.get("classification")
            == UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION
        }
        diagnostic_quarantined_doprinato = {
            (str(item["supply_id"]), int(item["nm_id"]))
            for item in anomaly_report.get(
                "diagnostic_quarantined_doprinato"
            ) or []
        }
        open_layers: list[dict[str, Any]] = []
        for movement in movements:
            key = (str(movement["supply_id"]), int(movement["nm_id"]))
            fact = accepted.get(key, {})
            sent = _decimal(movement["sent_quantity"])
            accepted_qty = _decimal(fact.get("accepted_quantity"))
            open_qty = sent - accepted_qty
            if open_qty < ZERO:
                raise CanonicalCostBlocked("accepted_quantity_exceeds_sent", {"supply_id": key[0], "nm_id": key[1]})
            if not bool(fact.get("is_final_accepted")):
                continue
            normalized = (
                str(fact.get("normalization_policy") or "")
                == POSTCUTOVER_NORMALIZATION_POLICY
            )
            pool = movement_cost_pools.get(key[0], {}) if normalized else {}
            paid_share = (
                _decimal(pool.get("paid_share"))
                if normalized
                else _safe_ratio(
                    _decimal(movement["paid_equivalent_quantity"]), sent
                )
            )
            open_layers.append({
                "original_supply_id": key[0], "nm_id": key[1],
                "warehouse": str(fact.get("warehouse") or ""),
                "destination": str(fact.get("destination") or ""),
                "original_movement_layer_id": str(movement["movement_layer_id"]),
                "sent_quantity": _text(sent), "accepted_quantity": _text(accepted_qty),
                "open_quantity": _text(open_qty),
                "paid_equivalent_quantity": _text(
                    open_qty * paid_share
                ),
                "paid_equivalent_total_quantity": _text(
                    open_qty * paid_share
                ),
                "cost_coverage_share": _text(
                    _decimal(pool.get("coverage_share"))
                    if normalized
                    else _decimal(movement["cost_coverage_share"])
                ),
                "confirmation_share": _text(
                    _decimal(pool.get("confirmation_share"))
                    if normalized
                    else _decimal(movement["confirmation_share"])
                ),
                "recognized_unit_cost_rub": _text(
                    _decimal(pool.get("recognized_unit"))
                    if normalized
                    else _decimal(movement["recognized_unit_cost_rub"])
                ),
                "paid_unit_cost_rub": _text(
                    _decimal(pool.get("paid_unit"))
                    if normalized
                    else _decimal(movement["paid_unit_cost_rub"])
                ),
                "writeoff_date": str(movement["effective_date"]),
                "accepted_date": str(fact.get("accepted_date") or ""),
                "provenance": {
                    "acceptance_source": fact.get("source_identity", ""),
                    "raw_accepted_quantity": _text(
                        _decimal(fact.get("raw_accepted_quantity"))
                    ),
                    "direct_accepted_quantity": _text(
                        _decimal(fact.get("direct_accepted_quantity"))
                    ),
                    "normalized_accepted_quantity": _text(
                        _decimal(fact.get("normalized_accepted_quantity"))
                    ),
                    "normalization_policy": str(
                        fact.get("normalization_policy") or ""
                    ),
                    "effective_date_resolution": operation_date_provenance.get(
                        str(movement["operation_id"]), {}
                    ),
                },
            })
        open_layers = reconcile_outstanding_layers(
            open_layers,
            [
                item for item in supply_evidence
                if item["is_doprinato"] and item["accepted_date"] > CUTOVER_DATE
                and (str(item["supply_id"]), int(item["nm_id"]))
                not in absorbed_doprinato
                and (str(item["supply_id"]), int(item["nm_id"]))
                not in diagnostic_quarantined_doprinato
            ],
        )
        changed = 0
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            current_ids: set[str] = set()
            for plan in open_layers:
                identity = f"{plan['original_supply_id']}:{plan['nm_id']}"
                current_ids.add(identity)
                fingerprint = _stable_hash(plan)
                row = conn.execute(
                    "SELECT outstanding_layer_id,fingerprint,version FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE outstanding_identity=? AND is_current=1",
                    (identity,),
                ).fetchone()
                if row is not None and str(row["fingerprint"]) == fingerprint:
                    continue
                version = int(row["version"] or 0) + 1 if row else 1
                if row:
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_canonical_cost_wb_outstanding_layers SET is_current=0,superseded_at=? WHERE outstanding_layer_id=?",
                        (now, row["outstanding_layer_id"]),
                    )
                layer_id = f"cco_{_stable_hash(identity)[:16]}_{version}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_canonical_cost_wb_outstanding_layers(
                        outstanding_layer_id,outstanding_identity,original_supply_id,nm_id,warehouse,destination,
                        original_movement_layer_id,sent_quantity,accepted_quantity,open_quantity,
                        paid_equivalent_quantity,paid_equivalent_total_quantity,
                        cost_coverage_share,confirmation_share,
                        recognized_unit_cost_rub,paid_unit_cost_rub,writeoff_date,accepted_date,
                        provenance_json,fingerprint,version,is_current,supersedes_id,created_at,superseded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,NULL)
                    """,
                    (
                        layer_id, identity, plan["original_supply_id"], plan["nm_id"], plan["warehouse"],
                        plan["destination"], plan["original_movement_layer_id"], plan["sent_quantity"],
                        plan["accepted_quantity"], plan["open_quantity"], plan["paid_equivalent_quantity"],
                        plan["paid_equivalent_total_quantity"],
                        plan["cost_coverage_share"], plan["confirmation_share"], plan["recognized_unit_cost_rub"],
                        plan["paid_unit_cost_rub"], plan["writeoff_date"], plan["accepted_date"],
                        _json_dumps(plan["provenance"]), fingerprint, version,
                        str(row["outstanding_layer_id"]) if row else None, now,
                    ),
                )
                changed += 1
            stale = conn.execute(
                "SELECT outstanding_layer_id,outstanding_identity FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE is_current=1"
            ).fetchall()
            for row in stale:
                if str(row["outstanding_identity"]) not in current_ids:
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_canonical_cost_wb_outstanding_layers SET is_current=0,superseded_at=? WHERE outstanding_layer_id=?",
                        (now, row["outstanding_layer_id"]),
                    )
                    changed += 1
            conn.commit()
        return changed

    def _supplier_stage_costs_as_of(
        self, as_of_date: str
    ) -> dict[tuple[int, str], dict[str, Decimal | str]]:
        """Full physical quantity plus date-bounded paid-equivalent allocation."""
        result: dict[tuple[int, str], dict[str, Decimal | str]] = {}
        baseline = self._baseline_costs()
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT shipment.*,line.internal_nm_id,line.qty,line.amount,line.unit_price
                FROM sheet_vitrina_v1_supplier_shipments shipment
                JOIN sheet_vitrina_v1_supplier_shipment_lines line
                  ON line.shipment_id=shipment.shipment_id AND line.line_type='product'
                WHERE line.internal_nm_id IS NOT NULL AND COALESCE(line.qty,0)>0
                ORDER BY shipment.shipment_id,line.sort_order
                """
            ).fetchall()
            for row in rows:
                registered = min(
                    value for value in (str(row["shipment_date"] or "")[:10], str(row["created_at"] or "")[:10])
                    if value
                )
                if registered > as_of_date:
                    continue
                shipped = str(row["actual_shipment_date"] or "")[:10]
                accepted = str(row["actual_ff_acceptance_date"] or "")[:10]
                if accepted and accepted <= as_of_date:
                    continue
                stage = STAGE_PRODUCTION_TO_FF if shipped and shipped <= as_of_date else STAGE_PRODUCTION
                shipment_id = str(row["shipment_id"])
                payments = conn.execute(
                    """
                    SELECT cny_delta,rub_value_delta FROM sheet_vitrina_v1_cny_ledger_operations
                    WHERE source_order_id=? AND operation_type='supplier_payment_out'
                      AND status='posted' AND operation_date<=?
                    """,
                    (shipment_id, as_of_date),
                ).fetchall()
                paid_cny = sum((abs(_decimal(item["cny_delta"])) for item in payments), ZERO)
                paid_rub = sum((abs(_decimal(item["rub_value_delta"])) for item in payments), ZERO)
                invoice_total = _decimal(row["invoice_amount_total"])
                paid_share = min(_safe_ratio(paid_cny, invoice_total), ONE)
                line_value = _decimal(row["amount"])
                qty = _decimal(row["qty"])
                product_total = _decimal(row["product_amount_total"])
                allocated_paid = paid_rub * _safe_ratio(line_value, product_total)
                paid_equivalent = qty * paid_share
                paid_unit = _safe_ratio(allocated_paid, paid_equivalent)
                ff_line = conn.execute(
                    """
                    SELECT cost.sku_ff_unit_cost_rub,cost.source_status
                    FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines cost
                    JOIN sheet_vitrina_v1_supplier_ff_cost_layers layer ON layer.layer_id=cost.layer_id
                    WHERE layer.supplier_shipment_id=? AND layer.is_current=1 AND cost.nm_id=?
                      AND layer.accepted_ff_date<=?
                    ORDER BY cost.layer_line_id LIMIT 1
                    """,
                    (shipment_id, int(row["internal_nm_id"]), as_of_date),
                ).fetchone()
                recognized_unit = _decimal(ff_line["sku_ff_unit_cost_rub"]) if ff_line else ZERO
                if recognized_unit <= ZERO:
                    rate = _safe_ratio(paid_rub, paid_cny)
                    recognized_unit = _decimal(row["unit_price"]) * rate
                baseline_cost = baseline.get(int(row["internal_nm_id"]))
                baseline_owned = registered <= CUTOVER_DATE and baseline_cost is not None
                if baseline_owned and recognized_unit <= ZERO:
                    recognized_unit = _decimal(baseline_cost["recognized"])
                key = (int(row["internal_nm_id"]), stage)
                bucket = result.setdefault(
                    key,
                    {
                        "physical": ZERO, "paid_equivalent": ZERO,
                        "recognized_capital": ZERO, "paid_capital": ZERO,
                        "covered": ZERO, "confirmed": ZERO, "quality": "coverage_gap",
                    },
                )
                bucket["physical"] = _decimal(bucket["physical"]) + qty
                bucket["paid_equivalent"] = _decimal(bucket["paid_equivalent"]) + paid_equivalent
                bucket["recognized_capital"] = _decimal(bucket["recognized_capital"]) + qty * recognized_unit
                bucket["paid_capital"] = _decimal(bucket["paid_capital"]) + allocated_paid
                if recognized_unit > ZERO:
                    bucket["covered"] = _decimal(bucket["covered"]) + qty
                if ff_line is not None and str(ff_line["source_status"]) == "confirmed":
                    bucket["confirmed"] = _decimal(bucket["confirmed"]) + qty
                    bucket["quality"] = "primary_documents"
                elif recognized_unit > ZERO:
                    bucket["quality"] = (
                        "primary_documents"
                        if baseline_owned and _decimal(baseline_cost["confirmation"]) == ONE
                        else ("legacy_1c_fallback" if baseline_owned else "estimated_source")
                    )
                    if baseline_owned and _decimal(baseline_cost["confirmation"]) == ONE:
                        bucket["confirmed"] = _decimal(bucket["confirmed"]) + qty
        return result

    def _ff_costs_as_of(self, as_of_date: str) -> dict[int, dict[str, Decimal | str]]:
        baseline = self._baseline_costs()
        opening = self.physical_quantities_as_of(CUTOVER_DATE)
        state: dict[int, dict[str, Decimal | str]] = {
            nm_id: {
                "quantity": opening.get(nm_id, {}).get(STAGE_FF, ZERO),
                "recognized_capital": opening.get(nm_id, {}).get(STAGE_FF, ZERO) * costs["recognized"],
                "paid_quantity": opening.get(nm_id, {}).get(STAGE_FF, ZERO),
                "paid_capital": opening.get(nm_id, {}).get(STAGE_FF, ZERO) * costs["paid"],
                "covered_quantity": opening.get(nm_id, {}).get(STAGE_FF, ZERO),
                "confirmed_quantity": opening.get(nm_id, {}).get(STAGE_FF, ZERO) * costs["confirmation"],
                "quality": "primary_documents" if costs["confirmation"] == ONE else "legacy_1c_fallback",
            }
            for nm_id, costs in baseline.items()
        }
        with _connect(self.runtime.db_path) as conn:
            boundary = _ff_opening_boundary_context(conn)
            component_costs: dict[tuple[str, int], dict[str, Decimal | str]] = {}
            for row in conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_canonical_cost_components
                WHERE is_current=1 AND (recognized_date<=? OR (paid_date IS NOT NULL AND paid_date<=?))
                """,
                (as_of_date, as_of_date),
            ).fetchall():
                qty = _decimal(row["quantity"])
                key = (str(row["shipment_id"]), int(row["nm_id"]))
                bucket = component_costs.setdefault(
                    key,
                    {"recognized_amount": ZERO, "paid_amount": ZERO, "quantity": qty,
                     "paid_quantity": ZERO, "confirmed": ONE},
                )
                if str(row["recognized_date"] or "") <= as_of_date:
                    bucket["recognized_amount"] = _decimal(bucket["recognized_amount"]) + _decimal(row["recognized_amount_rub"])
                    if str(row["confirmation_status"]) != "confirmed":
                        bucket["confirmed"] = ZERO
                if row["paid_date"] and str(row["paid_date"]) <= as_of_date:
                    bucket["paid_amount"] = _decimal(bucket["paid_amount"]) + _decimal(row["paid_amount_rub"])
                    bucket["paid_quantity"] = _decimal(bucket["paid_quantity"]) + _decimal(row["paid_equivalent_quantity"])
            component_costs = {
                key: {
                    "recognized": _safe_ratio(_decimal(costs["recognized_amount"]), _decimal(costs["quantity"])),
                    "paid": _safe_ratio(_decimal(costs["paid_amount"]), _decimal(costs["paid_quantity"])),
                    "paid_quantity": _decimal(costs["paid_quantity"]),
                    "confirmed": _decimal(costs["confirmed"]),
                }
                for key, costs in component_costs.items()
            }
            for operation in _ff_operation_rows(conn):
                effective = _canonical_ff_operation_effective_date(
                    conn, operation, boundary=boundary
                )
                if not effective or effective <= CUTOVER_DATE or effective > as_of_date:
                    continue
                for line in conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
                    (operation["operation_id"],),
                ).fetchall():
                    nm_id = int(line["nm_id"])
                    delta = _decimal(line["quantity_delta"])
                    bucket = state.setdefault(
                        nm_id,
                        {"quantity": ZERO, "recognized_capital": ZERO, "paid_quantity": ZERO,
                         "paid_capital": ZERO, "covered_quantity": ZERO,
                         "confirmed_quantity": ZERO, "quality": "coverage_gap"},
                    )
                    if delta > ZERO:
                        shipment_id = str(operation["source_object_id"] or "") if str(operation["source_type"]) == "supplier_shipment" else ""
                        costs = component_costs.get((shipment_id, nm_id))
                        if costs is None:
                            bucket["quantity"] = _decimal(bucket["quantity"]) + delta
                            bucket["quality"] = "coverage_gap"
                            continue
                        bucket["quantity"] = _decimal(bucket["quantity"]) + delta
                        bucket["recognized_capital"] = _decimal(bucket["recognized_capital"]) + delta * _decimal(costs["recognized"])
                        bucket["covered_quantity"] = _decimal(bucket["covered_quantity"]) + delta
                        if _decimal(costs["paid"]) > ZERO:
                            receipt_paid_qty = min(_decimal(costs["paid_quantity"]), delta)
                            bucket["paid_quantity"] = _decimal(bucket["paid_quantity"]) + receipt_paid_qty
                            bucket["paid_capital"] = _decimal(bucket["paid_capital"]) + receipt_paid_qty * _decimal(costs["paid"])
                        bucket["confirmed_quantity"] = _decimal(bucket["confirmed_quantity"]) + delta * _decimal(costs["confirmed"])
                        bucket["quality"] = "primary_documents" if costs["confirmed"] == ONE else "estimated_source"
                    elif delta < ZERO:
                        writeoff = abs(delta)
                        quantity = _decimal(bucket["quantity"])
                        if writeoff > quantity:
                            raise CanonicalCostBlocked("ff_quantity_replay_negative", {"nm_id": nm_id, "as_of_date": as_of_date})
                        covered_quantity = _decimal(bucket["covered_quantity"])
                        covered_removed = writeoff * _safe_ratio(covered_quantity, quantity)
                        rec_unit = _safe_ratio(
                            _decimal(bucket["recognized_capital"]), covered_quantity
                        )
                        bucket["quantity"] = quantity - writeoff
                        bucket["recognized_capital"] = (
                            _decimal(bucket["recognized_capital"])
                            - covered_removed * rec_unit
                        )
                        bucket["covered_quantity"] = covered_quantity - covered_removed
                        paid_qty = _decimal(bucket["paid_quantity"])
                        paid_removed = writeoff * min(_safe_ratio(paid_qty, quantity), ONE)
                        paid_unit = _safe_ratio(_decimal(bucket["paid_capital"]), paid_qty)
                        bucket["paid_quantity"] = paid_qty - paid_removed
                        bucket["paid_capital"] = _decimal(bucket["paid_capital"]) - paid_removed * paid_unit
                        confirmed_qty = _decimal(bucket["confirmed_quantity"])
                        bucket["confirmed_quantity"] = max(
                            confirmed_qty - writeoff * _safe_ratio(confirmed_qty, quantity), ZERO
                        )
        return state

    def _transit_costs_as_of(self, as_of_date: str) -> dict[int, dict[str, Decimal | str]]:
        anomaly_report = self.source_anomaly_preflight(date_to=as_of_date)
        if anomaly_report["status"] != "ok":
            raise CanonicalCostBlocked(
                "cutover_source_anomaly_preflight_blocked",
                {"fingerprint": anomaly_report["fingerprint"]},
            )
        with _connect(self.runtime.db_path) as conn:
            movements = {
                (str(row["supply_id"]), int(row["nm_id"])): dict(row)
                for row in conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_canonical_cost_movement_layers WHERE is_current=1 AND effective_date<=?",
                    (as_of_date,),
                ).fetchall()
            }
            evidence = _wb_movement_evidence(
                conn, as_of_date=as_of_date, anomaly_report=anomaly_report
            )
        result: dict[int, dict[str, Decimal | str]] = {}
        for fact in evidence:
            open_qty = _decimal(fact["open_quantity"])
            if open_qty <= ZERO:
                continue
            movement = movements.get((fact["supply_id"], fact["nm_id"]))
            bucket = result.setdefault(
                fact["nm_id"],
                {"physical": ZERO, "recognized_capital": ZERO, "paid_capital": ZERO,
                 "paid_equivalent": ZERO, "covered": ZERO, "confirmed": ZERO,
                 "quality": "coverage_gap"},
            )
            bucket["physical"] = _decimal(bucket["physical"]) + open_qty
            if movement is None:
                continue
            recognized = _decimal(movement["recognized_unit_cost_rub"])
            paid = _decimal(movement["paid_unit_cost_rub"])
            covered_qty = open_qty * _decimal(movement["cost_coverage_share"])
            paid_equivalent = open_qty * _safe_ratio(
                _decimal(movement["paid_equivalent_quantity"]),
                _decimal(movement["sent_quantity"]),
            )
            bucket["recognized_capital"] = _decimal(bucket["recognized_capital"]) + covered_qty * recognized
            bucket["paid_capital"] = _decimal(bucket["paid_capital"]) + paid_equivalent * paid
            bucket["covered"] = _decimal(bucket["covered"]) + covered_qty
            bucket["paid_equivalent"] = _decimal(bucket["paid_equivalent"]) + paid_equivalent
            bucket["confirmed"] = _decimal(bucket["confirmed"]) + open_qty * _decimal(movement["confirmation_share"])
            bucket["quality"] = "primary_documents"
        for bucket in result.values():
            if _decimal(bucket["covered"]) < _decimal(bucket["physical"]):
                bucket["quality"] = "coverage_gap"
            elif _decimal(bucket["confirmed"]) < _decimal(bucket["physical"]):
                bucket["quality"] = "estimated_source"
        return result

    def _wb_cost_states(self, dates: Iterable[str]) -> dict[str, dict[int, dict[str, Decimal | str]]]:
        ordered = sorted(dates)
        baseline = self._baseline_costs()
        anomaly_report = self.source_anomaly_preflight(date_to=max(ordered))
        if anomaly_report["status"] != "ok":
            raise CanonicalCostBlocked(
                "cutover_source_anomaly_preflight_blocked",
                {"fingerprint": anomaly_report["fingerprint"]},
            )
        previous: dict[int, dict[str, Decimal | str]] = {}
        result: dict[str, dict[int, dict[str, Decimal | str]]] = {}
        with _connect(self.runtime.db_path) as conn:
            movement_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_canonical_cost_movement_layers WHERE is_current=1"
            ).fetchall()]
            movement_by_key = {
                (str(row["supply_id"]), int(row["nm_id"])): row
                for row in movement_rows
            }
            movement_by_id = {
                str(row["movement_layer_id"]): row for row in movement_rows
            }
            movement_cost_pools = _movement_cost_pools(movement_rows)
            acceptance = [
                item
                for item in _wb_movement_evidence(
                    conn,
                    as_of_date=max(ordered),
                    anomaly_report=anomaly_report,
                )
                if item["is_final_accepted"]
            ]
            doprinato_inbounds: list[dict[str, Any]] = []
            for row in conn.execute(
                """
                SELECT nm_id,original_movement_layer_id,provenance_json
                FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers
                WHERE is_current=1
                """
            ).fetchall():
                movement = movement_by_id.get(str(row["original_movement_layer_id"]))
                if movement is None:
                    continue
                provenance = _json_loads(row["provenance_json"])
                for item in provenance.get("doprinato") or []:
                    accepted_date = str(item.get("accepted_date") or "")
                    qty = _decimal(item.get("quantity"))
                    if accepted_date and qty > ZERO:
                        doprinato_inbounds.append({
                            "nm_id": int(row["nm_id"]),
                            "accepted_date": accepted_date,
                            "quantity": qty,
                            "movement": movement,
                        })
        previous_day = CUTOVER_DATE
        for day in ordered:
            stock = {nm_id: _decimal(qty) for nm_id, qty in self._snapshot_metric(day, OFFICIAL_WB_STOCK_METRIC).items()}
            current: dict[int, dict[str, Decimal | str]] = {}
            for nm_id in sorted(set(stock) | set(previous) | set(baseline)):
                stock_qty = stock.get(nm_id, ZERO)
                if day == CUTOVER_DATE:
                    cost = baseline.get(nm_id)
                    current[nm_id] = {
                        "quantity": stock_qty,
                        "recognized_capital": stock_qty * (cost["recognized"] if cost else ZERO),
                        "paid_quantity": stock_qty if cost and cost["paid"] > ZERO else ZERO,
                        "paid_capital": stock_qty * (cost["paid"] if cost else ZERO),
                        "covered": stock_qty if cost else ZERO,
                        "confirmed": stock_qty * (cost["confirmation"] if cost else ZERO),
                        "quality": "primary_documents" if cost and cost["confirmation"] == ONE else ("legacy_1c_fallback" if cost else "coverage_gap"),
                    }
                    continue
                prev = previous.get(
                    nm_id,
                    {
                        "quantity": ZERO, "recognized_capital": ZERO,
                        "paid_quantity": ZERO, "paid_capital": ZERO,
                        "covered": ZERO, "confirmed": ZERO,
                        "quality": "coverage_gap",
                    },
                )
                inbounds: list[dict[str, Decimal]] = []
                for fact in acceptance:
                    if (
                        fact["nm_id"] != nm_id
                        or not (previous_day < fact["accepted_date"] <= day)
                    ):
                        continue
                    movement = movement_by_key.get((fact["supply_id"], nm_id))
                    if movement is None:
                        continue
                    movement_sent = _decimal(movement["sent_quantity"])
                    qty = _decimal(fact["accepted_quantity"])
                    normalized = (
                        str(fact.get("normalization_policy") or "")
                        == POSTCUTOVER_NORMALIZATION_POLICY
                    )
                    pool = movement_cost_pools.get(str(fact["supply_id"]), {})
                    ratio = _safe_ratio(qty, movement_sent)
                    direct_qty = _decimal(fact.get("direct_accepted_quantity"))
                    inbounds.append({
                        "quantity": qty,
                        "recognized_capital": (
                            qty
                            * _decimal(pool.get("coverage_share"))
                            * _decimal(pool.get("recognized_unit"))
                            if normalized
                            else _decimal(movement["recognized_capital_rub"]) * ratio
                        ),
                        "paid_quantity": (
                            qty * _decimal(pool.get("paid_share"))
                            if normalized
                            else _decimal(movement["paid_equivalent_quantity"]) * ratio
                        ),
                        "paid_capital": (
                            qty
                            * _decimal(pool.get("paid_share"))
                            * _decimal(pool.get("paid_unit"))
                            if normalized
                            else _decimal(movement["paid_capital_rub"]) * ratio
                        ),
                        "covered": qty * (
                            _decimal(pool.get("coverage_share"))
                            if normalized
                            else _decimal(movement["cost_coverage_share"])
                        ),
                        "confirmed": (
                            direct_qty * _decimal(movement["confirmation_share"])
                            if normalized
                            else qty * _decimal(movement["confirmation_share"])
                        ),
                    })
                for fact in doprinato_inbounds:
                    if (
                        fact["nm_id"] != nm_id
                        or not (previous_day < fact["accepted_date"] <= day)
                    ):
                        continue
                    movement = fact["movement"]
                    qty = min(
                        _decimal(fact["quantity"]), _decimal(movement["sent_quantity"])
                    )
                    movement_sent = _decimal(movement["sent_quantity"])
                    ratio = _safe_ratio(qty, movement_sent)
                    inbounds.append({
                        "quantity": qty,
                        "recognized_capital": _decimal(movement["recognized_capital_rub"]) * ratio,
                        "paid_quantity": _decimal(movement["paid_equivalent_quantity"]) * ratio,
                        "paid_capital": _decimal(movement["paid_capital_rub"]) * ratio,
                        "covered": qty * _decimal(movement["cost_coverage_share"]),
                        "confirmed": qty * _decimal(movement["confirmation_share"]),
                    })
                inbound_qty = sum((item["quantity"] for item in inbounds), ZERO)
                prev_qty = _decimal(prev["quantity"])
                pool_qty = prev_qty + inbound_qty
                pool_recognized = _decimal(prev["recognized_capital"]) + sum(
                    (item["recognized_capital"] for item in inbounds), ZERO
                )
                pool_paid_quantity = _decimal(prev["paid_quantity"]) + sum(
                    (item["paid_quantity"] for item in inbounds), ZERO
                )
                pool_paid = _decimal(prev["paid_capital"]) + sum(
                    (item["paid_capital"] for item in inbounds), ZERO
                )
                pool_covered = _decimal(prev["covered"]) + sum(
                    (item["covered"] for item in inbounds), ZERO
                )
                pool_confirmed = _decimal(prev["confirmed"]) + sum(
                    (item["confirmed"] for item in inbounds), ZERO
                )
                unexplained_growth = max(stock_qty - pool_qty, ZERO)
                retained = min(_safe_ratio(stock_qty, pool_qty), ONE)
                recognized_capital = pool_recognized * retained
                paid_quantity = pool_paid_quantity * retained
                paid_capital = pool_paid * retained
                covered = pool_covered * retained
                confirmed = pool_confirmed * retained
                if unexplained_growth > ZERO and pool_qty > ZERO and pool_covered > ZERO:
                    recognized_capital += unexplained_growth * _safe_ratio(
                        pool_recognized, pool_covered
                    )
                    covered += unexplained_growth
                    paid_share = min(_safe_ratio(pool_paid_quantity, pool_qty), ONE)
                    growth_paid_quantity = unexplained_growth * paid_share
                    paid_quantity += growth_paid_quantity
                    paid_capital += growth_paid_quantity * _safe_ratio(
                        pool_paid, pool_paid_quantity
                    )
                quality = "primary_documents"
                if stock_qty > ZERO and covered <= ZERO:
                    quality = "coverage_gap"
                elif covered < stock_qty:
                    quality = "coverage_gap"
                elif unexplained_growth > ZERO:
                    quality = "unexplained_growth_existing_wac"
                elif confirmed < stock_qty:
                    quality = "estimated_source"
                current[nm_id] = {
                    "quantity": stock_qty, "recognized_capital": recognized_capital,
                    "paid_quantity": paid_quantity, "paid_capital": paid_capital,
                    "covered": min(covered, stock_qty), "confirmed": min(confirmed, stock_qty),
                    "quality": quality,
                }
            result[day] = current
            previous = current
            previous_day = day
        return result

    def _materialize_daily_state(self, start: str, end: str) -> int:
        baseline = self._baseline_costs()
        dates = sorted(set(
            self.runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
                date_from=start, date_to=end, descending=False
            ) + [end]
        ))
        wb_by_date = self._wb_cost_states(dates)
        changed = 0
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            outstanding_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE is_current=1"
            ).fetchall()]
            for day in dates:
                physical = self.physical_quantities_as_of(day)
                supplier_costs = self._supplier_stage_costs_as_of(day)
                ff_costs = self._ff_costs_as_of(day)
                transit_costs = self._transit_costs_as_of(day)
                for nm_id, stages in physical.items():
                    costs = baseline.get(nm_id)
                    for stage in STAGES:
                        qty = stages.get(stage, ZERO)
                        recognized_unit = costs["recognized"] if costs else ZERO
                        paid_unit = costs["paid"] if costs else ZERO
                        confirmation = costs["confirmation"] if costs else ZERO
                        paid_equivalent = qty if paid_unit > ZERO else ZERO
                        covered = qty if recognized_unit > ZERO else ZERO
                        quality = "primary_documents" if confirmation == ONE else ("legacy_1c_fallback" if costs else "coverage_gap")
                        recognized_capital = qty * recognized_unit
                        paid_capital = paid_equivalent * paid_unit
                        confirmed_quantity = qty * confirmation
                        if stage in {STAGE_PRODUCTION, STAGE_PRODUCTION_TO_FF}:
                            source = supplier_costs.get((nm_id, stage))
                            if source:
                                paid_equivalent = _decimal(source["paid_equivalent"])
                                recognized_capital = _decimal(source["recognized_capital"])
                                paid_capital = _decimal(source["paid_capital"])
                                covered = _decimal(source["covered"])
                                confirmed_quantity = _decimal(source["confirmed"])
                                quality = str(source["quality"])
                                recognized_unit = _safe_ratio(recognized_capital, qty)
                                paid_unit = _safe_ratio(paid_capital, paid_equivalent)
                            else:
                                recognized_capital = ZERO
                                paid_capital = ZERO
                                paid_equivalent = ZERO
                                covered = ZERO
                                confirmed_quantity = ZERO
                                quality = "coverage_gap"
                        elif stage == STAGE_FF:
                            source = ff_costs.get(nm_id)
                            if source:
                                recognized_capital = _decimal(source["recognized_capital"])
                                paid_capital = _decimal(source["paid_capital"])
                                paid_equivalent = _decimal(source["paid_quantity"])
                                covered = min(_decimal(source["covered_quantity"]), qty)
                                confirmed_quantity = min(_decimal(source["confirmed_quantity"]), qty)
                                quality = str(source["quality"])
                                recognized_unit = _safe_ratio(recognized_capital, qty)
                                paid_unit = _safe_ratio(paid_capital, paid_equivalent)
                            else:
                                recognized_capital = paid_capital = confirmed_quantity = ZERO
                                paid_equivalent = covered = ZERO
                                quality = "coverage_gap"
                        elif stage == STAGE_FF_TO_WB:
                            source = transit_costs.get(nm_id)
                            if source:
                                recognized_capital = _decimal(source["recognized_capital"])
                                paid_capital = _decimal(source["paid_capital"])
                                paid_equivalent = _decimal(source["paid_equivalent"])
                                covered = _decimal(source["covered"])
                                confirmed_quantity = _decimal(source["confirmed"])
                                quality = str(source["quality"])
                                recognized_unit = _safe_ratio(recognized_capital, qty)
                                paid_unit = _safe_ratio(paid_capital, paid_equivalent)
                            else:
                                recognized_capital = paid_capital = confirmed_quantity = ZERO
                                paid_equivalent = covered = ZERO
                                quality = "coverage_gap"
                        elif stage == STAGE_WB:
                            source = wb_by_date.get(day, {}).get(nm_id)
                            if source:
                                recognized_capital = _decimal(source["recognized_capital"])
                                paid_capital = _decimal(source["paid_capital"])
                                paid_equivalent = _decimal(source["paid_quantity"])
                                covered = _decimal(source["covered"])
                                confirmed_quantity = _decimal(source["confirmed"])
                                quality = str(source["quality"])
                                recognized_unit = _safe_ratio(recognized_capital, qty)
                                paid_unit = _safe_ratio(paid_capital, paid_equivalent)
                            else:
                                recognized_capital = paid_capital = confirmed_quantity = ZERO
                                paid_equivalent = covered = ZERO
                                quality = "coverage_gap"
                        under_qty = ZERO
                        under_rec_cap = ZERO
                        under_paid_cap = ZERO
                        if stage == STAGE_FF_TO_WB:
                            eligible = [
                                row for row in outstanding_rows
                                if int(row["nm_id"]) == nm_id
                                and str(row["accepted_date"] or row["writeoff_date"]) <= day
                            ]
                            historical_open: dict[str, Decimal] = {}
                            for row in eligible:
                                initial = max(
                                    _decimal(row["sent_quantity"]) - _decimal(row["accepted_quantity"]),
                                    ZERO,
                                )
                                provenance = _json_loads(row["provenance_json"])
                                closed = sum((
                                    _decimal(item.get("quantity"))
                                    for item in provenance.get("doprinato") or []
                                    if str(item.get("accepted_date") or "") <= day
                                ), ZERO)
                                historical_open[str(row["outstanding_layer_id"])] = max(initial - closed, ZERO)
                            under_qty = sum(historical_open.values(), ZERO)
                            under_rec_cap = sum((
                                historical_open[str(row["outstanding_layer_id"])]
                                * _decimal(row["cost_coverage_share"])
                                * _decimal(row["recognized_unit_cost_rub"])
                                for row in eligible
                            ), ZERO)
                            under_paid_cap = sum((
                                historical_open[str(row["outstanding_layer_id"])]
                                * _safe_ratio(
                                    _decimal(row["paid_equivalent_total_quantity"]),
                                    max(
                                        _decimal(row["sent_quantity"]) - _decimal(row["accepted_quantity"]),
                                        ZERO,
                                    ),
                                )
                                * _decimal(row["paid_unit_cost_rub"])
                                for row in eligible
                            ), ZERO)
                            # Exact layers are already included in physical FF->WB quantity.
                            if under_qty > qty:
                                raise CanonicalCostBlocked(
                                    "underaccepted_exceeds_ff_to_wb_stage",
                                    {"as_of_date": day, "nm_id": nm_id, "underaccepted": _text(under_qty), "stage": _text(qty)},
                                )
                            # The exact transit aggregate already contains underaccepted;
                            # submetrics are presentation-only and must never be re-added.
                        row_payload = {
                            "as_of_date": day, "nm_id": nm_id, "stage": stage,
                            "physical_quantity": _text(qty),
                            "paid_equivalent_quantity": _text(paid_equivalent),
                            "recognized_capital_rub": _text(recognized_capital),
                            "paid_capital_rub": _text(paid_capital),
                            "cost_covered_quantity": _text(covered),
                            "confirmed_quantity": _text(confirmed_quantity),
                            "recognized_unit_cost_rub": (
                                _text(_safe_ratio(recognized_capital, covered))
                                if covered > ZERO else None
                            ),
                            "paid_unit_cost_rub": (
                                _text(_safe_ratio(paid_capital, paid_equivalent))
                                if paid_equivalent > ZERO else None
                            ),
                            "underaccepted_quantity": _text(under_qty),
                            "underaccepted_recognized_capital_rub": _text(under_rec_cap),
                            "underaccepted_paid_capital_rub": _text(under_paid_cap),
                            "source_quality": quality,
                        }
                        fingerprint = _stable_hash(row_payload)
                        existing = conn.execute(
                            "SELECT fingerprint FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE as_of_date=? AND nm_id=? AND stage=?",
                            (day, nm_id, stage),
                        ).fetchone()
                        if existing is not None and str(existing["fingerprint"]) == fingerprint:
                            continue
                        conn.execute(
                            """
                            INSERT INTO sheet_vitrina_v1_canonical_cost_daily_state(
                                as_of_date,nm_id,stage,physical_quantity,paid_equivalent_quantity,
                                recognized_capital_rub,paid_capital_rub,cost_covered_quantity,
                                confirmed_quantity,recognized_unit_cost_rub,paid_unit_cost_rub,
                                underaccepted_quantity,underaccepted_recognized_capital_rub,
                                underaccepted_paid_capital_rub,source_quality,diagnostics_json,
                                calculated_at,fingerprint
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(as_of_date,nm_id,stage) DO UPDATE SET
                                physical_quantity=excluded.physical_quantity,
                                paid_equivalent_quantity=excluded.paid_equivalent_quantity,
                                recognized_capital_rub=excluded.recognized_capital_rub,
                                paid_capital_rub=excluded.paid_capital_rub,
                                cost_covered_quantity=excluded.cost_covered_quantity,
                                confirmed_quantity=excluded.confirmed_quantity,
                                recognized_unit_cost_rub=excluded.recognized_unit_cost_rub,
                                paid_unit_cost_rub=excluded.paid_unit_cost_rub,
                                underaccepted_quantity=excluded.underaccepted_quantity,
                                underaccepted_recognized_capital_rub=excluded.underaccepted_recognized_capital_rub,
                                underaccepted_paid_capital_rub=excluded.underaccepted_paid_capital_rub,
                                source_quality=excluded.source_quality,
                                diagnostics_json=excluded.diagnostics_json,
                                calculated_at=excluded.calculated_at,fingerprint=excluded.fingerprint
                            """,
                            (
                                day,nm_id,stage,row_payload["physical_quantity"],row_payload["paid_equivalent_quantity"],
                                row_payload["recognized_capital_rub"],row_payload["paid_capital_rub"],
                                row_payload["cost_covered_quantity"],row_payload["confirmed_quantity"],
                                row_payload["recognized_unit_cost_rub"],row_payload["paid_unit_cost_rub"],
                                row_payload["underaccepted_quantity"],row_payload["underaccepted_recognized_capital_rub"],
                                row_payload["underaccepted_paid_capital_rub"],row_payload["source_quality"],
                                _json_dumps({"physical_source": _stage_source(stage)}),now,fingerprint,
                            ),
                        )
                        changed += 1
                        # Prevent one stage's local values leaking into the next stage.
                        del recognized_capital, paid_capital, confirmed_quantity
            conn.commit()
        return changed

    def _projection_fingerprint(self, start: str, end: str) -> str:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT as_of_date,nm_id,stage,fingerprint
                FROM sheet_vitrina_v1_canonical_cost_daily_state
                WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date,nm_id,stage
                """,
                (start, end),
            ).fetchall()
        return _stable_hash([list(row) for row in rows])


def ensure_canonical_cost_schema(conn: sqlite3.Connection) -> None:
    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_baseline_versions(
            baseline_id TEXT PRIMARY KEY, version INTEGER NOT NULL, cutover_date TEXT NOT NULL,
            primary_shipment_id TEXT NOT NULL, primary_accepted_ff_date TEXT NOT NULL,
            primary_quantity TEXT NOT NULL, primary_sku_count INTEGER NOT NULL,
            weighted_ff_unit_cost_rub TEXT NOT NULL, fallback_sku_count INTEGER NOT NULL,
            business_approved_sku_count INTEGER NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE, report_json TEXT NOT NULL, is_current INTEGER NOT NULL,
            created_at TEXT NOT NULL, superseded_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_cost_baseline_current
        ON sheet_vitrina_v1_canonical_cost_baseline_versions(is_current) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_baseline_lines(
            baseline_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_canonical_cost_baseline_versions(baseline_id),
            nm_id INTEGER NOT NULL, stage TEXT NOT NULL, physical_quantity TEXT NOT NULL,
            paid_equivalent_quantity TEXT NOT NULL, recognized_unit_cost_rub TEXT NOT NULL,
            paid_unit_cost_rub TEXT NOT NULL, recognized_capital_rub TEXT NOT NULL,
            paid_capital_rub TEXT NOT NULL, cost_covered_quantity TEXT NOT NULL,
            confirmed_quantity TEXT NOT NULL, source_type TEXT NOT NULL,
            source_identity TEXT NOT NULL, source_date TEXT NOT NULL,
            provenance_json TEXT NOT NULL, line_fingerprint TEXT NOT NULL,
            PRIMARY KEY(baseline_id,nm_id,stage)
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_components(
            component_id TEXT PRIMARY KEY, component_identity TEXT NOT NULL,
            component_type TEXT NOT NULL, shipment_id TEXT, supply_id TEXT, nm_id INTEGER NOT NULL,
            quantity TEXT NOT NULL, recognized_amount_rub TEXT NOT NULL, recognized_date TEXT NOT NULL,
            paid_amount_rub TEXT NOT NULL, paid_date TEXT,
            paid_equivalent_quantity TEXT NOT NULL, allocation_method TEXT NOT NULL,
            source_document_id TEXT, source_line_id TEXT, evidence_json TEXT NOT NULL,
            confirmation_status TEXT NOT NULL, fingerprint TEXT NOT NULL, version INTEGER NOT NULL,
            is_current INTEGER NOT NULL, supersedes_id TEXT, created_at TEXT NOT NULL, superseded_at TEXT,
            UNIQUE(component_identity,version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_cost_components_current
        ON sheet_vitrina_v1_canonical_cost_components(component_identity) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_movement_layers(
            movement_layer_id TEXT PRIMARY KEY, movement_identity TEXT NOT NULL,
            operation_id TEXT NOT NULL, supply_id TEXT NOT NULL, nm_id INTEGER NOT NULL,
            effective_date TEXT NOT NULL, sent_quantity TEXT NOT NULL,
            paid_equivalent_quantity TEXT NOT NULL, cost_coverage_share TEXT NOT NULL,
            confirmation_share TEXT NOT NULL,
            recognized_unit_cost_rub TEXT, paid_unit_cost_rub TEXT,
            recognized_capital_rub TEXT NOT NULL, paid_capital_rub TEXT NOT NULL,
            ff_wac_quantity_before TEXT NOT NULL, source_operation_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL, version INTEGER NOT NULL, is_current INTEGER NOT NULL,
            supersedes_id TEXT, created_at TEXT NOT NULL, superseded_at TEXT,
            UNIQUE(movement_identity,version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_cost_movements_current
        ON sheet_vitrina_v1_canonical_cost_movement_layers(movement_identity) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_wb_outstanding_layers(
            outstanding_layer_id TEXT PRIMARY KEY, outstanding_identity TEXT NOT NULL,
            original_supply_id TEXT NOT NULL, nm_id INTEGER NOT NULL, warehouse TEXT NOT NULL,
            destination TEXT NOT NULL, original_movement_layer_id TEXT NOT NULL,
            sent_quantity TEXT NOT NULL, accepted_quantity TEXT NOT NULL, open_quantity TEXT NOT NULL,
            paid_equivalent_quantity TEXT NOT NULL, paid_equivalent_total_quantity TEXT NOT NULL,
            cost_coverage_share TEXT NOT NULL,
            confirmation_share TEXT NOT NULL,
            recognized_unit_cost_rub TEXT NOT NULL, paid_unit_cost_rub TEXT NOT NULL,
            writeoff_date TEXT NOT NULL, accepted_date TEXT, provenance_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL, version INTEGER NOT NULL, is_current INTEGER NOT NULL,
            supersedes_id TEXT, created_at TEXT NOT NULL, superseded_at TEXT,
            UNIQUE(outstanding_identity,version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_cost_outstanding_current
        ON sheet_vitrina_v1_canonical_cost_wb_outstanding_layers(outstanding_identity) WHERE is_current=1;
        CREATE INDEX IF NOT EXISTS canonical_cost_outstanding_fifo
        ON sheet_vitrina_v1_canonical_cost_wb_outstanding_layers(
            warehouse,destination,nm_id,accepted_date,original_supply_id
        ) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_daily_state(
            as_of_date TEXT NOT NULL, nm_id INTEGER NOT NULL, stage TEXT NOT NULL,
            physical_quantity TEXT NOT NULL, paid_equivalent_quantity TEXT NOT NULL,
            recognized_capital_rub TEXT NOT NULL, paid_capital_rub TEXT NOT NULL,
            cost_covered_quantity TEXT NOT NULL, confirmed_quantity TEXT NOT NULL,
            recognized_unit_cost_rub TEXT, paid_unit_cost_rub TEXT,
            underaccepted_quantity TEXT NOT NULL, underaccepted_recognized_capital_rub TEXT NOT NULL,
            underaccepted_paid_capital_rub TEXT NOT NULL, source_quality TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL, calculated_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
            PRIMARY KEY(as_of_date,nm_id,stage)
        );
        CREATE INDEX IF NOT EXISTS canonical_cost_daily_by_date_stage
        ON sheet_vitrina_v1_canonical_cost_daily_state(as_of_date,stage,nm_id);
        """
    )


def _execute_schema_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute simple DDL without sqlite3.executescript's implicit COMMIT."""
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)


def allocate_partial_payment(
    product_lines: Iterable[Mapping[str, Any]], *, paid_share: Any, paid_rub: Any
) -> list[dict[str, Decimal | int]]:
    """Deterministically allocate a partial payment to every matched SKU line."""
    share = _decimal(paid_share)
    amount = _decimal(paid_rub)
    if share <= ZERO or share > ONE or amount <= ZERO:
        raise ValueError("paid_share must be in (0,1] and paid_rub must be positive")
    lines = [dict(item) for item in product_lines]
    if not lines:
        raise ValueError("product lines are required")
    values: list[tuple[int, Decimal, Decimal]] = []
    for item in lines:
        nm_id = int(item.get("nm_id") or item.get("internal_nm_id") or 0)
        qty = _decimal(item.get("qty"))
        invoice_value = _decimal(item.get("invoice_value") or item.get("amount"))
        if nm_id <= 0 or qty <= ZERO or invoice_value <= ZERO:
            raise ValueError("all payment allocation lines require matched nm_id, qty and invoice value")
        values.append((nm_id, qty, invoice_value))
    total_value = sum((item[2] for item in values), ZERO)
    remaining = amount
    result: list[dict[str, Decimal | int]] = []
    for index, (nm_id, qty, invoice_value) in enumerate(values):
        allocated = remaining if index == len(values) - 1 else amount * invoice_value / total_value
        remaining -= allocated
        result.append({
            "nm_id": nm_id,
            "physical_quantity": qty,
            "paid_equivalent_quantity": qty * share,
            "paid_capital_rub": allocated,
        })
    return result


def roll_wac(
    *, quantity: Any, capital: Any, receipt_quantity: Any = ZERO,
    receipt_unit_cost: Any = ZERO, writeoff_quantity: Any = ZERO,
) -> tuple[Decimal, Decimal, Decimal]:
    qty = _decimal(quantity)
    cap = _decimal(capital)
    receipt = _decimal(receipt_quantity)
    unit = _decimal(receipt_unit_cost)
    writeoff = _decimal(writeoff_quantity)
    if min(qty, cap, receipt, unit, writeoff) < ZERO:
        raise ValueError("WAC inputs cannot be negative")
    qty += receipt
    cap += receipt * unit
    if writeoff > qty:
        raise ValueError("writeoff exceeds WAC quantity")
    wac = _safe_ratio(cap, qty) if qty > ZERO else ZERO
    qty -= writeoff
    cap -= writeoff * wac
    return qty, cap, wac


def reconcile_outstanding_layers(
    layers: Iterable[Mapping[str, Any]],
    reconciliations: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply direct-identity/FIFO doprinato without inventing or mixing layers."""
    result = [
        {
            **dict(item),
            "provenance": dict(item.get("provenance") or {}),
            "open_quantity": _text(_decimal(item.get("open_quantity"))),
        }
        for item in layers
    ]
    seen: dict[str, str] = {}
    ordered = sorted(
        (dict(item) for item in reconciliations),
        key=lambda item: (str(item.get("accepted_date") or ""), str(item.get("supply_id") or ""), int(item.get("nm_id") or 0)),
    )
    for fact in ordered:
        supply_id = str(fact.get("supply_id") or "")
        identity = f"{supply_id}:{int(fact.get('nm_id') or 0)}"
        fingerprint = _stable_hash(fact)
        if identity in seen:
            if seen[identity] != fingerprint:
                raise CanonicalCostBlocked("doprinato_identity_conflict", {"supply_id": supply_id})
            continue
        seen[identity] = fingerprint
        remaining = _decimal(fact.get("accepted_quantity"))
        if remaining <= ZERO:
            raise CanonicalCostBlocked("doprinato_nonpositive_quantity", {"supply_id": supply_id})
        nm_id = int(fact.get("nm_id") or 0)
        accepted_date = str(fact.get("accepted_date") or "")
        original = str(fact.get("original_supply_id") or "")
        candidates = [
            item for item in result
            if int(item.get("nm_id") or 0) == nm_id
            and _decimal(item.get("open_quantity")) > ZERO
            and str(item.get("accepted_date") or item.get("writeoff_date") or "") <= accepted_date
            and (
                (original and str(item.get("original_supply_id") or "") == original)
                or (
                    not original
                    and str(item.get("warehouse") or "") == str(fact.get("warehouse") or "")
                    and str(item.get("destination") or "") == str(fact.get("destination") or "")
                )
            )
        ]
        candidates.sort(
            key=lambda item: (
                str(item.get("accepted_date") or item.get("writeoff_date") or ""),
                str(item.get("original_supply_id") or ""),
            )
        )
        for item in candidates:
            if remaining <= ZERO:
                break
            open_before = _decimal(item["open_quantity"])
            close = min(remaining, open_before)
            item["open_quantity"] = _text(open_before - close)
            if "paid_equivalent_quantity" in item:
                paid_before = _decimal(item.get("paid_equivalent_quantity"))
                item["paid_equivalent_quantity"] = _text(
                    max(paid_before - close * _safe_ratio(paid_before, open_before), ZERO)
                )
            item["provenance"].setdefault("doprinato", []).append(
                {
                    "supply_id": supply_id,
                    "quantity": _text(close),
                    "accepted_date": accepted_date,
                }
            )
            remaining -= close
        if remaining > ZERO:
            raise CanonicalCostBlocked(
                "doprinato_unmatched_surplus",
                {"supply_id": supply_id, "nm_id": nm_id, "surplus": _text(remaining)},
            )
    return result


def _ff_operation_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM sheet_vitrina_v1_ff_stock_operations"
    ).fetchall()]
    return sorted(
        rows,
        key=lambda row: (
            _ff_operation_effective_date(conn, row),
            str(row.get("created_at") or ""),
            str(row.get("operation_id") or ""),
        ),
    )


def _ff_opening_boundary_context(conn: sqlite3.Connection) -> dict[str, Any]:
    """Identify repo-owned activation/checkpoint history collapsed at cutover."""

    cache_keys, source_keys, supply_ids, checkpoint_id = _checkpoint_identity_sets(conn)
    checkpoint_operation_ids: set[str] = set()
    for row in conn.execute(
        """
        SELECT operation_id,source_key,source_object_id,diagnostics_json
        FROM sheet_vitrina_v1_ff_stock_operations
        WHERE operation_type='auto_writeoff' AND source_type='wb_supply'
        """
    ).fetchall():
        operation_id = str(row["operation_id"] or "")
        source_key = str(row["source_key"] or "")
        supply_id = str(row["source_object_id"] or "")
        diagnostics = _json_loads(row["diagnostics_json"])
        if (
            str(diagnostics.get("reason") or "")
            == TARGETED_PRE_ACTIVATION_REMEDIATION_REASON
            or str(diagnostics.get("remediation") or "")
            == TARGETED_PRE_ACTIVATION_REMEDIATION_REASON
        ):
            # This is the one repo-owned real post-cutover movement.  Its
            # checkpoint membership authorizes the bounded ledger debit; it
            # does not turn the debit into opening audit history.
            continue
        source_identity = source_key.removeprefix("wb_supply_debit:")
        if (
            source_key in source_keys
            and supply_id in supply_ids
            and source_identity in cache_keys
        ):
            checkpoint_operation_ids.add(operation_id)
    compensation_operation_ids: set[str] = set()
    compensated_operation_ids: set[str] = set()
    for row in conn.execute(
        """
        SELECT operation_id,source_type,diagnostics_json
        FROM sheet_vitrina_v1_ff_stock_operations
        WHERE source_type='runtime_repair'
           OR operation_type='correction_receipt'
        """
    ).fetchall():
        diagnostics = _json_loads(row["diagnostics_json"])
        original_id = str(
            diagnostics.get("original_operation_id")
            or diagnostics.get("compensates_operation_id")
            or ""
        )
        if original_id and original_id in checkpoint_operation_ids:
            compensation_operation_ids.add(str(row["operation_id"] or ""))
            compensated_operation_ids.add(original_id)
    activation = conn.execute(
        """
        SELECT operation_id,created_at,source_key,total_quantity_delta
        FROM sheet_vitrina_v1_ff_stock_operations
        WHERE source_type<>'wb_supply' AND total_quantity_delta>0
          AND operation_id NOT IN (
              SELECT operation_id FROM sheet_vitrina_v1_ff_stock_operations
              WHERE source_type='runtime_repair'
                 OR operation_type='correction_receipt'
          )
        ORDER BY created_at,operation_id LIMIT 1
        """
    ).fetchone()
    activation_id = str(activation["operation_id"] or "") if activation else ""
    return {
        "checkpoint_id": checkpoint_id,
        "activation_operation_id": activation_id,
        "activation_created_at": str(activation["created_at"] or "") if activation else "",
        "checkpoint_operation_ids": checkpoint_operation_ids,
        "compensation_operation_ids": compensation_operation_ids,
        "compensated_operation_ids": compensated_operation_ids,
    }


def _canonical_ff_operation_effective_date(
    conn: sqlite3.Connection,
    operation: Mapping[str, Any],
    *,
    boundary: Mapping[str, Any] | None = None,
) -> str | None:
    """Return canonical physical replay date, or ``None`` for audit-only rows."""

    context = boundary or _ff_opening_boundary_context(conn)
    operation_id = str(operation.get("operation_id") or "")
    if operation_id == str(context.get("activation_operation_id") or ""):
        return CUTOVER_DATE
    if operation_id in set(context.get("checkpoint_operation_ids") or set()):
        return None
    if operation_id in set(context.get("compensation_operation_ids") or set()):
        return None
    effective = _ff_operation_effective_date(conn, operation)
    if effective < CUTOVER_DATE:
        return None
    return effective


def _ff_operation_effective_date(conn: sqlite3.Connection, operation: Mapping[str, Any]) -> str:
    return resolve_ff_operation_effective_date(conn, operation).effective_date


def resolve_ff_operation_effective_date(
    conn: sqlite3.Connection, operation: Mapping[str, Any]
) -> FfOperationDateResolution:
    """Resolve an FF operation business date without trusting WB write timestamps."""

    operation_id = str(operation.get("operation_id") or "")
    source_type = str(operation.get("source_type") or "")
    if source_type == "supplier_shipment":
        row = conn.execute(
            "SELECT actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (str(operation.get("source_object_id") or ""),),
        ).fetchone()
        if row and row[0]:
            effective_date = _effective_date_value(
                row[0],
                blocker_code="supplier_shipment_effective_date_invalid",
                details={"operation_id": operation_id},
            )
            return FfOperationDateResolution(
                effective_date=effective_date,
                provenance={
                    "resolution_method": "supplier_shipment_actual_ff_acceptance_date",
                    "source_field": "supplier_shipment.actual_ff_acceptance_date",
                    "source_identity": str(operation.get("source_object_id") or ""),
                    "operation_id": operation_id,
                },
            )
    diagnostics = _json_loads(operation.get("diagnostics_json"))
    if "source_timestamp" in diagnostics and str(
        diagnostics.get("source_timestamp") or ""
    ).strip():
        effective_date = _effective_date_value(
            diagnostics.get("source_timestamp"),
            blocker_code="wb_supply_source_timestamp_invalid",
            details={"operation_id": operation_id, "source_field": "diagnostics.source_timestamp"},
        )
        return FfOperationDateResolution(
            effective_date=effective_date,
            provenance={
                "resolution_method": "persisted_operation_source_timestamp",
                "source_field": "diagnostics.source_timestamp",
                "source_identity": str(operation.get("source_key") or ""),
                "operation_id": operation_id,
                "supply_id": str(operation.get("source_object_id") or ""),
            },
        )
    # The bounded 40561872 remediation predates this canonical resolver and
    # persisted the same repo-owned source timestamp as ``supply_timestamp``.
    # It remains evidence, not a hardcoded identity/date exception.
    if source_type == "wb_supply" and str(
        diagnostics.get("supply_timestamp") or ""
    ).strip():
        effective_date = _effective_date_value(
            diagnostics.get("supply_timestamp"),
            blocker_code="wb_supply_source_timestamp_invalid",
            details={"operation_id": operation_id, "source_field": "diagnostics.supply_timestamp"},
        )
        return FfOperationDateResolution(
            effective_date=effective_date,
            provenance={
                "resolution_method": "persisted_operation_supply_timestamp_compatibility",
                "source_field": "diagnostics.supply_timestamp",
                "source_identity": str(operation.get("source_key") or ""),
                "operation_id": operation_id,
                "supply_id": str(operation.get("source_object_id") or ""),
            },
        )
    if source_type == "wb_supply":
        supply = _load_exact_wb_supply_for_operation(conn, operation)
        effective_date, source_field = _wb_supply_authoritative_business_date(supply)
        return FfOperationDateResolution(
            effective_date=effective_date,
            provenance={
                "resolution_method": "authoritative_persisted_wb_supply_business_date",
                "source_field": source_field,
                "source_identity": str(supply.get("cache_key") or supply.get("supply_id") or ""),
                "operation_id": operation_id,
                "supply_id": str(supply.get("supply_id") or ""),
                "source_key": str(operation.get("source_key") or ""),
            },
        )
    created_at = str(operation.get("created_at") or "")
    return FfOperationDateResolution(
        effective_date=created_at[:10],
        provenance={
            "resolution_method": "operation_created_at",
            "source_field": "ff_operation.created_at",
            "source_identity": operation_id,
            "operation_id": operation_id,
        },
    )


def _load_exact_wb_supply_for_operation(
    conn: sqlite3.Connection, operation: Mapping[str, Any]
) -> dict[str, Any]:
    operation_id = str(operation.get("operation_id") or "")
    source_object_id = str(operation.get("source_object_id") or "").strip()
    source_key = str(operation.get("source_key") or "").strip()
    source_identity = source_key.removeprefix("wb_supply_debit:")
    lookup_values = tuple(
        sorted({value for value in (source_object_id, source_identity) if value})
    )
    if not lookup_values:
        raise CanonicalCostBlocked(
            "wb_supply_effective_date_identity_missing",
            {"operation_id": operation_id, "source_key": source_key},
        )
    placeholders = ",".join("?" for _ in lookup_values)
    rows = conn.execute(
        f"""
        SELECT * FROM sheet_vitrina_v1_wb_supplies
        WHERE supply_id IN ({placeholders})
           OR cache_key IN ({placeholders})
           OR wb_supply_id IN ({placeholders})
           OR preorder_id IN ({placeholders})
        ORDER BY supply_id
        """,
        lookup_values * 4,
    ).fetchall()
    unique = {str(row["supply_id"]): dict(row) for row in rows}
    if not unique:
        raise CanonicalCostBlocked(
            "wb_supply_effective_date_supply_missing",
            {
                "operation_id": operation_id,
                "source_object_id": source_object_id,
                "source_key": source_key,
            },
        )
    if len(unique) != 1:
        raise CanonicalCostBlocked(
            "wb_supply_effective_date_supply_ambiguous",
            {
                "operation_id": operation_id,
                "source_object_id": source_object_id,
                "source_key": source_key,
                "matched_supply_ids": sorted(unique),
            },
        )
    supply = next(iter(unique.values()))
    normalized = _json_loads(supply.get("normalized_row_json"))
    supply_identities = {
        str(value).strip()
        for value in (
            supply.get("supply_id"),
            supply.get("wb_supply_id"),
            supply.get("preorder_id"),
            normalized.get("supply_id"),
            normalized.get("wb_supply_id"),
            normalized.get("preorder_id"),
        )
        if str(value or "").strip()
    }
    cache_identities = {
        str(value).strip()
        for value in (
            supply.get("cache_key"),
            normalized.get("cache_key"),
        )
        if str(value or "").strip()
    }
    if source_object_id not in supply_identities or source_identity not in cache_identities:
        raise CanonicalCostBlocked(
            "wb_supply_effective_date_identity_mismatch",
            {
                "operation_id": operation_id,
                "source_object_id": source_object_id,
                "source_key": source_key,
                "matched_supply_id": str(supply.get("supply_id") or ""),
                "matched_cache_key": str(supply.get("cache_key") or ""),
            },
        )
    return supply


def _wb_supply_authoritative_business_date(
    supply: Mapping[str, Any]
) -> tuple[str, str]:
    normalized = _json_loads(supply.get("normalized_row_json"))
    candidates = (
        ("normalized.actual_acceptance_date", normalized.get("actual_acceptance_date")),
        ("normalized.actualAcceptanceDate", normalized.get("actualAcceptanceDate")),
        ("normalized.acceptance_date", normalized.get("acceptance_date")),
        ("normalized.acceptanceDate", normalized.get("acceptanceDate")),
        ("normalized.fact_date", normalized.get("fact_date")),
        ("normalized.factDate", normalized.get("factDate")),
        ("normalized.closed_at", normalized.get("closed_at")),
        ("normalized.closedAt", normalized.get("closedAt")),
        ("wb_supply.fact_date", supply.get("fact_date")),
        ("normalized.supply_date", normalized.get("supply_date")),
        ("normalized.supplyDate", normalized.get("supplyDate")),
        ("wb_supply.supply_date", supply.get("supply_date")),
    )
    for source_field, value in candidates:
        if not str(value or "").strip():
            continue
        return (
            _effective_date_value(
                value,
                blocker_code="wb_supply_effective_date_business_date_invalid",
                details={
                    "supply_id": str(supply.get("supply_id") or ""),
                    "source_field": source_field,
                },
            ),
            source_field,
        )
    raise CanonicalCostBlocked(
        "wb_supply_effective_date_business_date_missing",
        {
            "supply_id": str(supply.get("supply_id") or ""),
            "cache_key": str(supply.get("cache_key") or ""),
        },
    )


def _effective_date_value(
    value: Any, *, blocker_code: str, details: Mapping[str, Any]
) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except (TypeError, ValueError):
        raise CanonicalCostBlocked(
            blocker_code, {**dict(details), "value": text}
        ) from None


def _wb_operation_and_acceptance_nm_ids(
    conn: sqlite3.Connection, *, date_to: str
) -> set[int]:
    result = {
        int(row["nm_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT line.nm_id
            FROM sheet_vitrina_v1_ff_stock_operations operation
            JOIN sheet_vitrina_v1_ff_stock_operation_lines line
              ON line.operation_id=operation.operation_id
            WHERE operation.operation_type='auto_writeoff'
              AND operation.source_type='wb_supply'
              AND line.nm_id IS NOT NULL
            """
        ).fetchall()
        if int(row["nm_id"] or 0) > 0
    }
    for item in _wb_supply_cache_evidence(conn, date_to=date_to):
        if int(item["nm_id"] or 0) > 0:
            result.add(int(item["nm_id"]))
    return result


def _checkpoint_identity_sets(
    conn: sqlite3.Connection,
) -> tuple[set[str], set[str], set[str], str]:
    row = conn.execute(
        """
        SELECT checkpoint_id,baseline_cache_keys_json,baseline_source_keys_json,
               baseline_supply_ids_json
        FROM sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint
        WHERE slot='current'
        """
    ).fetchone()
    if row is None:
        return set(), set(), set(), ""
    return (
        set(_json_loads(row["baseline_cache_keys_json"]) or []),
        set(_json_loads(row["baseline_source_keys_json"]) or []),
        set(_json_loads(row["baseline_supply_ids_json"]) or []),
        str(row["checkpoint_id"] or ""),
    )


def _is_integer_quantity(value: Decimal) -> bool:
    return value == value.to_integral_value()


def _source_anomaly_preflight_conn(
    conn: sqlite3.Connection,
    *,
    date_to: str,
    baseline_costs: Mapping[int, Mapping[str, Any]],
    baseline_cost_references: Mapping[
        tuple[int, str], Mapping[str, Any]
    ] | None = None,
    diagnostic_quarantined_doprinato_keys: set[tuple[str, int]] | None = None,
) -> dict[str, Any]:
    """Collect every bounded-policy candidate without failing on the first row."""

    cache_keys, source_keys, supply_ids, checkpoint_id = _checkpoint_identity_sets(conn)
    boundary = _ff_opening_boundary_context(conn)
    quarantined_doprinato_keys = (
        diagnostic_quarantined_doprinato_keys or set()
    )
    anomalies: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    legacy_operations: list[dict[str, Any]] = []
    legacy_doprinato: list[dict[str, Any]] = []
    movement_rows: list[dict[str, Any]] = []

    duplicate_rows = conn.execute(
        """
        SELECT source_type,source_key,COUNT(*) row_count,
               GROUP_CONCAT(operation_id) operation_ids
        FROM sheet_vitrina_v1_ff_stock_operations
        GROUP BY source_type,source_key HAVING COUNT(*)>1
        ORDER BY source_type,source_key
        """
    ).fetchall()
    for row in duplicate_rows:
        blockers.append(
            {
                "blocker_class": "duplicate_operation_source_identity",
                "operation_id": str(row["operation_ids"] or ""),
                "supply_id": "",
                "business_date": "",
                "nm_id": None,
                "raw_quantities": {"duplicate_count": int(row["row_count"] or 0)},
                "discrepancy": str(int(row["row_count"] or 0) - 1),
                "source_identity": f"{row['source_type']}:{row['source_key']}",
                "checkpoint_identity": checkpoint_id,
                "cost_source": None,
                "classification": "duplicate_operation_source_identity",
                "eligible": False,
                "reason": "duplicate identity is never eligible for cutover absorption",
            }
        )

    raw_operations = conn.execute(
        """
        SELECT * FROM sheet_vitrina_v1_ff_stock_operations
        WHERE operation_type='auto_writeoff' AND source_type='wb_supply'
        ORDER BY created_at,operation_id
        """
    ).fetchall()
    for raw in raw_operations:
        operation = dict(raw)
        operation_id = str(operation.get("operation_id") or "")
        source_key = str(operation.get("source_key") or "")
        supply_id = str(operation.get("source_object_id") or "")
        try:
            resolution = resolve_ff_operation_effective_date(conn, operation)
            supply = _load_exact_wb_supply_for_operation(conn, operation)
        except CanonicalCostBlocked as exc:
            blockers.append(
                {
                    "blocker_class": exc.code,
                    "operation_id": operation_id,
                    "supply_id": supply_id,
                    "business_date": "",
                    "nm_id": None,
                    "raw_quantities": {},
                    "discrepancy": None,
                    "source_identity": source_key,
                    "checkpoint_identity": checkpoint_id,
                    "cost_source": None,
                    "classification": "unresolved_source_identity_or_business_date",
                    "eligible": False,
                    "reason": "missing or ambiguous identity/date is never eligible",
                    "details": exc.details,
                }
            )
            continue
        normalized = _json_loads(supply.get("normalized_row_json"))
        accepted_by_nm: dict[int, Decimal] = defaultdict(Decimal)
        goods = _goods(supply.get("raw_goods_json"))
        status_id = int(
            normalized.get("status_id")
            or normalized.get("statusID")
            or supply.get("status_id")
            or 0
        )
        if (
            resolution.effective_date >= CUTOVER_DATE
            and status_id == 5
            and not goods
        ):
            blockers.append(
                {
                    "blocker_class": "wb_supply_per_sku_acceptance_evidence_missing",
                    "operation_id": operation_id,
                    "supply_id": str(supply.get("supply_id") or supply_id),
                    "business_date": resolution.effective_date,
                    "nm_id": None,
                    "raw_quantities": {},
                    "discrepancy": None,
                    "source_identity": source_key,
                    "checkpoint_identity": checkpoint_id,
                    "cost_source": None,
                    "classification": "missing_exact_per_sku_acceptance_evidence",
                    "eligible": False,
                    "reason": "final accepted supply requires exact persisted goods evidence",
                }
            )
        for item in goods:
            nm_id = int(item.get("nmID") or item.get("nmId") or item.get("nm_id") or 0)
            if nm_id > 0:
                accepted_by_nm[nm_id] += _decimal(
                    item.get("acceptedQuantity") or item.get("accepted_quantity") or 0
                )
        line_rows = conn.execute(
            """
            SELECT nm_id,SUM(ABS(MIN(quantity_delta,0))) sent_quantity
            FROM sheet_vitrina_v1_ff_stock_operation_lines
            WHERE operation_id=? GROUP BY nm_id ORDER BY nm_id
            """,
            (operation_id,),
        ).fetchall()
        sent_by_nm = {
            int(row["nm_id"]): _decimal(row["sent_quantity"]) for row in line_rows
        }
        line_set_rows = [
            {
                "line_no": int(row["line_no"] or 0),
                "nm_id": int(row["nm_id"] or 0),
                "quantity_delta": _text(_decimal(row["quantity_delta"])),
            }
            for row in conn.execute(
                "SELECT line_no,nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
                (operation_id,),
            ).fetchall()
        ]
        accepted_line_set_rows = [
            {
                "nm_id": nm_id,
                "accepted_quantity": _text(quantity),
            }
            for nm_id, quantity in sorted(accepted_by_nm.items())
        ]
        cache_key = str(supply.get("cache_key") or "")
        checkpoint = {
            "cache_key": cache_key in cache_keys,
            "source_key": source_key in source_keys,
            "supply_id": str(supply.get("supply_id") or "") in supply_ids,
        }
        fully_checkpoint_matched = all(checkpoint.values())
        operation_row = {
            "operation_id": operation_id,
            "supply_id": str(supply.get("supply_id") or supply_id),
            "business_date": resolution.effective_date,
            "created_at": str(operation.get("created_at") or ""),
            "date_provenance": resolution.provenance,
            "source_key": source_key,
            "checkpoint_membership": checkpoint,
            "sent_quantity": _text(sum(sent_by_nm.values(), ZERO)),
            "raw_accepted_quantity": _text(sum(accepted_by_nm.values(), ZERO)),
            "line_count": len(sent_by_nm),
            "line_set_fingerprint": "sha256:" + _stable_hash(line_set_rows),
            "accepted_line_set_fingerprint": "sha256:"
            + _stable_hash(accepted_line_set_rows),
            "sent_lines": line_set_rows,
            "accepted_lines": accepted_line_set_rows,
            "warehouse": str(
                normalized.get("warehouse_name")
                or normalized.get("warehouseName")
                or supply.get("warehouse_id")
                or ""
            ),
            "destination": str(
                normalized.get("destination_name")
                or normalized.get("target_warehouse_name")
                or normalized.get("warehouse_name")
                or supply.get("warehouse_id")
                or ""
            ),
            "underaccepted_quantity": _text(sum(
                (max(sent_by_nm.get(nm_id, ZERO) - accepted_by_nm.get(nm_id, ZERO), ZERO)
                 for nm_id in set(sent_by_nm) | set(accepted_by_nm)),
                ZERO,
            )),
            "overaccepted_surplus_quantity": _text(sum(
                (max(accepted_by_nm.get(nm_id, ZERO) - sent_by_nm.get(nm_id, ZERO), ZERO)
                 for nm_id in set(sent_by_nm) | set(accepted_by_nm)),
                ZERO,
            )),
        }
        operation_row["net_quantity_discrepancy"] = _text(
            _decimal(operation_row["underaccepted_quantity"])
            - _decimal(operation_row["overaccepted_surplus_quantity"])
        )
        operation_row["supply_invariant_ok"] = (
            sum(sent_by_nm.values(), ZERO) - sum(accepted_by_nm.values(), ZERO)
            == _decimal(operation_row["underaccepted_quantity"])
            - _decimal(operation_row["overaccepted_surplus_quantity"])
        )
        surplus_total = _decimal(operation_row["overaccepted_surplus_quantity"])
        shortage_total = _decimal(operation_row["underaccepted_quantity"])
        sent_total = _decimal(operation_row["sent_quantity"])
        accepted_total = _decimal(operation_row["raw_accepted_quantity"])
        missing_cost_nm_ids = sorted(
            nm_id for nm_id in set(sent_by_nm) | set(accepted_by_nm)
            if baseline_costs.get(nm_id) is None
            or _decimal(
                (baseline_costs.get(nm_id) or {}).get(
                    "recognized_unit_cost_rub"
                )
            ) <= ZERO
        )
        legacy_cost_rows = [
            dict(row) for row in conn.execute(
                """
                SELECT nm_id,accepted_qty,our_wb_unit_cost_rub,source_status,
                       supplier_ff_cost_layer_id,supplier_ff_cost_layer_line_id,
                       inputs_hash
                FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE wb_supply_id=? AND is_current=1 ORDER BY nm_id
                """,
                (str(supply.get("supply_id") or supply_id),),
            ).fetchall()
        ]
        recognized_pool = sum(
            (
                sent_by_nm.get(nm_id, ZERO)
                * _decimal(
                    (baseline_costs.get(nm_id) or {}).get(
                        "recognized_unit_cost_rub"
                    )
                )
                for nm_id in sent_by_nm
            ),
            ZERO,
        )
        paid_pool = sum(
            (
                sent_by_nm.get(nm_id, ZERO)
                * _decimal(
                    (baseline_costs.get(nm_id) or {}).get(
                        "paid_unit_cost_rub"
                    )
                )
                for nm_id in sent_by_nm
            ),
            ZERO,
        )
        operation_row["classification"] = (
            "legacy_audit_only"
            if resolution.effective_date < CUTOVER_DATE
            else "post_cutover_movement"
        )
        operation_row["evidence_fingerprint"] = "sha256:" + _stable_hash(
            {
                "operation_id": operation_id,
                "supply_id": operation_row["supply_id"],
                "source_key": source_key,
                "business_date": resolution.effective_date,
                "warehouse": operation_row["warehouse"],
                "destination": operation_row["destination"],
                "sent_lines": line_set_rows,
                "accepted_lines": accepted_line_set_rows,
            }
        )
        normalization_checks = {
            "business_date_in_allowed_window": (
                "2026-07-02" <= resolution.effective_date <= "2026-07-12"
            ),
            "accepted_skus_subset_of_sent": set(accepted_by_nm) <= set(sent_by_nm),
            "aggregate_accepted_not_above_sent": accepted_total <= sent_total,
            "ff_debit_quantity_present": sent_total > ZERO,
            "legacy_supply_cost_evidence_present": bool(legacy_cost_rows),
            "missing_cost_nm_ids": missing_cost_nm_ids,
            "normalized_quantity_within_500": surplus_total <= Decimal("500"),
        }
        operation_row["postcutover_normalization"] = {
            "policy": POSTCUTOVER_NORMALIZATION_POLICY,
            "surplus_quantity": _text(surplus_total),
            "shortage_quantity": _text(shortage_total),
            "effective_accepted_quantity": _text(min(accepted_total, sent_total)),
            "remaining_underaccepted_quantity": _text(
                sent_total - min(accepted_total, sent_total)
            ),
            "recognized_cost_pool_rub": _text(recognized_pool),
            "paid_cost_pool_rub": _text(paid_pool),
            "recognized_weighted_unit_cost_rub": _text(
                _safe_ratio(recognized_pool, sent_total)
            ),
            "paid_weighted_unit_cost_rub": _text(
                _safe_ratio(paid_pool, sent_total)
            ),
            "legacy_supply_cost_rows": legacy_cost_rows,
            "checks": normalization_checks,
            "manifest_match": _postcutover_manifest_matches(operation_row),
        }
        operations.append(operation_row)
        if resolution.effective_date < CUTOVER_DATE:
            legacy_operations.append(operation_row)
            continue
        canonical_replay_date = _canonical_ff_operation_effective_date(
            conn, operation, boundary=boundary
        )
        for nm_id in sorted(set(sent_by_nm) | set(accepted_by_nm)):
            sent = sent_by_nm.get(nm_id, ZERO)
            raw_accepted = accepted_by_nm.get(nm_id, ZERO)
            applied = min(sent, raw_accepted)
            surplus = max(raw_accepted - sent, ZERO)
            open_quantity = max(sent - raw_accepted, ZERO)
            # Only a finalized operation that the strict physical replay
            # materializes may provide an outstanding candidate.  Checkpoint
            # audit history must not make an otherwise orphan doprinato look
            # matched during source preflight.
            if canonical_replay_date is not None and status_id == 5:
                movement_rows.append({
                    "operation_id": operation_id,
                    "supply_id": str(supply.get("supply_id") or supply_id),
                    "business_date": resolution.effective_date,
                    "nm_id": nm_id,
                    "sent_quantity": sent,
                    "raw_accepted_quantity": raw_accepted,
                    "accepted_applied_quantity": applied,
                    "open_quantity": open_quantity,
                    "overaccepted_surplus": surplus,
                    "warehouse": str(
                        normalized.get("warehouse_name")
                        or normalized.get("warehouseName")
                        or supply.get("warehouse_id")
                        or ""
                    ),
                    "destination": str(
                        normalized.get("destination_name")
                        or normalized.get("target_warehouse_name")
                        or normalized.get("warehouse_name")
                        or supply.get("warehouse_id")
                        or ""
                    ),
                    "accepted_date": _wb_accepted_date(normalized, supply),
                    "checkpoint_matched": fully_checkpoint_matched,
                })
            if surplus <= ZERO:
                continue
            cost = baseline_costs.get(nm_id)
            guard_failures: list[str] = []
            if not _is_integer_quantity(surplus):
                guard_failures.append("discrepancy is not an integer quantity")
            if not normalization_checks["business_date_in_allowed_window"]:
                guard_failures.append("business date is outside 2026-07-02..2026-07-12")
            if nm_id <= 0 or nm_id not in sent_by_nm:
                guard_failures.append("exact operation-line SKU identity is missing")
            if cost is None:
                guard_failures.append("permitted canonical baseline cost is missing")
            elif _decimal(cost.get("recognized_unit_cost_rub")) <= ZERO:
                guard_failures.append("canonical baseline cost is not positive")
            for check_name in (
                "accepted_skus_subset_of_sent",
                "aggregate_accepted_not_above_sent",
                "ff_debit_quantity_present",
                "legacy_supply_cost_evidence_present",
                "normalized_quantity_within_500",
            ):
                if not bool(normalization_checks[check_name]):
                    guard_failures.append(check_name)
            if missing_cost_nm_ids:
                guard_failures.append("one or more supply SKU costs are missing")
            if not _postcutover_manifest_matches(operation_row):
                guard_failures.append("operation is absent or changed in exact normalization manifest")
            prelim_ok = not guard_failures
            anomalies.append(
                {
                    "blocker_class": "accepted_quantity_exceeds_sent",
                    "operation_id": operation_id,
                    "supply_id": str(supply.get("supply_id") or supply_id),
                    "business_date": resolution.effective_date,
                    "nm_id": nm_id,
                    "raw_quantities": {
                        "sent": _text(sent),
                        "raw_accepted": _text(raw_accepted),
                        "accepted_applied_to_movement": _text(applied),
                        "underaccepted": _text(open_quantity),
                        "overaccepted_surplus": _text(surplus),
                    },
                    "discrepancy": _text(surplus),
                    "source_identity": source_key,
                    "checkpoint_identity": {
                        "checkpoint_id": checkpoint_id,
                        "membership": checkpoint,
                    },
                    "cost_source": (
                        {
                            key: (_text(value) if isinstance(value, Decimal) else value)
                            for key, value in cost.items()
                        }
                        if cost is not None else None
                    ),
                    "classification": (
                        "postcutover_source_normalized"
                        if prelim_ok else "postcutover_normalization_candidate"
                    ),
                    "eligible": prelim_ok,
                    "policy_guard_failures": guard_failures,
                    "reason": (
                        "exact manifest normalization preserves aggregate supply quantity and capital"
                        if prelim_ok else "; ".join(guard_failures)
                    ),
                    "gross_discrepancy": surplus,
                    "net_discrepancy": raw_accepted - sent,
                }
            )

    # Reconcile doprinato against exact/direct then strict FIFO opening layers.
    outstanding = [dict(item) for item in movement_rows if item["open_quantity"] > ZERO]
    doprinato = sorted(
        (item for item in _wb_supply_cache_evidence(conn, date_to=date_to) if item["is_doprinato"]),
        key=lambda item: (item["accepted_date"], item["supply_id"], item["nm_id"]),
    )
    manifest_entries = _unmatched_doprinato_manifest_entries()
    manifest_source_keys = {
        (str(item["supply_id"]), int(item["nm_id"]))
        for item in doprinato
        if _unmatched_doprinato_manifest_entry(
            str(item["supply_id"]), int(item["nm_id"])
        ) is not None
    }
    manifest_supply_ids = {
        str(item["expected"]["supply_id"]) for item in manifest_entries
    }
    persisted_manifest_supply_count = sum(
        int(
            conn.execute(
                "SELECT EXISTS(SELECT 1 FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=?)",
                (supply_id,),
            ).fetchone()[0]
        )
        for supply_id in manifest_supply_ids
    )
    matched_absorption_identities: set[tuple[str, str, int]] = set()
    for fact in doprinato:
        fact_key = (str(fact["supply_id"]), int(fact["nm_id"]))
        if fact_key in quarantined_doprinato_keys:
            # Disposable diagnostic-only exclusion.  Raw persisted evidence
            # stays untouched and the strict apply path cannot populate this
            # set.
            continue
        if str(fact["accepted_date"] or "") < CUTOVER_DATE:
            legacy_doprinato.append(
                {
                    "supply_id": str(fact["supply_id"]),
                    "nm_id": int(fact["nm_id"]),
                    "business_date": str(fact["accepted_date"]),
                    "accepted_quantity": _text(
                        _decimal(fact["accepted_quantity"])
                    ),
                    "source_identity": str(fact["source_identity"]),
                    "classification": "legacy_audit_only",
                }
            )
            continue
        manifest_decision = _unmatched_doprinato_manifest_decision(
            conn,
            fact,
            baseline_cost_references=baseline_cost_references,
        )
        if manifest_decision is not None:
            expected = dict(manifest_decision["expected"])
            reference = dict(manifest_decision.get("cost_reference") or {})
            quantity = _decimal(fact["accepted_quantity"])
            guard_failures = [
                f"manifest field drift: {key}"
                for key in sorted(manifest_decision["mismatches"])
            ]
            matched = bool(manifest_decision["matched"])
            if matched:
                matched_absorption_identities.add(
                    (
                        str(manifest_decision["policy"]),
                        str(fact["supply_id"]),
                        int(fact["nm_id"]),
                    )
                )
            anomalies.append(
                {
                    "blocker_class": "doprinato_unmatched_surplus",
                    "operation_id": "",
                    "supply_id": str(fact["supply_id"]),
                    "business_date": str(fact["accepted_date"]),
                    "nm_id": int(fact["nm_id"]),
                    "raw_quantities": {
                        "raw_doprinato": _text(quantity),
                        "absorbed_source_evidence": _text(quantity),
                        "movement_quantity_delta": "0",
                        "recognized_capital_delta_rub": "0",
                        "paid_capital_delta_rub": "0",
                        "confirmation_quantity_delta": "0",
                        "underaccepted_quantity_delta": "0",
                    },
                    "doprinato_evidence": {
                        **_doprinato_fact_fingerprint_payload(fact),
                        "raw_row_line_fingerprint": str(
                            fact["raw_row_line_fingerprint"]
                        ),
                        "raw_source_row_fingerprint": str(
                            fact["raw_source_row_fingerprint"]
                        ),
                        "raw_source_line_fingerprint": str(
                            fact["raw_source_line_fingerprint"]
                        ),
                        "semantic_evidence_fingerprint": str(
                            fact["semantic_evidence_fingerprint"]
                        ),
                        "source_status": str(fact["source_status"]),
                    },
                    "discrepancy": _text(quantity),
                    "source_identity": str(fact["source_identity"]),
                    "checkpoint_identity": checkpoint_id,
                    "cost_source": {
                        "recognized_unit_cost_rub": expected[
                            "recognized_reference_unit_cost_rub"
                        ],
                        "paid_unit_cost_rub": expected[
                            "paid_reference_unit_cost_rub"
                        ],
                        "cost_reference_stage": expected[
                            "cost_reference_stage"
                        ],
                        "baseline_source_type": reference.get("source_type"),
                        "baseline_source_identity": reference.get(
                            "source_identity"
                        ),
                        "baseline_source_date": reference.get("source_date"),
                        "baseline_line_fingerprint": reference.get(
                            "line_fingerprint"
                        ),
                        "baseline_fingerprint": reference.get(
                            "baseline_fingerprint"
                        ),
                    },
                    "classification": (
                        UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION
                        if matched
                        else "unmatched_doprinato_absorption_manifest_drift"
                    ),
                    "source_quality": (
                        UNMATCHED_DOPRINATO_ABSORPTION_SOURCE_QUALITY
                        if matched else "manifest_drift"
                    ),
                    "eligible": matched,
                    "policy": str(manifest_decision["policy"]),
                    "policy_guard_failures": guard_failures,
                    "manifest_decision": manifest_decision,
                    "reason": (
                        str(manifest_decision["reason"])
                        if matched else "; ".join(guard_failures)
                    ),
                    "gross_discrepancy": quantity,
                    "net_discrepancy": ZERO,
                }
            )
            # Exact manifest evidence is audit-only and must never close a
            # direct/FIFO layer.  Drift is also isolated and fail-closed.
            continue
        remaining = _decimal(fact["accepted_quantity"])
        candidates = [
            item for item in outstanding
            if item["nm_id"] == fact["nm_id"]
            and _decimal(item["open_quantity"]) > ZERO
            and (item["accepted_date"] or item["business_date"]) <= fact["accepted_date"]
            and (
                (fact["original_supply_id"] and item["supply_id"] == fact["original_supply_id"])
                or (
                    not fact["original_supply_id"]
                    and item["warehouse"] == fact["warehouse"]
                    and item["destination"] == fact["destination"]
                )
            )
        ]
        candidates.sort(key=lambda item: (item["accepted_date"] or item["business_date"], item["supply_id"]))
        for candidate in candidates:
            if remaining <= ZERO:
                break
            close = min(remaining, _decimal(candidate["open_quantity"]))
            candidate["open_quantity"] = _decimal(candidate["open_quantity"]) - close
            remaining -= close
        if remaining <= ZERO:
            continue
        nm_id = int(fact["nm_id"])
        cost = baseline_costs.get(nm_id)
        exact_source = bool(str(fact["supply_id"] or ""))
        checkpoint_match = any(
            item["checkpoint_matched"]
            and item["nm_id"] == nm_id
            and item["business_date"] <= str(fact["accepted_date"] or "")
            and (
                (
                    fact["original_supply_id"]
                    and item["supply_id"] == fact["original_supply_id"]
                )
                or (
                    not fact["original_supply_id"]
                    and item["warehouse"] == fact["warehouse"]
                    and item["destination"] == fact["destination"]
                )
            )
            for item in movement_rows
        )
        guard_failures = ["post-cutover unmatched doprinato is never normalized"]
        if not _is_integer_quantity(remaining):
            guard_failures.append("discrepancy is not an integer quantity")
        if not exact_source:
            guard_failures.append("exact doprinato source identity is missing")
        if not checkpoint_match:
            guard_failures.append("no exact/direct or strict route checkpoint identity")
        if cost is None:
            guard_failures.append("permitted canonical baseline cost is missing")
        elif _decimal(cost.get("recognized_unit_cost_rub")) <= ZERO:
            guard_failures.append("canonical baseline cost is not positive")
        anomalies.append(
            {
                "blocker_class": "doprinato_unmatched_surplus",
                "operation_id": "",
                "supply_id": str(fact["supply_id"]),
                "business_date": str(fact["accepted_date"]),
                "nm_id": nm_id,
                "raw_quantities": {
                    "raw_doprinato": _text(_decimal(fact["accepted_quantity"])),
                    "unmatched_surplus": _text(remaining),
                },
                "doprinato_evidence": {
                    **_doprinato_fact_fingerprint_payload(fact),
                    "raw_row_line_fingerprint": str(
                        fact.get("raw_row_line_fingerprint") or ""
                    ),
                    "raw_source_row_fingerprint": str(
                        fact.get("raw_source_row_fingerprint") or ""
                    ),
                    "raw_source_line_fingerprint": str(
                        fact.get("raw_source_line_fingerprint") or ""
                    ),
                    "semantic_evidence_fingerprint": str(
                        fact.get("semantic_evidence_fingerprint") or ""
                    ),
                    "source_status": str(fact.get("source_status") or ""),
                },
                "discrepancy": _text(remaining),
                "source_identity": str(fact["source_identity"]),
                "checkpoint_identity": checkpoint_id,
                "cost_source": (
                    {
                        key: (_text(value) if isinstance(value, Decimal) else value)
                        for key, value in cost.items()
                    }
                    if cost is not None else None
                ),
                "classification": "blocked_doprinato_unmatched_surplus",
                "eligible": False,
                "policy_guard_failures": guard_failures,
                "reason": "; ".join(guard_failures),
                "gross_discrepancy": remaining,
                "net_discrepancy": remaining,
            }
        )

    if persisted_manifest_supply_count:
        for manifest_entry in manifest_entries:
            expected = manifest_entry["expected"]
            policy = str(manifest_entry["policy"])
            supply_id = str(expected["supply_id"])
            nm_id = int(expected["nm_id"])
            if (policy, supply_id, nm_id) in matched_absorption_identities:
                continue
            if str(expected["business_date"]) > date_to:
                continue
            if (supply_id, nm_id) in manifest_source_keys:
                # Present-but-drifted evidence already emitted its exact
                # mismatch record above.
                continue
            blockers.append(
                {
                    "blocker_class": "unmatched_doprinato_manifest_source_missing",
                    "operation_id": "",
                    "supply_id": supply_id,
                    "business_date": str(expected["business_date"]),
                    "nm_id": nm_id,
                    "raw_quantities": {},
                    "discrepancy": str(expected["quantity"]),
                    "source_identity": str(expected["source_identity"]),
                    "checkpoint_identity": checkpoint_id,
                    "cost_source": None,
                    "classification": "unmatched_doprinato_absorption_manifest_drift",
                    "eligible": False,
                    "policy": policy,
                    "reason": "approved exact manifest source row is missing",
                }
            )

    # Replay the physical opening boundary chronologically.  Checkpoint rows
    # and their exact compensations are audit-only; the activation receipt is
    # the FF opening snapshot at cutover.
    replay_balance: dict[int, Decimal] = defaultdict(Decimal)
    authoritative_balance: dict[int, Decimal] = defaultdict(Decimal)
    for row in conn.execute(
        "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines"
    ).fetchall():
        authoritative_balance[int(row["nm_id"])] += _decimal(row["quantity_delta"])
    authoritative_as_of = defaultdict(Decimal, authoritative_balance)
    canonical_operations: list[tuple[str, dict[str, Any]]] = []
    replay_operations = [
        dict(row) for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_stock_operations ORDER BY created_at,operation_id"
        ).fetchall()
    ]
    for operation in replay_operations:
        try:
            effective = _canonical_ff_operation_effective_date(
                conn, operation, boundary=boundary
            )
        except CanonicalCostBlocked as exc:
            if not any(
                item.get("operation_id") == str(operation.get("operation_id") or "")
                and item.get("blocker_class") == exc.code
                for item in blockers
            ):
                blockers.append(
                    {
                        "blocker_class": exc.code,
                        "operation_id": str(operation.get("operation_id") or ""),
                        "supply_id": str(operation.get("source_object_id") or ""),
                        "business_date": "",
                        "nm_id": None,
                        "raw_quantities": {},
                        "discrepancy": None,
                        "source_identity": str(operation.get("source_key") or ""),
                        "checkpoint_identity": checkpoint_id,
                        "cost_source": None,
                        "classification": "unresolved_ff_operation_effective_date",
                        "eligible": False,
                        "reason": "unresolved operation date is never eligible",
                        "details": exc.details,
                    }
                )
            continue
        if effective and effective <= date_to:
            canonical_operations.append((effective, operation))
        elif effective and effective > date_to:
            for line in conn.execute(
                "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=?",
                (str(operation.get("operation_id") or ""),),
            ).fetchall():
                authoritative_as_of[int(line["nm_id"])] -= _decimal(
                    line["quantity_delta"]
                )
    canonical_operations.sort(
        key=lambda item: (
            item[0], str(item[1].get("created_at") or ""),
            str(item[1].get("operation_id") or ""),
        )
    )
    for effective, operation in canonical_operations:
        operation_id = str(operation.get("operation_id") or "")
        for line in conn.execute(
            "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
            (operation_id,),
        ).fetchall():
            nm_id = int(line["nm_id"])
            delta = _decimal(line["quantity_delta"])
            if delta < ZERO and replay_balance[nm_id] + delta < ZERO:
                residual = abs(replay_balance[nm_id] + delta)
                cost = baseline_costs.get(nm_id)
                source_key = str(operation.get("source_key") or "")
                supply_id = str(operation.get("source_object_id") or "")
                source_identity = source_key.removeprefix("wb_supply_debit:")
                checkpoint_match = (
                    source_key in source_keys
                    and supply_id in supply_ids
                    and source_identity in cache_keys
                )
                guard_failures = [
                    "post-cutover FF replay deficit is never normalized"
                ]
                if not _is_integer_quantity(residual):
                    guard_failures.append("discrepancy is not an integer quantity")
                if not checkpoint_match:
                    guard_failures.append("source is not fully checkpoint matched")
                if cost is None:
                    guard_failures.append("permitted canonical baseline cost is missing")
                elif _decimal(cost.get("recognized_unit_cost_rub")) <= ZERO:
                    guard_failures.append("canonical baseline cost is not positive")
                anomalies.append(
                    {
                        "blocker_class": "ff_debit_without_available_opening_or_movement_cost_inventory",
                        "operation_id": operation_id,
                        "supply_id": supply_id,
                        "business_date": effective,
                        "nm_id": nm_id,
                        "raw_quantities": {
                            "available": _text(replay_balance[nm_id]),
                            "debit": _text(abs(delta)),
                            "residual": _text(residual),
                        },
                        "discrepancy": _text(residual),
                        "source_identity": source_key,
                        "checkpoint_identity": checkpoint_id,
                        "cost_source": (
                            {
                                key: (_text(value) if isinstance(value, Decimal) else value)
                                for key, value in cost.items()
                            }
                            if cost is not None else None
                        ),
                        "classification": "blocked_ff_replay_inventory_deficit",
                        "eligible": False,
                        "policy_guard_failures": guard_failures,
                        "reason": "; ".join(guard_failures),
                        "gross_discrepancy": residual,
                        "net_discrepancy": -residual,
                    }
                )
                replay_balance[nm_id] = ZERO
            else:
                replay_balance[nm_id] += delta
    for nm_id, quantity in sorted(authoritative_balance.items()):
        if quantity < ZERO:
            blockers.append(
                {
                    "blocker_class": "negative_current_ff_physical_balance",
                    "operation_id": "",
                    "supply_id": "",
                    "business_date": date_to,
                    "nm_id": nm_id,
                    "raw_quantities": {"current_ff_quantity": _text(quantity)},
                    "discrepancy": _text(abs(quantity)),
                    "source_identity": "ff_stock_ledger",
                    "checkpoint_identity": checkpoint_id,
                    "cost_source": None,
                    "classification": "negative_current_physical_balance",
                    "eligible": False,
                    "reason": "negative current physical balance is never eligible",
                }
            )
    negative_outstanding_rows: list[sqlite3.Row] = []
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_canonical_cost_wb_outstanding_layers'"
    ).fetchone():
        negative_outstanding_rows = conn.execute(
            """
            SELECT outstanding_layer_id,original_supply_id,nm_id,open_quantity,
                   recognized_unit_cost_rub,paid_unit_cost_rub
            FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers
            WHERE is_current=1 AND (
                (open_quantity+0)<0
                OR (recognized_unit_cost_rub+0)<=0
                OR (paid_unit_cost_rub+0)<0
            )
            ORDER BY original_supply_id,nm_id
            """
        ).fetchall()
    for row in negative_outstanding_rows:
        blockers.append(
            {
                "blocker_class": "persisted_outstanding_quantity_or_cost_invalid",
                "operation_id": "",
                "supply_id": str(row["original_supply_id"] or ""),
                "business_date": date_to,
                "nm_id": int(row["nm_id"]),
                "raw_quantities": {
                    "open_quantity": str(row["open_quantity"]),
                    "recognized_unit_cost_rub": str(row["recognized_unit_cost_rub"]),
                    "paid_unit_cost_rub": str(row["paid_unit_cost_rub"]),
                },
                "discrepancy": None,
                "source_identity": str(row["outstanding_layer_id"] or ""),
                "checkpoint_identity": checkpoint_id,
                "cost_source": None,
                "classification": "invalid_persisted_outstanding",
                "eligible": False,
                "reason": "negative outstanding or nonpositive recognized cost is never eligible",
            }
        )
    replay_mismatch = {
        nm_id: {
            "authoritative": _text(authoritative_as_of.get(nm_id, ZERO)),
            "canonical_replay": _text(replay_balance.get(nm_id, ZERO)),
        }
        for nm_id in sorted(set(authoritative_as_of) | set(replay_balance))
        if authoritative_as_of.get(nm_id, ZERO) != replay_balance.get(nm_id, ZERO)
    }
    if replay_mismatch:
        blockers.append(
            {
                "blocker_class": "opening_ff_snapshot_event_replay_mismatch",
                "operation_id": "",
                "supply_id": "",
                "business_date": CUTOVER_DATE,
                "nm_id": None,
                "raw_quantities": replay_mismatch,
                "discrepancy": None,
                "source_identity": "ff_stock_ledger:opening_boundary",
                "checkpoint_identity": checkpoint_id,
                "cost_source": None,
                "classification": "opening_snapshot_event_replay_mismatch",
                "eligible": False,
                "reason": "opening/event replay reconciliation is fail closed",
            }
        )

    gross = sum((_decimal(item["gross_discrepancy"]) for item in anomalies), ZERO)
    candidate_net = sum(
        (_decimal(item["net_discrepancy"]) for item in anomalies), ZERO
    )
    candidate_operation_ids = {
        str(item.get("operation_id") or item.get("supply_id") or "")
        for item in anomalies
    }
    candidate_nm_ids = {int(item["nm_id"]) for item in anomalies}
    for item in anomalies:
        if gross > Decimal("500"):
            item["eligible"] = False
            item["reason"] = (
                str(item["reason"])
                + "; total post-cutover normalization exceeds 500 units"
            )
        item.pop("gross_discrepancy", None)
        item.pop("net_discrepancy", None)
        item.pop("operation_budget_key", None)
    unresolved = [item for item in anomalies if not item["eligible"]]
    blockers.extend(unresolved)
    eligible = [item for item in anomalies if item["eligible"]]
    eligible_gross = sum((_decimal(item["discrepancy"]) for item in eligible), ZERO)
    recognized_exposure = sum(
        (
            _decimal(item["discrepancy"])
            * _decimal((item.get("cost_source") or {}).get("recognized_unit_cost_rub"))
            for item in eligible
        ),
        ZERO,
    )
    paid_exposure = sum(
        (
            _decimal(item["discrepancy"])
            * _decimal((item.get("cost_source") or {}).get("paid_unit_cost_rub"))
            for item in eligible
        ),
        ZERO,
    )
    checks = {
        "accepted_quantity_exceeds_sent": sum(item["blocker_class"] == "accepted_quantity_exceeds_sent" for item in anomalies),
        "doprinato_unmatched_surplus": sum(item["blocker_class"] == "doprinato_unmatched_surplus" for item in anomalies),
        "ff_debit_without_cost_inventory": sum(
            item["blocker_class"] == "ff_debit_without_available_opening_or_movement_cost_inventory"
            for item in anomalies
        ),
        "legacy_business_date_unresolved": sum("date" in str(item["blocker_class"]) for item in blockers),
        "operation_identity_unresolved": sum("identity" in str(item["blocker_class"]) for item in blockers),
        "pre_cutover_movement_without_baseline_cost": sum(item.get("cost_source") is None for item in anomalies),
        "negative_underaccepted_or_outstanding": len(negative_outstanding_rows),
        "duplicate_acceptance_or_doprinato_closure": 0,
        "duplicate_operation_source_identity": len(duplicate_rows),
        "opening_snapshot_event_replay": {
            "status": "ok" if not replay_mismatch else "mismatch",
            "activation_operation_id": boundary["activation_operation_id"],
            "checkpoint_operation_count": len(boundary["checkpoint_operation_ids"]),
            "compensation_operation_count": len(boundary["compensation_operation_ids"]),
            "authoritative_ff_total_as_of": _text(sum(authoritative_as_of.values(), ZERO)),
            "current_ff_total": _text(sum(authoritative_balance.values(), ZERO)),
            "canonical_replay_total": _text(sum(replay_balance.values(), ZERO)),
        },
        "zero_or_negative_cost": sum(
            item.get("cost_source") is not None
            and _decimal(item["cost_source"].get("recognized_unit_cost_rub")) <= ZERO
            for item in anomalies
        ),
        "incomplete_cost_coverage": sum(item.get("cost_source") is None for item in anomalies),
        "wac_reconciliation_mismatch": "checked_by_candidate_reconciliation",
        "source_protected_pre_cutover_digest_anomaly": "checked_by_backfill_runner",
        "potential_non_idempotency": len(duplicate_rows),
    }
    manifest_reports = [
        _unmatched_doprinato_manifest_report(),
        _unmatched_doprinato_manifest_report_v2(),
    ]
    manifest_report = manifest_reports[0]
    matched_absorptions = [
        item
        for item in eligible
        if item.get("classification")
        == UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION
    ]
    absorption_report = {
        **manifest_report,
        "versions": manifest_reports,
        "version_fingerprints": {
            str(report["policy"]): str(report["manifest_fingerprint"])
            for report in manifest_reports
        },
        "approved_row_count": sum(
            int(report["row_count"]) for report in manifest_reports
        ),
        "approved_supply_count": len(
            {
                str(entry["expected"]["supply_id"])
                for entry in manifest_entries
            }
        ),
        "approved_sku_count": len(
            {int(entry["expected"]["nm_id"]) for entry in manifest_entries}
        ),
        "approved_unit_count": _text(
            sum(
                (
                    _decimal(entry["expected"]["quantity"])
                    for entry in manifest_entries
                ),
                ZERO,
            )
        ),
        "classification": UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION,
        "source_quality": UNMATCHED_DOPRINATO_ABSORPTION_SOURCE_QUALITY,
        "matched_supply_count": len(
            {str(item["supply_id"]) for item in matched_absorptions}
        ),
        "matched_sku_count": len(
            {int(item["nm_id"]) for item in matched_absorptions}
        ),
        "matched_unit_count": _text(
            sum(
                (_decimal(item["discrepancy"]) for item in matched_absorptions),
                ZERO,
            )
        ),
        "all_rows_match": (
            len(matched_absorptions)
            == sum(
                str(entry["expected"]["business_date"]) <= date_to
                for entry in manifest_entries
            )
        ),
        "movement_quantity_delta": "0",
        "recognized_capital_delta_rub": "0",
        "paid_capital_delta_rub": "0",
        "confirmation_quantity_delta": "0",
        "underaccepted_quantity_delta": "0",
    }
    return {
        "status": "blocked" if blockers else "ok",
        "checkpoint_id": checkpoint_id,
        "operation_count": len(operations),
        "legacy_audit_operation_count": len(legacy_operations),
        "post_cutover_operation_count": len(operations) - len(legacy_operations),
        "legacy_doprinato_count": len(legacy_doprinato),
        "legacy_operations": legacy_operations,
        "legacy_doprinato": legacy_doprinato,
        "diagnostic_quarantined_doprinato": [
            {"supply_id": supply_id, "nm_id": nm_id}
            for supply_id, nm_id in sorted(quarantined_doprinato_keys)
        ],
        "operations": operations,
        "unmatched_doprinato_absorption": absorption_report,
        "anomalies": sorted(
            anomalies,
            key=lambda item: (
                item["business_date"], item["blocker_class"], item["supply_id"],
                int(item["nm_id"] or 0), item["operation_id"],
            ),
        ),
        "unresolved_anomalies": blockers,
        "checks": checks,
        "budget": {
            "candidate_anomaly_count": len(anomalies),
            "candidate_affected_operation_count": len(candidate_operation_ids),
            "candidate_affected_sku_count": len(candidate_nm_ids),
            "candidate_gross_quantity_discrepancy": _text(gross),
            "candidate_net_quantity_discrepancy": _text(candidate_net),
            "eligible_anomaly_count": len(eligible),
            "affected_operation_count": len({item["operation_id"] or item["supply_id"] for item in eligible}),
            "affected_sku_count": len({int(item["nm_id"]) for item in eligible}),
            "gross_quantity_discrepancy": _text(eligible_gross),
            "net_quantity_discrepancy": _text(sum(
                (
                    _decimal(item["raw_quantities"].get("raw_accepted"))
                    - _decimal(item["raw_quantities"].get("sent"))
                    if item["blocker_class"] == "accepted_quantity_exceeds_sent"
                    else _decimal(item["discrepancy"])
                    for item in eligible
                ),
                ZERO,
            )),
            "estimated_recognized_capital_exposure_rub": _text(recognized_exposure),
            "estimated_paid_capital_exposure_rub": _text(paid_exposure),
            "limit_quantity": "500",
            "remaining_quantity": _text(Decimal("500") - gross),
            "over_budget_quantity": _text(
                max(gross - Decimal("500"), ZERO)
            ),
        },
    }


def _eligible_anomaly_index(report: Mapping[str, Any]) -> dict[tuple[str, str, int], Mapping[str, Any]]:
    return {
        (
            str(item["blocker_class"]),
            str(item.get("operation_id") or item.get("supply_id") or ""),
            int(item["nm_id"]),
        ): item
        for item in report.get("anomalies") or []
        if bool(item.get("eligible"))
    }


def _postcutover_manifest_matches(operation: Mapping[str, Any]) -> bool:
    operation_id = str(operation.get("operation_id") or "")
    expected = POSTCUTOVER_NORMALIZATION_MANIFEST.get(operation_id)
    if expected is None:
        return False
    actual = {
        "operation_id": operation_id,
        "supply_id": str(operation.get("supply_id") or ""),
        "source_key": str(operation.get("source_key") or ""),
        "business_date": str(operation.get("business_date") or ""),
        "line_set_fingerprint": str(operation.get("line_set_fingerprint") or ""),
        "accepted_line_set_fingerprint": str(
            operation.get("accepted_line_set_fingerprint") or ""
        ),
        "evidence_fingerprint": str(operation.get("evidence_fingerprint") or ""),
    }
    return actual == expected


def _doprinato_fact_fingerprint_payload(
    fact: Mapping[str, Any],
) -> dict[str, Any]:
    """Stable semantic projection of one persisted doprinato source line."""

    return {
        "supply_id": str(fact.get("supply_id") or ""),
        "accepted_date": str(fact.get("accepted_date") or ""),
        "nm_id": int(fact.get("nm_id") or 0),
        "warehouse": str(fact.get("warehouse") or ""),
        "destination": str(fact.get("destination") or ""),
        "accepted_quantity": _text(_decimal(fact.get("accepted_quantity"))),
        "original_supply_id": str(fact.get("original_supply_id") or ""),
        "is_doprinato": bool(fact.get("is_doprinato")),
        "is_final_accepted": bool(fact.get("is_final_accepted")),
        "source_identity": str(fact.get("source_identity") or ""),
    }


def _wb_supply_raw_row_fingerprint_payload(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Immutable persisted source fields, excluding refresh-only timestamps."""

    return {
        key: row.get(key)
        for key in (
            "supply_id",
            "cache_key",
            "normalized_row_json",
            "raw_goods_json",
            "warehouse_id",
            "status_id",
            "quantity_for_size_filter",
            "fact_date",
            "supply_date",
            "updated_date",
        )
    }


def _wb_supply_raw_row_line_fingerprints(
    row: Mapping[str, Any], line: Mapping[str, Any]
) -> dict[str, str]:
    row_fingerprint = "sha256:" + _stable_hash(
        _wb_supply_raw_row_fingerprint_payload(row)
    )
    line_fingerprint = "sha256:" + _stable_hash(dict(line))
    return {
        "raw_source_row_fingerprint": row_fingerprint,
        "raw_source_line_fingerprint": line_fingerprint,
        "raw_row_line_fingerprint": "sha256:"
        + _stable_hash(
            {
                "row_fingerprint": row_fingerprint,
                "line_fingerprint": line_fingerprint,
            }
        ),
    }


def _unmatched_doprinato_manifest_report() -> dict[str, Any]:
    rows = [
        dict(UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST[key])
        for key in sorted(UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST)
    ]
    payload = {
        "policy": UNMATCHED_DOPRINATO_ABSORPTION_POLICY,
        "diagnostic_fingerprint": UNMATCHED_DOPRINATO_DIAGNOSTIC_FINGERPRINT,
        "reason": UNMATCHED_DOPRINATO_ABSORPTION_REASON,
        "approval_date": UNMATCHED_DOPRINATO_ABSORPTION_APPROVAL_DATE,
        "rows": rows,
    }
    recognized = sum(
        (
            _decimal(row["quantity"])
            * _decimal(row["recognized_reference_unit_cost_rub"])
            for row in rows
        ),
        ZERO,
    )
    paid = sum(
        (
            _decimal(row["quantity"])
            * _decimal(row["paid_reference_unit_cost_rub"])
            for row in rows
        ),
        ZERO,
    )
    return {
        **payload,
        "manifest_fingerprint": "sha256:" + _stable_hash(payload),
        "row_count": len(rows),
        "supply_count": len(rows),
        "sku_count": len({int(row["nm_id"]) for row in rows}),
        "unit_count": _text(
            sum((_decimal(row["quantity"]) for row in rows), ZERO)
        ),
        "recognized_reference_exposure_rub": _text(recognized),
        "paid_reference_exposure_rub": _text(paid),
    }


def _unmatched_doprinato_manifest_report_v2() -> dict[str, Any]:
    """Report the second approval without changing V1's payload/fingerprint."""

    rows = [
        dict(UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2[key])
        for key in sorted(UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2)
    ]
    payload = {
        "policy": UNMATCHED_DOPRINATO_ABSORPTION_POLICY_V2,
        "diagnostic_fingerprint": UNMATCHED_DOPRINATO_DIAGNOSTIC_FINGERPRINT_V2,
        "reason": UNMATCHED_DOPRINATO_ABSORPTION_REASON_V2,
        "approval_date": UNMATCHED_DOPRINATO_ABSORPTION_APPROVAL_DATE_V2,
        "rows": rows,
    }
    recognized = sum(
        (
            _decimal(row["quantity"])
            * _decimal(row["recognized_reference_unit_cost_rub"])
            for row in rows
        ),
        ZERO,
    )
    paid = sum(
        (
            _decimal(row["quantity"])
            * _decimal(row["paid_reference_unit_cost_rub"])
            for row in rows
        ),
        ZERO,
    )
    return {
        **payload,
        "manifest_fingerprint": "sha256:" + _stable_hash(payload),
        "row_count": len(rows),
        "supply_count": len({str(row["supply_id"]) for row in rows}),
        "sku_count": len({int(row["nm_id"]) for row in rows}),
        "unit_count": _text(
            sum((_decimal(row["quantity"]) for row in rows), ZERO)
        ),
        "recognized_reference_exposure_rub": _text(recognized),
        "paid_reference_exposure_rub": _text(paid),
    }


def _unmatched_doprinato_manifest_entries() -> list[dict[str, Any]]:
    """Return both exact approvals with policy-specific provenance."""

    entries = [
        {
            "policy": UNMATCHED_DOPRINATO_ABSORPTION_POLICY,
            "reason": UNMATCHED_DOPRINATO_ABSORPTION_REASON,
            "approval_date": UNMATCHED_DOPRINATO_ABSORPTION_APPROVAL_DATE,
            "diagnostic_fingerprint": UNMATCHED_DOPRINATO_DIAGNOSTIC_FINGERPRINT,
            "expected": expected,
        }
        for expected in UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.values()
    ]
    entries.extend(
        {
            "policy": UNMATCHED_DOPRINATO_ABSORPTION_POLICY_V2,
            "reason": UNMATCHED_DOPRINATO_ABSORPTION_REASON_V2,
            "approval_date": UNMATCHED_DOPRINATO_ABSORPTION_APPROVAL_DATE_V2,
            "diagnostic_fingerprint": UNMATCHED_DOPRINATO_DIAGNOSTIC_FINGERPRINT_V2,
            "expected": expected,
        }
        for expected in UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2.values()
    )
    return sorted(
        entries,
        key=lambda item: (
            str(item["policy"]),
            str(item["expected"]["supply_id"]),
            int(item["expected"]["nm_id"]),
        ),
    )


def _unmatched_doprinato_manifest_entry(
    supply_id: str, nm_id: int
) -> dict[str, Any] | None:
    """Resolve only one fully specified supply/SKU manifest identity."""

    v1 = UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.get(str(supply_id))
    if v1 is not None and int(v1["nm_id"]) == int(nm_id):
        return {
            "policy": UNMATCHED_DOPRINATO_ABSORPTION_POLICY,
            "reason": UNMATCHED_DOPRINATO_ABSORPTION_REASON,
            "approval_date": UNMATCHED_DOPRINATO_ABSORPTION_APPROVAL_DATE,
            "diagnostic_fingerprint": UNMATCHED_DOPRINATO_DIAGNOSTIC_FINGERPRINT,
            "expected": v1,
        }
    v2 = UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2.get(
        (str(supply_id), int(nm_id))
    )
    if v2 is None:
        return None
    return {
        "policy": UNMATCHED_DOPRINATO_ABSORPTION_POLICY_V2,
        "reason": UNMATCHED_DOPRINATO_ABSORPTION_REASON_V2,
        "approval_date": UNMATCHED_DOPRINATO_ABSORPTION_APPROVAL_DATE_V2,
        "diagnostic_fingerprint": UNMATCHED_DOPRINATO_DIAGNOSTIC_FINGERPRINT_V2,
        "expected": v2,
    }


def _unmatched_doprinato_cost_reference(
    conn: sqlite3.Connection,
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Read the exact current baseline stage pinned by one manifest row."""

    row = conn.execute(
        """
        SELECT line.stage,line.recognized_unit_cost_rub,line.paid_unit_cost_rub,
               line.source_type,line.source_identity,line.source_date,
               line.line_fingerprint,version.fingerprint baseline_fingerprint
        FROM sheet_vitrina_v1_canonical_cost_baseline_lines AS line
        JOIN sheet_vitrina_v1_canonical_cost_baseline_versions AS version
          ON version.baseline_id=line.baseline_id AND version.is_current=1
        WHERE line.nm_id=? AND line.stage=?
        ORDER BY version.version DESC LIMIT 1
        """,
        (int(expected["nm_id"]), str(expected["cost_reference_stage"])),
    ).fetchone()
    if row is None:
        return None
    return {
        "stage": str(row["stage"]),
        "recognized_unit_cost_rub": _text(
            _decimal(row["recognized_unit_cost_rub"])
        ),
        "paid_unit_cost_rub": _text(_decimal(row["paid_unit_cost_rub"])),
        "source_type": str(row["source_type"]),
        "source_identity": str(row["source_identity"]),
        "source_date": str(row["source_date"]),
        "line_fingerprint": str(row["line_fingerprint"]),
        "baseline_fingerprint": str(row["baseline_fingerprint"]),
    }


def _unmatched_doprinato_manifest_decision(
    conn: sqlite3.Connection,
    fact: Mapping[str, Any],
    *,
    baseline_cost_references: Mapping[
        tuple[int, str], Mapping[str, Any]
    ] | None = None,
) -> dict[str, Any] | None:
    """Return an exact allowlist decision, including fail-closed drift proof."""

    supply_id = str(fact.get("supply_id") or "")
    manifest_entry = _unmatched_doprinato_manifest_entry(
        supply_id, int(fact.get("nm_id") or 0)
    )
    if manifest_entry is None:
        return None
    expected = manifest_entry["expected"]
    reference = dict(
        (baseline_cost_references or {}).get(
            (int(expected["nm_id"]), str(expected["cost_reference_stage"]))
        )
        or {}
    ) or _unmatched_doprinato_cost_reference(conn, expected=expected)
    actual = {
        "supply_id": supply_id,
        "business_date": str(fact.get("accepted_date") or ""),
        "nm_id": int(fact.get("nm_id") or 0),
        "warehouse": str(fact.get("warehouse") or ""),
        "destination": str(fact.get("destination") or ""),
        "quantity": _text(_decimal(fact.get("accepted_quantity"))),
        "source_identity": str(fact.get("source_identity") or ""),
        "original_supply_id": str(fact.get("original_supply_id") or ""),
        "raw_source_row_fingerprint": str(
            fact.get("raw_source_row_fingerprint") or ""
        ),
        "raw_source_line_fingerprint": str(
            fact.get("raw_source_line_fingerprint") or ""
        ),
        "raw_row_line_fingerprint": str(
            fact.get("raw_row_line_fingerprint") or ""
        ),
        "semantic_evidence_fingerprint": str(
            fact.get("semantic_evidence_fingerprint") or ""
        ),
        "status": (
            "final-accepted" if bool(fact.get("is_final_accepted")) else ""
        ),
        "cost_reference_stage": (
            str(reference.get("stage") or "") if reference else ""
        ),
        "recognized_reference_unit_cost_rub": (
            str(reference.get("recognized_unit_cost_rub") or "")
            if reference else ""
        ),
        "paid_reference_unit_cost_rub": (
            str(reference.get("paid_unit_cost_rub") or "")
            if reference else ""
        ),
    }
    mismatches = {
        key: {"expected": expected.get(key), "actual": actual.get(key)}
        for key in expected
        if actual.get(key) != expected.get(key)
    }
    return {
        "matched": not mismatches,
        "expected": dict(expected),
        "actual": actual,
        "mismatches": mismatches,
        "cost_reference": reference,
        "classification": UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION,
        "source_quality": UNMATCHED_DOPRINATO_ABSORPTION_SOURCE_QUALITY,
        "policy": str(manifest_entry["policy"]),
        "reason": str(manifest_entry["reason"]),
        "human_approval": {
            "approval_date": str(manifest_entry["approval_date"]),
            "diagnostic_fingerprint": str(
                manifest_entry["diagnostic_fingerprint"]
            ),
            "policy": str(manifest_entry["policy"]),
        },
    }


def _normalized_acceptance_plan(
    *,
    operation: Mapping[str, Any],
    sent_by_nm: Mapping[int, Decimal],
    accepted_by_nm: Mapping[int, Decimal],
) -> dict[int, dict[str, Decimal]]:
    """Resolve one exact manifest supply without inventing aggregate quantity.

    Raw evidence is immutable.  A manifest supply first applies direct same-SKU
    acceptance and then assigns only its own composition surplus to its own
    shortage pool in deterministic nmID order.  No other supply, date or SKU is
    a candidate.
    """
    all_nm_ids = sorted(set(sent_by_nm) | set(accepted_by_nm))
    result: dict[int, dict[str, Decimal]] = {}
    surplus_pool = ZERO
    for nm_id in all_nm_ids:
        sent = sent_by_nm.get(nm_id, ZERO)
        raw = accepted_by_nm.get(nm_id, ZERO)
        direct = min(sent, raw)
        surplus = max(raw - sent, ZERO)
        surplus_pool += surplus
        result[nm_id] = {
            "sent": sent,
            "raw_accepted": raw,
            "direct_accepted": direct,
            "normalized_accepted": ZERO,
            "effective_accepted": direct,
            "open": sent - direct,
            "raw_surplus": surplus,
        }
    if surplus_pool <= ZERO:
        return result
    if not _postcutover_manifest_matches(operation):
        return result
    for nm_id in all_nm_ids:
        if surplus_pool <= ZERO:
            break
        row = result[nm_id]
        normalized = min(row["open"], surplus_pool)
        row["normalized_accepted"] += normalized
        row["effective_accepted"] += normalized
        row["open"] -= normalized
        surplus_pool -= normalized
    if surplus_pool > ZERO:
        raise CanonicalCostBlocked(
            "postcutover_normalization_surplus_exceeds_supply_shortage",
            {
                "operation_id": str(operation.get("operation_id") or ""),
                "supply_id": str(operation.get("supply_id") or ""),
                "unallocated_surplus": _text(surplus_pool),
            },
        )
    return result


def _movement_cost_pools(
    movements: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Decimal]]:
    pools: dict[str, dict[str, Decimal]] = {}
    for movement in movements:
        supply_id = str(movement.get("supply_id") or "")
        bucket = pools.setdefault(
            supply_id,
            {
                "sent": ZERO,
                "recognized_capital": ZERO,
                "paid_equivalent": ZERO,
                "paid_capital": ZERO,
                "covered": ZERO,
                "confirmed": ZERO,
            },
        )
        sent = _decimal(movement.get("sent_quantity"))
        bucket["sent"] += sent
        bucket["recognized_capital"] += _decimal(
            movement.get("recognized_capital_rub")
        )
        bucket["paid_equivalent"] += _decimal(
            movement.get("paid_equivalent_quantity")
        )
        bucket["paid_capital"] += _decimal(movement.get("paid_capital_rub"))
        bucket["covered"] += sent * _decimal(
            movement.get("cost_coverage_share")
        )
        bucket["confirmed"] += sent * _decimal(
            movement.get("confirmation_share")
        )
    for bucket in pools.values():
        bucket["recognized_unit"] = _safe_ratio(
            bucket["recognized_capital"], bucket["covered"]
        )
        bucket["paid_unit"] = _safe_ratio(
            bucket["paid_capital"], bucket["paid_equivalent"]
        )
        bucket["paid_share"] = min(
            _safe_ratio(bucket["paid_equivalent"], bucket["sent"]), ONE
        )
        bucket["coverage_share"] = min(
            _safe_ratio(bucket["covered"], bucket["sent"]), ONE
        )
        bucket["confirmation_share"] = min(
            _safe_ratio(bucket["confirmed"], bucket["sent"]), ONE
        )
    return pools


def _wb_movement_evidence(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    anomaly_report: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    movements: list[dict[str, Any]] = []
    eligible_anomalies = _eligible_anomaly_index(anomaly_report or {})
    diagnostic_quarantined_doprinato = {
        (str(item["supply_id"]), int(item["nm_id"]))
        for item in (anomaly_report or {}).get(
            "diagnostic_quarantined_doprinato"
        ) or []
    }
    boundary = _ff_opening_boundary_context(conn)
    for operation in _ff_operation_rows(conn):
        if str(operation.get("operation_type")) != "auto_writeoff":
            continue
        effective = _canonical_ff_operation_effective_date(
            conn, operation, boundary=boundary
        )
        if not effective or effective > as_of_date:
            continue
        supply_id = str(operation.get("source_object_id") or "")
        supply = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=? LIMIT 1",
            (supply_id,),
        ).fetchone()
        accepted_by_nm: dict[int, Decimal] = defaultdict(Decimal)
        accepted_date = ""
        warehouse = ""
        destination = ""
        is_final_accepted = False
        if supply is not None:
            normalized = _json_loads(supply["normalized_row_json"])
            status_id = int(normalized.get("status_id") or normalized.get("statusID") or supply["status_id"] or 0)
            is_final_accepted = status_id == 5
            accepted_date = _wb_accepted_date(normalized, supply)
            warehouse = str(normalized.get("warehouse_name") or normalized.get("warehouseName") or supply["warehouse_id"] or "")
            destination = str(normalized.get("destination_name") or normalized.get("target_warehouse_name") or warehouse)
            if accepted_date and accepted_date <= as_of_date:
                for item in _goods(supply["raw_goods_json"]):
                    nm_id = int(item.get("nmID") or item.get("nmId") or item.get("nm_id") or 0)
                    qty = _decimal(item.get("acceptedQuantity") or item.get("accepted_quantity") or 0)
                    if nm_id > 0:
                        accepted_by_nm[nm_id] += qty
        sent_by_nm: dict[int, Decimal] = defaultdict(Decimal)
        for line in conn.execute(
            "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchall():
            nm_id = int(line["nm_id"])
            sent_by_nm[nm_id] += abs(min(_decimal(line["quantity_delta"]), ZERO))
        operation_evidence = next(
            (
                dict(item)
                for item in (anomaly_report or {}).get("operations") or []
                if str(item.get("operation_id") or "")
                == str(operation["operation_id"])
            ),
            {
                "operation_id": str(operation["operation_id"]),
                "supply_id": supply_id,
                "source_key": str(operation.get("source_key") or ""),
                "business_date": effective,
            },
        )
        acceptance_plan = _normalized_acceptance_plan(
            operation=operation_evidence,
            sent_by_nm=sent_by_nm,
            accepted_by_nm=accepted_by_nm,
        )
        manifest_normalized = _postcutover_manifest_matches(operation_evidence)
        for nm_id in sorted(sent_by_nm):
            sent = sent_by_nm[nm_id]
            planned = acceptance_plan[nm_id]
            raw_accepted = planned["raw_accepted"]
            accepted_quantity = planned["effective_accepted"]
            if raw_accepted > sent:
                decision = _eligible_anomaly_index(anomaly_report or {}).get(
                    ("accepted_quantity_exceeds_sent", str(operation["operation_id"]), nm_id)
                )
                if decision is None:
                    raise CanonicalCostBlocked(
                        "accepted_quantity_exceeds_sent",
                        {
                            "operation_id": str(operation["operation_id"]),
                            "supply_id": supply_id,
                            "nm_id": nm_id,
                            "sent": _text(sent),
                            "raw_accepted": _text(raw_accepted),
                        },
                    )
            movements.append({
                "operation_id": str(operation["operation_id"]),
                "supply_id": supply_id,
                "nm_id": nm_id,
                "sent_quantity": sent,
                "raw_accepted_quantity": raw_accepted,
                "direct_accepted_quantity": planned["direct_accepted"],
                "normalized_accepted_quantity": planned["normalized_accepted"],
                "accepted_quantity": accepted_quantity,
                "open_quantity": planned["open"],
                "accepted_date": accepted_date,
                "writeoff_date": effective,
                "warehouse": warehouse,
                "destination": destination,
                "is_final_accepted": is_final_accepted,
                "normalization_policy": (
                    POSTCUTOVER_NORMALIZATION_POLICY
                    if manifest_normalized
                    else ""
                ),
            })
    for fact in sorted(
        (item for item in _wb_supply_cache_evidence(conn, date_to=as_of_date) if item["is_doprinato"]),
        key=lambda item: (item["accepted_date"], item["supply_id"], item["nm_id"]),
    ):
        if str(fact["accepted_date"] or "") < CUTOVER_DATE:
            continue
        if (
            str(fact["supply_id"]), int(fact["nm_id"])
        ) in diagnostic_quarantined_doprinato:
            continue
        absorption = eligible_anomalies.get(
            (
                "doprinato_unmatched_surplus",
                str(fact["supply_id"]),
                int(fact["nm_id"]),
            )
        )
        if (
            absorption is not None
            and absorption.get("classification")
            == UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION
        ):
            # Already present in official WB stock.  It is immutable source
            # evidence only and cannot close any unrelated outstanding layer.
            continue
        remaining = _decimal(fact["accepted_quantity"])
        candidates = [
            item for item in movements
            if item["nm_id"] == fact["nm_id"]
            and item["open_quantity"] > ZERO
            and item["is_final_accepted"]
            and (item["accepted_date"] or item["writeoff_date"]) <= fact["accepted_date"]
            and (
                (fact["original_supply_id"] and item["supply_id"] == fact["original_supply_id"])
                or (
                    not fact["original_supply_id"]
                    and item["warehouse"] == fact["warehouse"]
                    and item["destination"] == fact["destination"]
                )
            )
        ]
        candidates.sort(key=lambda item: (item["accepted_date"] or item["writeoff_date"], item["supply_id"]))
        for item in candidates:
            if remaining <= ZERO:
                break
            closed = min(remaining, item["open_quantity"])
            item["open_quantity"] -= closed
            remaining -= closed
        if remaining > ZERO:
            # The cutover baseline absorbs legacy history.  An orphan
            # doprinato absorbed by the opening snapshot cannot be safely
            # reconstructed and therefore stays source evidence only: it
            # creates neither a movement nor a zero-cost buffer.  New-contour
            # evidence remains strict and fail-closed.
            decision = eligible_anomalies.get(
                ("doprinato_unmatched_surplus", str(fact["supply_id"]), int(fact["nm_id"]))
            )
            if decision is not None:
                continue
            raise CanonicalCostBlocked(
                "doprinato_unmatched_surplus",
                {
                    "supply_id": fact["supply_id"],
                    "nm_id": fact["nm_id"],
                    "accepted_date": fact["accepted_date"],
                    "surplus": _text(remaining),
                },
            )
    return movements


def _wb_supply_cache_evidence(conn: sqlite3.Connection, *, date_to: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_wb_supplies ORDER BY COALESCE(fact_date,supply_date,updated_date),supply_id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        raw_row = dict(row)
        normalized = _json_loads(row["normalized_row_json"])
        accepted_date = _wb_accepted_date(normalized, row)
        if not accepted_date or accepted_date > date_to:
            continue
        is_doprinato = int(normalized.get("virtual_type_id") or 0) == 5 or str(normalized.get("type_label") or "").strip() == "Допринято"
        status_id = int(normalized.get("status_id") or normalized.get("statusID") or row["status_id"] or 0)
        warehouse = str(normalized.get("warehouse_name") or normalized.get("warehouseName") or row["warehouse_id"] or "")
        destination = str(normalized.get("destination_name") or normalized.get("target_warehouse_name") or warehouse)
        original = str(normalized.get("original_supply_id") or normalized.get("originalSupplyID") or normalized.get("parent_supply_id") or "")
        for item in _goods(row["raw_goods_json"]):
            nm_id = int(item.get("nmID") or item.get("nmId") or item.get("nm_id") or 0)
            accepted = _decimal(item.get("acceptedQuantity") or item.get("accepted_quantity") or (item.get("quantity") if is_doprinato else 0))
            if nm_id <= 0 or accepted < ZERO:
                continue
            fact = {
                "supply_id": str(row["supply_id"]), "nm_id": nm_id,
                "accepted_quantity": accepted, "accepted_date": accepted_date,
                "warehouse": warehouse, "destination": destination,
                "original_supply_id": original, "is_doprinato": is_doprinato,
                "is_final_accepted": status_id == 5,
                "source_identity": str(row["cache_key"]),
            }
            fact.update(
                _wb_supply_raw_row_line_fingerprints(raw_row, dict(item))
            )
            fact["semantic_evidence_fingerprint"] = (
                "sha256:" + _stable_hash(_doprinato_fact_fingerprint_payload(fact))
            )
            fact["source_status"] = (
                "final-accepted" if status_id == 5 else f"status-{status_id}"
            )
            result.append(fact)
    return result


def _wb_accepted_date(normalized: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    for key in (
        "actual_acceptance_date", "actualAcceptanceDate", "acceptance_date",
        "acceptanceDate", "fact_date", "factDate", "closed_at", "closedAt",
    ):
        value = str(normalized.get(key) or "").strip()
        if len(value) >= 10:
            return value[:10]
    try:
        value = str(row["fact_date"] or "").strip()
    except (KeyError, IndexError, TypeError):
        value = ""
    return value[:10] if len(value) >= 10 else ""


def _goods(raw: Any) -> list[dict[str, Any]]:
    payload = _json_loads(raw)
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        rows = payload.get("goods") or payload.get("items") or payload.get("data") or []
        return [dict(item) for item in rows if isinstance(item, Mapping)] if isinstance(rows, list) else []
    return []


def _stage_source(stage: str) -> str:
    return {
        STAGE_PRODUCTION: "supplier_registry.production",
        STAGE_PRODUCTION_TO_FF: "supplier_registry.actual_shipment_without_ff_acceptance",
        STAGE_FF: "ff_stock_ledger",
        STAGE_FF_TO_WB: "ff_debit_plus_persisted_wb_acceptance",
        STAGE_WB: "official_wb_stock_ready_snapshot",
    }[stage]


def _json_safe_physical(value: Mapping[int, Mapping[str, Decimal]]) -> dict[str, dict[str, str]]:
    return {
        str(nm_id): {stage: _text(stages.get(stage, ZERO)) for stage in STAGES}
        for nm_id, stages in sorted(value.items())
    }


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator > ZERO else ZERO


def _decimal(value: Any) -> Decimal:
    if value in {None, ""}:
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _text(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000001"))
    text = format(normalized, "f").rstrip("0").rstrip(".")
    return text or "0"


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value}") from exc


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

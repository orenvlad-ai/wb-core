#!/usr/bin/env python3
"""Full exact-catalog FBS source generation and omission provenance proof."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_content import (  # noqa: E402
    WbContentCard,
    WbContentCatalogSnapshot,
)
from packages.adapters.wb_fbs_orders import (  # noqa: E402
    WbFbsOffice,
    WbFbsSellerWarehouse,
    WbFbsStock,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    ensure_ff_pool_foundation_schema,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    WAREHOUSE_MAPPINGS_TABLE,
    ensure_wb_fbs_orders_schema,
)
from packages.application.wb_fbs_warehouse_registry import (  # noqa: E402
    COMPLETE_CATALOG_OMISSION_ZERO_POLICY,
    STOCK_ROWS_TABLE,
    WbFbsWarehouseRegistry,
)


MOSCOW_WAREHOUSE_ID = 1988668
MOSCOW_OFFICE_ID = 14017
ORENBURG_WAREHOUSE_ID = 854205
ORENBURG_OFFICE_ID = 12223


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        current = self.value
        self.value += timedelta(seconds=1)
        return current.isoformat().replace("+00:00", "Z")


class OfficialSource:
    def list_seller_warehouses(self):
        return [
            WbFbsSellerWarehouse(
                warehouse_id=MOSCOW_WAREHOUSE_ID,
                office_id=MOSCOW_OFFICE_ID,
                name="FBS Москва",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
            WbFbsSellerWarehouse(
                warehouse_id=ORENBURG_WAREHOUSE_ID,
                office_id=ORENBURG_OFFICE_ID,
                name="FBS Оренбург",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
            WbFbsSellerWarehouse(
                warehouse_id=777777,
                office_id=777,
                name="Unbound evidence only",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
        ]

    def list_offices(self):
        return [
            WbFbsOffice(
                office_id=MOSCOW_OFFICE_ID,
                name="Москва (Софьино)",
                city="Москва_Восток",
                federal_district="ЦФО",
            ),
            WbFbsOffice(
                office_id=ORENBURG_OFFICE_ID,
                name="Оренбург Центральная",
                city="Оренбург",
                federal_district="ПФО",
            ),
            WbFbsOffice(
                office_id=777,
                name="Unbound office",
                city="Казань",
                federal_district="ПФО",
            ),
        ]

    def list_stocks(self, *, warehouse_id, chrt_ids):
        assert chrt_ids == [9001, 9002, 9003, 9303], chrt_ids
        if warehouse_id == MOSCOW_WAREHOUSE_ID:
            return [
                WbFbsStock(chrt_id=9001, amount=0),
                WbFbsStock(chrt_id=9003, amount=5),
            ]
        if warehouse_id == ORENBURG_WAREHOUSE_ID:
            return [
                WbFbsStock(chrt_id=9001, amount=2),
                WbFbsStock(chrt_id=9002, amount=0),
            ]
        raise AssertionError("unbound seller warehouse must not enter complete source scope")


class StockFailureSource(OfficialSource):
    def list_stocks(self, *, warehouse_id, chrt_ids):
        if warehouse_id == ORENBURG_WAREHOUSE_ID:
            raise RuntimeError("synthetic Orenburg stock read failure")
        return super().list_stocks(warehouse_id=warehouse_id, chrt_ids=chrt_ids)


class ConfirmationSource(OfficialSource):
    def __init__(self, mode='stable'):
        self.mode, self.calls = mode, {}
        self.registry_calls = self.office_calls = 0

    def list_seller_warehouses(self):
        self.registry_calls += 1
        return super().list_seller_warehouses()

    def list_offices(self):
        self.office_calls += 1
        return super().list_offices()

    def list_stocks(self, *, warehouse_id, chrt_ids):
        self.calls[warehouse_id] = self.calls.get(warehouse_id, 0) + 1
        if warehouse_id == MOSCOW_WAREHOUSE_ID and self.calls[warehouse_id] == 1 and self.mode == 'initial_failed':
            raise RuntimeError('Authorization=secret-fixture-token raw response body')
        rows = super().list_stocks(warehouse_id=warehouse_id, chrt_ids=chrt_ids)
        if warehouse_id == MOSCOW_WAREHOUSE_ID and self.calls[warehouse_id] == 2:
            if self.mode == 'failed':
                raise RuntimeError('Authorization=secret-fixture-token raw response body')
            if self.mode == 'amount':
                rows = [WbFbsStock(chrt_id=r.chrt_id, amount=r.amount + 1) for r in rows]
            if self.mode == 'provenance':
                rows = rows + [WbFbsStock(chrt_id=9002, amount=0)]
            if self.mode == 'duplicate':
                rows = rows + [rows[0]]
        return rows


def check_confirmation_diagnostics():
    import json
    from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE, fbs_generation_failure
    for mode, reason in (('stable', None), ('failed', 'confirmation_stock_request_failed'),
                         ('duplicate', 'duplicate_stock_identity'),
                         ('initial_failed', 'initial_stock_request_failed'),
                         ('amount', 'stock_amount_changed'), ('provenance', 'stock_provenance_changed')):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'operational.sqlite3'
            _seed(db)
            source, catalog = ConfirmationSource(mode), CatalogSource()
            result = WbFbsWarehouseRegistry(db_path=db, runtime_dir=db.parent,
                timestamp_factory=Clock(), source=source, catalog_source=catalog).collect()
            assert source.calls == {MOSCOW_WAREHOUSE_ID: 2, ORENBURG_WAREHOUSE_ID: 2}
            assert source.registry_calls == source.office_calls == catalog.calls == 2
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(f'SELECT * FROM {REGISTRY_RUNS_TABLE} ORDER BY run_sequence DESC LIMIT 1').fetchone()
                saved = json.loads(row['catalog_scope_json'])['generation_diagnostics']
                diagnostic = fbs_generation_failure(row)
                assert 'secret-fixture-token' not in repr([tuple(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_runs')])
            assert saved['stock_confirmation_count'] == 2
            assert 'secret-fixture-token' not in json.dumps(dict(row))
            if reason is None:
                assert result['source_generation']['complete'] and row['complete'] == 1
                # Stable fixture under the explicit nmID/chrtID identity policy.
                assert result['source_generation']['generation_digest'] == 'sha256:a45c85a65a9753b849c865f82d5d4cec866322a1588ef869064d46a4e3322b0c', result['source_generation']['generation_digest']
                assert all(not p['reason_codes'] for p in saved['stock_confirmations'])
            else:
                assert row['complete'] == 0 and result['source_generation']['complete'] is False
                assert reason in diagnostic['reason_codes']
                proof = next(p for p in diagnostic['stock_confirmations'] if p['seller_warehouse_id'] == MOSCOW_WAREHOUSE_ID)
                assert reason in proof['reason_codes']
                if mode == 'provenance':
                    assert proof['amount_change_count'] == 0 and proof['provenance_change_count'] == 1
                    assert proof['changed_identities'] == [{'chrt_id':9002, 'nm_id':101}]
                if mode == 'amount':
                    assert proof['amount_change_count'] == 2 and proof['provenance_change_count'] == 0
                    assert proof['changed_identities'] == [{'chrt_id':9001, 'nm_id':101}, {'chrt_id':9003, 'nm_id':202}]
                if mode in ('failed', 'initial_failed'):
                    assert proof['changed_identity_count'] == 0 and proof['changed_identities'] == []
    # Large exact reads retain bounded identifiers, never a raw second response.
    from packages.application.wb_fbs_warehouse_registry import _stock_confirmation_proofs
    before = [{'chrt_id':k, 'nm_id':100+k, 'amount':1, 'provenance':'explicit_wb_row'} for k in range(30)]
    after = [{**r, 'amount':2} for r in before]
    pair = lambda rows: {'seller_warehouse_id':1988668, 'complete':True, 'rows':rows}
    proof = _stock_confirmation_proofs([pair(before)], [pair(after)])[0]
    assert proof['changed_identity_count'] == 30 and proof['changed_identities_truncated']
    assert len(proof['changed_identities']) == 10
    assert proof['amount_change_count'] == 30 and 'amount' not in proof['changed_identities'][0]

    # The retained proof cap must not reduce the count of reads actually attempted.
    from unittest.mock import patch
    class LateCatalogFailure(CatalogSource):
        def fetch_catalog_snapshot(self):
            if self.calls:
                raise RuntimeError('Authorization=secret-fixture-token raw response body')
            return super().fetch_catalog_snapshot()

    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / 'operational.sqlite3'
        _seed(db)
        registry = WbFbsWarehouseRegistry(db_path=db, runtime_dir=db.parent,
            timestamp_factory=Clock(), source=OfficialSource(), catalog_source=LateCatalogFailure())
        scope = {'warehouses':[{'seller_warehouse_id':k} for k in range(1, 10)],
                 'warehouse_count':9, 'complete':True, 'scope_digest':'sha256:scope'}
        persisted, reads = [], []
        def read_stocks(**kwargs):
            reads.append(kwargs['seller_warehouse_id'])
            return {'seller_warehouse_id':kwargs['seller_warehouse_id'],
                    'complete':True, 'status':'success', 'source_digest':'sha256:stock', 'rows':[]}
        with patch.object(registry, '_active_exact_warehouse_scope', return_value=scope), \
             patch.object(registry, '_read_warehouse_stocks', side_effect=read_stocks), \
             patch.object(registry, '_persist', side_effect=lambda **kwargs: persisted.append(kwargs)), \
             patch.object(registry, 'read_model', return_value={}):
            registry.collect()
        assert reads == list(range(1, 10)) * 2
        saved = persisted[0]['registry']['catalog_scope']['generation_diagnostics']
        assert saved['stock_confirmation_count'] == 9 and len(saved['stock_confirmations']) == 8
        assert persisted[0]['registry']['complete'] is False
        assert persisted[0]['registry']['error'] == 'official_fbs_acquisition_failed'
        assert 'secret-fixture-token' not in repr(persisted)


class OfficeMismatchSource(OfficialSource):
    def list_seller_warehouses(self):
        rows = super().list_seller_warehouses()
        return [
            WbFbsSellerWarehouse(
                warehouse_id=row.warehouse_id,
                office_id=14018 if row.warehouse_id == MOSCOW_WAREHOUSE_ID else row.office_id,
                name=row.name,
                cargo_type=row.cargo_type,
                delivery_type=row.delivery_type,
                is_deleting=row.is_deleting,
                is_processing=row.is_processing,
            )
            for row in rows
        ]

    def list_offices(self):
        return super().list_offices() + [
            WbFbsOffice(
                office_id=14018,
                name="Moscow wrong live office",
                city="Москва_Восток",
                federal_district="ЦФО",
            )
        ]


class CatalogSource:
    def __init__(self, *, drift: bool = False) -> None:
        self.calls = 0
        self.drift = drift

    def fetch_catalog_snapshot(self):
        self.calls += 1
        cards = [
            WbContentCard(
                nm_id=101,
                vendor_code="ACTIVE-101",
                title="Active 101",
                subject_name="Fixture",
                updated_at="2026-09-02T03:00:00Z",
                barcodes=["bc-101-a", "bc-101-b"],
                chrt_ids=[9001, 9002],
            ),
            WbContentCard(
                nm_id=202,
                vendor_code="ACTIVE-202",
                title="Active 202",
                subject_name="Fixture",
                updated_at="2026-09-02T03:00:01Z",
                barcodes=["bc-202"],
                chrt_ids=[9003] + ([9999] if self.drift and self.calls % 2 == 0 else []),
            ),
            WbContentCard(
                nm_id=303,
                vendor_code="HIDDEN-303",
                title="Hidden 303",
                subject_name="Fixture",
                updated_at="2026-09-02T03:00:02Z",
                barcodes=["bc-303"],
                chrt_ids=[9303],
            ),
            WbContentCard(
                nm_id=404,
                vendor_code="INACTIVE-404",
                title="Inactive 404",
                subject_name="Fixture",
                updated_at="2026-09-02T03:00:03Z",
                barcodes=["bc-404"],
                chrt_ids=[9404],
            ),
        ]
        return WbContentCatalogSnapshot.from_cards(
            cards,
            pages_fetched=1,
            terminal_short_page=True,
            cursor_chain_digest="sha256:fixture-cursor-chain",
        )



def check_catalog_identity():
    import json
    from dataclasses import replace
    from packages.application.wb_fbs_warehouse_registry import (
        REGISTRY_RUNS_TABLE, fbs_generation_failure, _catalog_comparison, _safe_catalog_comparison)
    baseline_digest = None
    for mode in ('stable', 'description', 'reordered', 'local_timestamp', 'chrt_added',
                 'chrt_removed', 'rebound', 'card_removed', 'card_added', 'local_scope', 'local_visibility', 'incomplete', 'duplicate'):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'operational.sqlite3'
            _seed(db)
            class ChangingCatalog(CatalogSource):
                def fetch_catalog_snapshot(self):
                    snapshot = super().fetch_catalog_snapshot()
                    if self.calls != 2:
                        return snapshot
                    cards = list(snapshot.cards)
                    if mode == 'description':
                        cards = [replace(c, title='changed', vendor_code='new article',
                                         subject_name='new category', updated_at='2099-01-01',
                                         barcodes=['new barcode']) for c in cards]
                    elif mode == 'reordered':
                        cards = [replace(c, chrt_ids=list(reversed(c.chrt_ids))) for c in reversed(cards)]
                    elif mode == 'chrt_added':
                        cards[0] = replace(cards[0], chrt_ids=[9001, 9002, 9999])
                    elif mode == 'chrt_removed':
                        cards[0] = replace(cards[0], chrt_ids=[9001])
                    elif mode == 'rebound':
                        cards[0] = replace(cards[0], chrt_ids=[9001, 9003])
                        cards[1] = replace(cards[1], chrt_ids=[9002])
                    elif mode == 'card_removed':
                        cards = cards[1:]
                    elif mode == 'card_added':
                        cards.append(replace(cards[-1], nm_id=505, chrt_ids=[9505]))
                    elif mode == 'duplicate':
                        cards.append(cards[0])
                    elif mode in ('local_timestamp', 'local_scope', 'local_visibility'):
                        with sqlite3.connect(db) as c:
                            if mode == 'local_timestamp':
                                c.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET updated_at='2099-01-01'")
                            elif mode == 'local_visibility':
                                c.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET is_hidden=1 WHERE nm_id=101")
                            else:
                                c.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET is_active=1 WHERE nm_id=404")
                    return WbContentCatalogSnapshot.from_cards(cards, pages_fetched=1,
                        terminal_short_page=mode != 'incomplete', cursor_chain_digest='changed-cursor')
            result = WbFbsWarehouseRegistry(db_path=db, runtime_dir=db.parent, timestamp_factory=Clock(),
                source=OfficialSource(), catalog_source=ChangingCatalog()).collect()
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(f'SELECT * FROM {REGISTRY_RUNS_TABLE} ORDER BY run_sequence DESC LIMIT 1').fetchone()
                scope = json.loads(row['catalog_scope_json'])
            proof = scope['generation_diagnostics']['catalog_comparison']
            accepted = mode in ('stable', 'description', 'reordered', 'local_timestamp')
            assert bool(row['complete']) == accepted, (mode, dict(row))
            if accepted:
                digest = result['source_generation']['generation_digest']
                if baseline_digest is None:
                    baseline_digest = digest
                assert digest == baseline_digest, mode
                assert proof['identity_change_count'] == 0
            else:
                safe = fbs_generation_failure(row)['catalog_comparison']
                assert safe == proof, (mode, safe, proof)
                if mode == 'local_scope':
                    assert safe['requested_added_nm_ids'] == [404]
                elif mode == 'local_visibility':
                    assert safe['requested_state_changes'] == [{'nm_id': 101, 'fields': ['is_hidden']}]
                elif mode not in ('incomplete', 'duplicate'):
                    assert safe['identity_change_count'] > 0 and safe['changes']
                if mode == 'incomplete':
                    assert safe['before_complete'] and not safe['after_complete']
                if mode == 'rebound':
                    assert safe['changes'][0]['before_chrt_ids'] == [9001, 9002]
                    assert safe['changes'][0]['after_chrt_ids'] == [9001, 9003]
            if mode == 'description':
                assert proof['description_change_count'] == 4
                assert proof['before_source_digest'] != proof['after_source_digest']
                assert proof['before_identity_digest'] == proof['after_identity_digest']
                assert proof['changes'][0]['fields'] == ['title', 'vendor_code', 'subject_name', 'updated_at', 'barcodes']
                assert 'new article' not in json.dumps(proof)
    # Bounding evidence never bounds the actual comparison. Late identity changes
    # must be visible even when earlier cards have description-only changes.
    original = CatalogSource().fetch_catalog_snapshot().cards[0]
    cards = [replace(original, nm_id=n, chrt_ids=[n]) for n in range(1, 40)]
    changed = [replace(c, title='changed') for c in cards]
    changed[-1] = replace(changed[-1], chrt_ids=list(range(100, 140)))
    def snapshot(rows):
        return WbContentCatalogSnapshot.from_cards(rows, pages_fetched=1,
            terminal_short_page=True, cursor_chain_digest='fixture')
    proof = _catalog_comparison(snapshot(cards), snapshot(changed), {}, {})
    assert proof['change_count'] == 39 and proof['identity_change_count'] == 1
    assert len(proof['changes']) == 10 and proof['changes_truncated']
    assert proof['changes'][0]['nm_id'] == 39 and proof['changes'][0]['chrt_ids_truncated']
    assert proof['changes'][0]['after_chrt_count'] == 40
    assert len(proof['changes'][0]['after_chrt_ids']) == 20
    proof['secret'] = 'Authorization=secret-fixture'
    proof['changes'][0]['fields'].append('secret-fixture')
    proof['before_source_digest'] = 'secret-fixture'
    assert 'secret-fixture' not in json.dumps(_safe_catalog_comparison(proof))
    assert _safe_catalog_comparison(None) is None


def main() -> int:
    check_catalog_identity()
    check_confirmation_diagnostics()
    with tempfile.TemporaryDirectory(prefix="wb-fbs-complete-snapshot-") as raw:
        db_path = Path(raw) / "operational.sqlite3"
        _seed(db_path)
        clock = Clock()
        registry = WbFbsWarehouseRegistry(
            db_path=db_path, runtime_dir=db_path.parent,
            timestamp_factory=clock,
            source=OfficialSource(),
            catalog_source=CatalogSource(),
        )
        good = registry.collect()
        generation = good["source_generation"]
        assert generation["status"] == "complete" and generation["complete"] is True
        assert generation["policy_version"] == COMPLETE_CATALOG_OMISSION_ZERO_POLICY
        assert generation["catalog_scope"]["active_nm_id_count"] == 3
        assert generation["catalog_scope"]["main_nm_id_count"] == 2
        assert generation["catalog_scope"]["retained_hidden_nm_id_count"] == 1
        assert generation["catalog_scope"]["requested_chrt_count"] == 4
        assert generation["warehouse_scope"]["warehouse_count"] == 2
        assert generation["cardinality"] == {
            "warehouse_count": 2,
            "requested_chrt_count": 4,
            "expected_dense_row_count": 8,
            "actual_dense_row_count": 8,
            "explicit_wb_row_count": 4,
            "explicit_zero_count": 2,
            "omitted_requested_zero_count": 4,
        }
        warehouse_pairs = {
            (row["seller_warehouse_id"], row["official_office_id"])
            for row in generation["warehouses"]
        }
        assert warehouse_pairs == {
            (MOSCOW_WAREHOUSE_ID, MOSCOW_OFFICE_ID),
            (ORENBURG_WAREHOUSE_ID, ORENBURG_OFFICE_ID),
        }
        with sqlite3.connect(db_path) as conn:
            provenance = conn.execute(
                f"""SELECT seller_warehouse_id,chrt_id,amount,provenance
                       FROM {STOCK_ROWS_TABLE}
                      WHERE run_id IN (
                            SELECT run_id FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_runs
                             WHERE registry_run_id=?
                      ) ORDER BY seller_warehouse_id,chrt_id""",
                (generation["generation_id"],),
            ).fetchall()
        assert len(provenance) == 8
        assert (MOSCOW_WAREHOUSE_ID, 9303, 0, "omitted_requested_zero") in provenance
        assert (ORENBURG_WAREHOUSE_ID, 9303, 0, "omitted_requested_zero") in provenance
        assert (MOSCOW_WAREHOUSE_ID, 9001, 0, "explicit_wb_row") in provenance
        assert (MOSCOW_WAREHOUSE_ID, 9002, 0, "omitted_requested_zero") in provenance
        assert (ORENBURG_WAREHOUSE_ID, 9002, 0, "explicit_wb_row") in provenance
        assert (ORENBURG_WAREHOUSE_ID, 9003, 0, "omitted_requested_zero") in provenance

        stable_generation_id = generation["generation_id"]
        drifted = WbFbsWarehouseRegistry(
            db_path=db_path, runtime_dir=db_path.parent,
            timestamp_factory=clock,
            source=OfficialSource(),
            catalog_source=CatalogSource(drift=True),
        ).collect()
        assert drifted["latest_attempt"]["complete"] == 0
        assert drifted["source_generation"]["generation_id"] == stable_generation_id

        failed = WbFbsWarehouseRegistry(
            db_path=db_path, runtime_dir=db_path.parent,
            timestamp_factory=clock,
            source=StockFailureSource(),
            catalog_source=CatalogSource(),
        ).collect()
        assert failed["latest_attempt"]["complete"] == 0
        assert failed["source_generation"]["generation_id"] == stable_generation_id

        office_mismatch = WbFbsWarehouseRegistry(
            db_path=db_path, runtime_dir=db_path.parent,
            timestamp_factory=clock,
            source=OfficeMismatchSource(),
            catalog_source=CatalogSource(),
        ).collect()
        assert office_mismatch["latest_attempt"]["complete"] == 0
        assert office_mismatch["source_generation"]["generation_id"] == stable_generation_id
    print("wb fbs complete snapshot smoke: ok")
    return 0


def _seed(path: Path) -> None:
    now = "2026-09-02T03:00:00Z"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_ff_pool_foundation_schema(conn)
        ensure_wb_fbs_orders_schema(conn)
        conn.execute(
            """CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                   item_id TEXT PRIMARY KEY,nm_id INTEGER,is_active INTEGER,
                   is_hidden INTEGER,updated_at TEXT)"""
        )
        conn.executemany(
            "INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?,?)",
            [
                ("active-101", 101, 1, 0, now),
                ("active-202", 202, 1, 0, now),
                ("hidden-303", 303, 1, 1, now),
                ("inactive-404", 404, 0, 0, now),
            ],
        )
        for facility_id, code, name, city in (
            ("fac_moscow", "MSK", "FF Москва", "Москва"),
            ("fac_orenburg", "ORE", "FF Оренбург", "Оренбург"),
        ):
            conn.execute(
                f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,1,'Asia/Yekaterinburg',?,?)",
                (facility_id, code, name, now, now),
            )
            conn.execute(
                f"INSERT INTO {FACILITY_PROFILES_TABLE} VALUES(?,?,'{{}}',?,?)",
                (facility_id, city, now, now),
            )
        conn.executemany(
            f"""INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(
                   mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
                   created_at,created_by,official_office_id,official_warehouse_name,
                   official_office_name,official_office_city,official_evidence_digest
               ) VALUES(?,?,?,?,1,?,?,?,?,?,?,?)""",
            [
                (
                    "map-moscow", MOSCOW_WAREHOUSE_ID, "fac_moscow", "sha256:map-moscow",
                    now, "fixture", MOSCOW_OFFICE_ID, "FBS Москва", "Москва (Софьино)",
                    "Москва_Восток", "sha256:official-moscow",
                ),
                (
                    "map-orenburg", ORENBURG_WAREHOUSE_ID, "fac_orenburg", "sha256:map-orenburg",
                    now, "fixture", ORENBURG_OFFICE_ID, "FBS Оренбург", "Оренбург Центральная",
                    "Оренбург", "sha256:official-orenburg",
                ),
            ],
        )
        conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())

"""Native retired-manual boundary, preserved legacy reads and cutover fence."""
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.operator_facility_mappings_http_smoke import setup, NOW
from packages.application.ff_pool_cutover import ensure_ff_pool_cutover_schema, MANIFESTS_TABLE
from packages.application.ff_stock_ledger import FfStockLedgerBlock
from packages.application.operator_manual_ff_stock import ModernFfWorkflowRequired
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes


def cutover(rt):
    with sqlite3.connect(rt.db_path) as conn:
        ensure_ff_pool_cutover_schema(conn)
        columns = conn.execute(f'PRAGMA table_info({MANIFESTS_TABLE})').fetchall()
        values = {r[1]: (0 if r[2] == 'INTEGER' else 'fixture') for r in columns}
        values.update(cutover_id='manual-fixture-cutover', manifest_digest='sha256:'+'a'*64,
            deployed_sha='a'*40, cutover_at=NOW, business_date=NOW[:10], feature_epoch=1,
            created_at=NOW, manifest_json='{}')
        conn.execute(f'INSERT INTO {MANIFESTS_TABLE}({",".join(values)}) VALUES({",".join("?" for _ in values)})', list(values.values()))


def source(rt, key, **extra):
    return rt.create_ff_stock_operation(operation_id='manual-'+key, operation_type='manual_receipt',
        source_type=extra.pop('source_type', 'manual_excel'), source_key=key, created_at=NOW,
        lines=[{'nm_id':101, 'quantity_delta':5}], **extra)


def counts(rt):
    with sqlite3.connect(rt.db_path) as conn:
        return tuple(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in (
            'sheet_vitrina_v1_ff_stock_operations', 'sheet_vitrina_v1_ff_stock_operation_lines',
            'sheet_vitrina_v1_ff_stock_operation_previews'))


def refused(callback):
    try: callback()
    except ModernFfWorkflowRequired: return
    raise AssertionError('manual source admitted after cutover')


def main():
    with TemporaryDirectory(prefix='manual-ff-native-') as raw:
        rt, entry = setup(raw)
        block = FfStockLedgerBlock(runtime=rt, timestamp_factory=lambda:NOW)
        workbook = build_single_sheet_workbook_bytes('Manual', [['Штрихкод','nmId','Количество'],['',101,5]])
        preview = block.parse_manual_operation_preview(workbook, operation_type='manual_receipt')
        assert preview['apply_allowed'], preview
        saved = source(rt,'legacy-before')
        cutover(rt); before = counts(rt)
        with patch('packages.application.ff_stock_ledger.WarehouseRecoveryRegistry', side_effect=AssertionError('recovery before refusal')):
            refused(lambda:block.confirm_manual_operation(preview['preview']['preview_id']))
        refused(lambda:block.parse_manual_operation_preview(workbook, operation_type='manual_writeoff'))
        refused(lambda:source(rt,'blocked'))
        assert counts(rt)==before
        assert source(rt,'legacy-before')['idempotent']
        assert rt.load_ff_stock_operation(saved['operation_id'])['operation_id']==saved['operation_id']
        # Other native owners and types remain untouched by this exact manual predicate.
        source(rt,'native-supplier',source_type='supplier_shipment')
        source(rt,'native-wb',source_type='wb_supply_return')
    with TemporaryDirectory(prefix='manual-ff-fence-') as raw:
        rt,_=setup(raw)
        # Cutover commits on a different connection immediately before the writer fence.
        from packages.application import warehouse_business_projection as projection
        original=projection.ensure_warehouse_projection_source_outbox
        def intervening(conn):
            original(conn)
            cutover(rt)
        with patch.object(projection,'ensure_warehouse_projection_source_outbox',side_effect=intervening):
            refused(lambda:source(rt,'concurrent-cutover'))
        with sqlite3.connect(rt.db_path) as conn:
            assert not conn.execute("SELECT 1 FROM sheet_vitrina_v1_ff_stock_operations WHERE source_key='concurrent-cutover'").fetchone()
    # Native pool rows without a cutover manifest are unknown authority, not legacy.
    from apps.operator_inventory_documents_smoke import InventoryDocuments
    invalid=InventoryDocuments(methodName='runTest');invalid.setUp()
    try:
        invalid.set_catalog()
        block=FfStockLedgerBlock(runtime=invalid.runtime,timestamp_factory=lambda:NOW)
        before=invalid.runtime.db_path.read_bytes()
        try:block.parse_manual_operation_preview(workbook,operation_type='manual_receipt')
        except ModernFfWorkflowRequired as error:assert error.code=='manual_ff_authority_unavailable'
        else:raise AssertionError('unproven authority admitted a preview')
        assert before==invalid.runtime.db_path.read_bytes()
    finally:invalid.doCleanups()
    print('manual FF native: preview/confirm/direct source zero-write after native cutover; legacy exact reads/idempotent retained; other owners preserved; concurrent cutover fence: OK')

if __name__=='__main__': main()

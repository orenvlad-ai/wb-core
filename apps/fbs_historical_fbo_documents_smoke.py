#!/usr/bin/env python3
"""Invoice/XLSX custody, HTTP permission and no-new-expense checks, local only."""
from contextlib import closing
import hashlib
from http import HTTPStatus
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_historical_fbo_action_smoke as fixture
from packages.application import fbs_historical_fbo_documents as documents
from packages.application import operator_fulfillment_services as receipts
from packages.application.fulfillment_services import FulfillmentServicesBlock
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.adapters import registry_upload_http_entrypoint as web


class InvoiceDocumentTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.ActionTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        preview=self.fx.launch();result=self.fx.launch('apply',preview=preview)
        self.assertTrue(result['readback']['ready_complete'])
        self.block=FulfillmentServicesBlock(runtime=self.fx.runtime)
        with closing(_connect(self.fx.runtime.db_path)) as conn:
            self.uploads=[dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_fulfillment_service_uploads ORDER BY upload_id')]
        self.upload=self.uploads[0]

    def monetary_snapshot(self):
        with closing(receipts.readonly(self.fx.runtime.db_path)) as conn:
            names=['sheet_vitrina_v1_fulfillment_service_uploads','sheet_vitrina_v1_fulfillment_service_lines',receipts.TABLE,receipts.REQUESTS,
                'sheet_vitrina_v1_warehouse_business_operations','sheet_vitrina_v1_ff_pool_balances']
            return {name:[tuple(r) for r in conn.execute('SELECT * FROM '+name)] for name in names}

    def test_three_cards_show_originals_and_existing_xlsx_without_money_changes(self):
        before=self.monetary_snapshot();cards=self.block.list_uploads()['uploads'];self.assertEqual(len(cards),3)
        for card in cards:
            self.assertEqual(card['original_invoice_status'],'available')
            self.assertTrue(card['source_path'].endswith('/source.xlsx'))
            original=self.block.download_source_document(card['upload_id'],'original-invoice.pdf')
            xlsx=self.block.download_source_document(card['upload_id'],'source.xlsx')
            self.assertEqual(original[2],'application/pdf')
            self.assertEqual(hashlib.sha256(original[0]).hexdigest(),card['original_invoice']['sha256'])
            self.assertEqual(hashlib.sha256(xlsx[0]).hexdigest(),card['file_sha256'])
            self.assertIn('payment_request_id',card['original_invoice'])
        self.assertEqual(before,self.monetary_snapshot())

    def test_invoice_tamper_is_unavailable_and_cannot_download(self):
        with closing(receipts.readonly(self.fx.runtime.db_path)) as conn:
            result=documents.original_invoice(conn,runtime_dir=self.fx.root,upload_id=self.upload['upload_id'])
            row=conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE kind=?",('fbs_historical_fbo_effect_intent_v1',)).fetchone()
            doc=next(d for d in json.loads(row[0])['original_documents'] if d['invoice_sha256']==result[0]['sha256'])
        Path(doc['invoice_artifact_path']).write_bytes(b'%PDF-1.4\ntampered')
        detail=self.block.get_upload(self.upload['upload_id'])['upload'];self.assertEqual(detail['original_invoice_status'],'unavailable')
        with self.assertRaises(ValueError):self.block.download_source_document(self.upload['upload_id'],'original-invoice.pdf')
        self.assertTrue(self.block.download_source_document(self.upload['upload_id'],'source.xlsx')[0].startswith(b'PK'))

    def test_unrelated_upload_or_request_cannot_borrow_an_invoice(self):
        from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook
        extra=self.block.upload_xlsx(_build_workbook([['101','Synthetic',None,None,None,None,None,None,1,0]]),uploaded_filename='other.xlsx',request_id='unrelated-source',request_scope='other')['upload']
        self.assertEqual(self.block.get_upload(extra['upload_id'])['upload']['original_invoice_status'],'not_attached')
        with self.assertRaises(ValueError):self.block.download_source_document(extra['upload_id'],'original-invoice.pdf')
        # A mismatching accepted request cannot be substituted under a recipe.
        with closing(_connect(self.fx.runtime.db_path)) as conn:
            conn.execute(f'DROP TRIGGER {receipts.REQUESTS}_no_update')
            conn.execute(f"UPDATE {receipts.REQUESTS} SET payload_digest='wrong' WHERE upload_id=?",(self.upload['upload_id'],));conn.commit()
        self.assertEqual(self.block.get_upload(self.upload['upload_id'])['upload']['original_invoice_status'],'unavailable')

    def test_http_auth_before_file_read_and_exact_download(self):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        entry=RegistryUploadHttpEntrypoint(runtime_dir=self.fx.root,runtime=self.fx.runtime)
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=port,upload_path=web.DEFAULT_UPLOAD_PATH,
            sheet_plan_path=web.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path=web.DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=web.DEFAULT_SHEET_OPERATOR_UI_PATH,runtime_dir=self.fx.root)
        server=web.build_registry_upload_http_server(config,entrypoint=entry);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(timeout=5)))
        base='http://127.0.0.1:'+str(port)
        path=documents.BASE+self.upload['upload_id']+'/original-invoice.pdf'
        supply={'role':web.WEB_AUTH_ROLE_SUPPLY_OPERATOR,'allowed_sections':[web.WEB_AUTH_SECTION_SUPPLY]}
        settings={'role':web.WEB_AUTH_ROLE_OPERATOR,'allowed_sections':[web.WEB_AUTH_SECTION_SETTINGS]}
        self.assertTrue(web._user_can_access_path(supply,path));self.assertFalse(web._user_can_access_path(settings,path))
        def read(path):
            try:
                with urlopen(base+path) as response:return response.status,response.read()
            except HTTPError as exc:return exc.code,exc.read()
        def deny(handler,path):web._write_json_response(handler,HTTPStatus.FORBIDDEN,{'error':'forbidden'});return False
        with patch.object(web,'_ensure_supply_operator_role',side_effect=deny),patch.object(entry,'handle_fulfillment_services_source_document_request',side_effect=AssertionError('read before permission')):
            self.assertEqual(read(path)[0],403)
        self.assertEqual(read(path)[0],200)
        self.assertTrue(read(path)[1].startswith(b'%PDF-'))
        self.assertEqual(read(documents.BASE+'unrelated-source/original-invoice.pdf')[0],404)
        self.assertEqual(read(documents.BASE+self.upload['upload_id']+'/source.xlsx')[0],200)


if __name__=='__main__':unittest.main()

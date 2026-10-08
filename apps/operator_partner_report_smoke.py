"""Native Partner Report source/acknowledgement tests in temporary SQLite."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.partner_report_smoke import _seed_sources,_settings
from packages.application.partner_report import PartnerReportBlock
from packages.application import operator_partner_report as receipts,operator_operations as operations


class PartnerReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.block=PartnerReportBlock(Path(self.temp.name),seller_id='seller-1',
            now_factory=lambda:datetime(2026,10,8,10,tzinfo=timezone.utc))
        self.block.ensure_schema();_seed_sources(self.block.db_path)

    def save(self,identity='ops_partner_first',share='40',actor='report-user'):
        return self.block.save_settings({**_settings(),'partner_share_pct':share},actor=actor,operation_id=identity)

    def current(self):
        with closing(sqlite3.connect(self.block.db_path)) as conn:
            return conn.execute('SELECT settings_version_id FROM partner_report_settings_current ORDER BY nm_id').fetchall()

    def count(self,table):
        with closing(sqlite3.connect(self.block.db_path)) as conn:
            return conn.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_lost_response_same_identity_after_newer_save_keeps_current(self):
        first=self.save();second=self.save('ops_partner_second','41')
        self.block=PartnerReportBlock(Path(self.temp.name),seller_id='seller-1')
        self.assertEqual(self.save(),first)
        self.assertEqual(self.current(),[(second['settings_version_id'],)])
        self.assertEqual(self.count(receipts.TABLE),2)
        self.assertFalse(first['acceptance']['calculation_completed'])

    def test_unchanged_settings_keeps_native_version_but_audits_each_action(self):
        first=self.save();again=self.save('ops_partner_again')
        self.assertEqual(first['settings_version_id'],again['settings_version_id'])
        self.assertNotEqual(first['acceptance']['operation_id'],again['acceptance']['operation_id'])
        self.assertEqual(self.count('partner_report_settings_versions'),1)

    def test_conflicting_identity_parameters_actor_cannot_change_source(self):
        self.save();before=self.current()
        for share,actor in [('41','report-user'),('40','another-user')]:
            with self.assertRaisesRegex(ValueError,'identity_conflict'):self.save(share=share,actor=actor)
        self.assertEqual(self.current(),before);self.assertEqual(self.count(receipts.TABLE),1)

    def test_receipt_failure_rolls_back_source_and_audit(self):
        self.save();before=self.current()
        with patch.object(receipts,'record',side_effect=RuntimeError('fixture failed receipt')):
            with self.assertRaises(RuntimeError):self.save('ops_partner_failed','41')
        self.assertEqual(self.current(),before)
        self.assertEqual(self.count('partner_report_settings_versions'),1)
        self.assertEqual(self.count('partner_report_audit'),1)
        self.assertEqual(self.count(receipts.TABLE),1)

    def test_journal_permissions_before_counts_detail_and_safe_summary(self):
        value=self.save()['acceptance'];path=self.block.db_path
        # Read-only journal must not bootstrap or mutate the native database.
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(operations.journal(path,allowed_domains={'ff_pool_document'})['total'],0)
        self.assertIsNone(operations.read_acceptance(path,value['operation_id'],allowed_domains={'ff_pool_document'}))
        page=operations.journal(path,allowed_domains={receipts.DOMAIN})
        self.assertEqual(page['total'],1);self.assertEqual(page['items'][0],value)
        query=value['fields'][1]['value']
        self.assertEqual(operations.journal(path,allowed_sections={'reports'},search=query)['total'],1)
        self.assertEqual(operations.journal(path,allowed_sections={'supply'},search=query)['total'],0)
        self.assertEqual(operations.journal(path,allowed_sections={'reports'},search='literal%missing_')['total'],0)
        self.assertEqual(operations.read_acceptance(path,value['operation_id'],allowed_domains={receipts.DOMAIN}),value)
        self.assertNotIn('parameters',value);self.assertNotIn('invested_capital_rub',str(value))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_receipt_immutable_and_identity_namespace(self):
        self.save()
        with closing(sqlite3.connect(self.block.db_path)) as conn:
            for sql in ['DELETE FROM '+receipts.TABLE,'UPDATE '+receipts.TABLE+" SET actor='foreign'"]:
                with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):conn.execute(sql)
                conn.rollback()
        with self.assertRaisesRegex(ValueError,'identity'):self.save('ors_wrong_domain')

    def test_journal_of_legacy_settings_does_not_bootstrap_receipts(self):
        path=self.block.db_path;before=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(operations.journal(path,allowed_domains={receipts.DOMAIN})['total'],0)
        self.assertIsNone(operations.read_acceptance(path,'ops_partner_absent',allowed_domains={receipts.DOMAIN}))
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name=?",(receipts.TABLE,)).fetchone())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()

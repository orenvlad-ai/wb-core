"""Native immutable calculation/job/registry for the intercepted Balance UI.

The visual fixture supplies saved calculation facts; all operator acceptance,
selection, manifest, actor and fake-WB completion use the real native owner.
"""
from contextlib import closing
from copy import deepcopy
import json
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from apps.sku_inventory_balance_live_apply_smoke import _build_runtime, FakeLiveAdapter
from packages.application.sku_inventory_balance import CALCULATION_CONTRACT, FORMULA_VERSION, SkuInventoryBalanceBlock


class BalanceJobsFixture:
    def __init__(self, *, native_actor="fixture-browser-native"):
        self.native_actor=native_actor

    def __enter__(self):
        self.temp=TemporaryDirectory();self.adapter=FakeLiveAdapter()
        with patch.object(SkuInventoryBalanceBlock,'_start_apply_worker_if_needed'):
            self.block,_,_=_build_runtime(Path(self.temp.name),self.adapter)
        self.worker=patch.object(self.block,'_start_apply_worker_if_needed');self.worker.start()
        self.claim=None;self.job=None
        return self

    def __exit__(self,*exc):
        self.worker.stop();self.temp.cleanup()

    def project(self,visual):
        value=deepcopy(visual);identity=value['calculation_id']
        with closing(self.block._connect()) as conn:
            if not conn.execute('SELECT 1 FROM sheet_vitrina_v1_inventory_balance_calculations WHERE calculation_id=?',(identity,)).fetchone():
                conn.execute('INSERT INTO sheet_vitrina_v1_inventory_balance_calculations(calculation_id,contract_name,formula_version,source_digest,settings_json,payload_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)',
                    (identity,CALCULATION_CONTRACT,FORMULA_VERSION,'visual-synthetic-source','{}',json.dumps(value),value.get('created_at','2026-08-30T09:00:00Z'),self.native_actor))
                conn.commit()
        native=self.block.get_calculation(identity)
        by_key={t['target_key']:t for r in native['rows'] for t in r['campaign_recommendations']}
        for row in value['rows']:
            for target in row['campaign_recommendations']:
                saved=by_key[target['target_key']]
                if saved['manual_target_bid_rub'] != target.get('manual_target_bid_rub') and saved['manual_override_allowed']:
                    self.block.save_override(identity,dict(target_key=target['target_key'],manual_target_bid_rub=target.get('manual_target_bid_rub')),actor=self.native_actor)
        native=self.block.get_calculation(identity)
        # Preserve visual-only live availability toggles used in old UI tests.
        native['apply_capability']['live_wb_available']=value['apply_capability']['live_wb_available']
        return native

    def start(self,body):
        self.job=self.block.start_apply(body,actor=self.native_actor,operator_actor='fixture-browser')
        return self.job

    def next(self):
        if self.claim is None:
            self.claim=self.block._claim_next_live_job()
        elif self.job['stored_state'] != 'completed':
            self.block._run_live_job(*self.claim)
        self.job=self.block.get_apply_job(self.job['job_id'],actor=self.native_actor,operator_actor='fixture-browser')
        return self.job

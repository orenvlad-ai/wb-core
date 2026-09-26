"""Durable, explicit manual cleaner orchestration through Production Apply.

Only a saved self_service_requested event is eligible. Legacy queued runs are
never discovered or consumed by this worker.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from apps import search_cluster_cleaner_stage_e as stage_e
from apps.production_apply_contract import AmbiguousSubmit
from apps.production_apply_launcher import execute as production_apply
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.contracts.search_cluster_cleaner import CleanerError, Principal


ADAPTER_NAME='search_cluster_cleaner_manual_v1'


class LocalStageEAdapter:
    """The same Stage E contract as the SSH adapter, on the trusted host."""
    def __init__(self, *, runtime_dir:Path, env_file:Path, admission_dir:Path):
        self.runtime_dir=runtime_dir
        self.env_file=env_file
        self.admission_dir=admission_dir
        self.runtime_sha=(stage_e.ROOT/'.wb-core-runtime-sha').read_text(encoding='utf-8').strip()

    def _call(self,action:str,request:dict,operation_id:str,**expected) -> dict:
        envelope=dict(action=action,request=request,operation_id=operation_id,
                      expected_runtime_sha=self.runtime_sha,actor='owner-web-self-service',**expected)
        return stage_e.execute(envelope,runtime_dir=self.runtime_dir,env_file=self.env_file,admission_dir=self.admission_dir)

    def preview(self,request:dict,operation_id:str) -> dict:
        return self._call('preview',request,operation_id)

    def apply(self,request:dict,operation_id:str,preview:dict) -> dict:
        try:
            return self._call('apply',request,operation_id,expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        except Exception as exc:
            # An exception after entering Stage E cannot prove that a submit
            # did not happen. Production Apply will read this exact identity.
            raise AmbiguousSubmit('cleaner-local-submit-ambiguous') from exc

    def readback(self,request:dict,operation_id:str) -> dict:
        return self._call('readback',request,operation_id)


class ManualCleanerCoordinator:
    def __init__(self, cleaner:KeywordCleaner, adapter:LocalStageEAdapter, *, bootstrap_owner_username:str=''):
        self.cleaner=cleaner
        self.adapter=adapter
        self.bootstrap_owner_username=bootstrap_owner_username.strip().casefold()
        self.owner=Principal(cleaner.owner_username,True,True,True)

    def _actor_for_job(self,job_id:str) -> Principal:
        with self.cleaner.store.read() as c:
            request=c.execute("SELECT actor FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-clean'",
                              (self.cleaner.key,job_id)).fetchone()
            initial=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_requested' AND json_extract(facts,'$.job_id')=? ORDER BY sequence LIMIT 1",
                              (self.cleaner.key,job_id)).fetchone()
            settings=self.cleaner._settings(c)
        if not request or not initial:raise CleanerError('manual_job_authority_missing','Владелец ручной команды не подтверждён',409)
        facts=json.loads(initial['facts']);actor=request['actor']
        if (facts.get('actor')!=actor or facts.get('account_key',self.cleaner.key)!=self.cleaner.key
                or facts.get('generation',settings['generation'])!=settings['generation']):
            raise CleanerError('manual_job_authority_mismatch','Владелец ручной команды изменился',409)
        authority=facts.get('actor_authority','configured_owner')
        if authority=='bootstrap_operator':
            if not self.bootstrap_owner_username or actor!=self.bootstrap_owner_username:
                raise CleanerError('manual_job_authority_mismatch','Владелец ручной команды изменился',409)
            return Principal(actor,True,True,True,site_owner=True)
        if authority!='configured_owner' or actor!=self.cleaner.owner_username.strip().casefold():
            raise CleanerError('manual_job_authority_mismatch','Владелец ручной команды изменился',409)
        return Principal(actor,True,True,True)

    @staticmethod
    def operation_id(job_id:str,phase:str) -> str:
        # UUID-shaped browser request IDs are durable and unique. The suffix
        # keeps scan, prepare and write identities disjoint.
        return 'ui-'+job_id.lower().replace(':','-')+'-'+phase

    def _launch(self,action:str,operation_id:str,request:dict,**expected) -> dict:
        return production_apply(action=action,adapter_name=ADAPTER_NAME,operation_id=operation_id,
                                request=request,adapters={ADAPTER_NAME:self.adapter},**expected)

    def _save(self,job_id:str,**facts) -> None:
        self.cleaner.record_manual_job(job_id,**facts)

    def _job(self,job_id:str) -> dict:
        self.owner=self._actor_for_job(job_id)
        return self.cleaner.manual_job(job_id,self.owner)

    def pending_jobs(self) -> list[str]:
        with self.cleaner.store.read() as c:
            rows=c.execute("SELECT json_extract(facts,'$.job_id') AS job_id FROM cleaner_events WHERE account=? AND kind='self_service_requested' ORDER BY sequence",(self.cleaner.key,)).fetchall()
            pending=[]
            for row in rows:
                latest=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%' AND json_extract(facts,'$.job_id')=? ORDER BY sequence DESC LIMIT 1",(self.cleaner.key,row['job_id'])).fetchone()
                state=json.loads(latest['facts']).get('state','queued') if latest else 'queued'
                if state not in {'complete','partial','failed','no_change'}:pending.append(row['job_id'])
        return pending

    def tick(self) -> dict|None:
        jobs=self.pending_jobs()
        if not jobs:return None
        job_id=jobs[0]
        try:self._advance(job_id)
        except CleanerError as exc:
            job=self._job(job_id)
            if job.get('stage','').endswith('_apply_claimed'):
                self._settle(job_id,job['stage'].split('_')[0],'ambiguous',{})
            else:
                self._release_unsubmitted(job)
                self._save(job_id,state='failed',stage='finished',error_code=exc.code,error=str(exc))
        except (OSError,ValueError,RuntimeError) as exc:
            # Preserve an apply claim for readback-only recovery. Other stages
            # can be retried by a later worker tick with the same identity.
            job=self._job(job_id)
            if job.get('stage','').endswith('_apply_claimed'):
                self._settle(job_id,job['stage'].split('_')[0],'ambiguous',{})
            else:
                self._release_unsubmitted(job)
                self._save(job_id,state='failed',stage='finished',error_code=type(exc).__name__,error='Не удалось завершить ручную чистку')
        return self._job(job_id)

    def _release_unsubmitted(self,job:dict) -> None:
        stage=job.get('stage','')
        if stage.startswith('scan') or stage=='fetching':
            if not job.get('adopted'):self.cleaner.stop_unsubmitted_manual_run(job['scan_run_id'])
        elif stage.startswith('write') and job.get('write_run_id'):
            self.cleaner.stop_unsubmitted_manual_run(job['write_run_id'])

    def _advance(self,job_id:str) -> None:
        job=self._job(job_id)
        target=dict(advert_id=job['advert_id'],nm_id=job['nm_id'])
        targets=[target]
        scan=dict(mode='manual',run_id=job['scan_run_id'],targets=targets)
        scan_op=self.operation_id(job_id,'scan')
        stage=job['stage']
        if stage=='fetching':
            preview=self._launch('preview',scan_op,scan)
            self._save(job_id,state='running',stage='scan_ready',scan_prestate=preview['prestate_sha256'],scan_candidate=preview['candidate_sha256'])
            return
        if stage=='scan_ready':
            self._save(job_id,state='running',stage='scan_apply_claimed')
            self._apply_claimed(job_id,scan_op,scan,'scan')
            return
        if stage=='scan_apply_claimed':
            self._readback_claimed(job_id,scan_op,scan,'scan')
            return
        if stage=='classifying':
            try:preview=self.cleaner.manual_apply_preview(job['scan_run_id'],stage_e.Target(**target))
            except CleanerError as exc:
                if exc.code=='manual_candidates_empty':
                    self._save(job_id,state='no_change',stage='finished',result='Нет новых допустимых фраз для исключения')
                    return
                raise
            self._save(job_id,state='running',stage='prepare_ready',candidate_count=len(preview['candidates']))
            return
        prepare=dict(mode='manual_prepare',scan_run_id=job['scan_run_id'],targets=targets)
        prepare_op=self.operation_id(job_id,'prepare')
        if stage=='prepare_ready':
            preview=self._launch('preview',prepare_op,prepare)
            self._save(job_id,state='running',stage='prepare_previewed',prepare_prestate=preview['prestate_sha256'],prepare_candidate=preview['candidate_sha256'])
            return
        if stage=='prepare_previewed':
            self._save(job_id,state='running',stage='prepare_apply_claimed')
            self._apply_claimed(job_id,prepare_op,prepare,'prepare')
            return
        if stage=='prepare_apply_claimed':
            self._readback_claimed(job_id,prepare_op,prepare,'prepare')
            return
        if stage=='write_previewing':
            write=dict(mode='manual',run_id=job['write_run_id'],targets=targets)
            preview=self._launch('preview',self.operation_id(job_id,'write'),write)
            self._save(job_id,state='running',stage='write_ready',write_prestate=preview['prestate_sha256'],write_candidate=preview['candidate_sha256'])
            return
        if stage=='write_ready':
            self._save(job_id,state='running',stage='write_apply_claimed')
            write=dict(mode='manual',run_id=job['write_run_id'],targets=targets)
            self._apply_claimed(job_id,self.operation_id(job_id,'write'),write,'write')
            return
        if stage=='write_apply_claimed':
            write=dict(mode='manual',run_id=job['write_run_id'],targets=targets)
            self._readback_claimed(job_id,self.operation_id(job_id,'write'),write,'write')

    def _apply_claimed(self,job_id:str,operation_id:str,request:dict,phase:str) -> None:
        job=self._job(job_id)
        try:
            receipt=self._launch('apply',operation_id,request,expected_prestate=job[phase+'_prestate'],expected_candidate=job[phase+'_candidate'])
        except Exception:
            self._readback_claimed(job_id,operation_id,request,phase)
            return
        self._settle(job_id,phase,receipt['state'],receipt)

    def _readback_claimed(self,job_id:str,operation_id:str,request:dict,phase:str) -> None:
        job=self._job(job_id)
        if float(job.get('next_readback_at') or 0)>time.time():return
        if int(job.get('readback_attempts') or 0)>=20:
            self._save(job_id,state='partial',stage=phase+'_apply_claimed',can_recheck=True,
                       error_code='readback_unresolved',error='Результат WB не подтверждён. Доступна повторная проверка той же операции.')
            return
        receipt=self._launch('readback',operation_id,request)
        self._settle(job_id,phase,receipt['state'],receipt)

    def _settle(self,job_id:str,phase:str,state:str,receipt:dict) -> None:
        if state=='ambiguous':
            attempts=int(self._job(job_id).get('readback_attempts') or 0)+1
            self._save(job_id,state='ambiguous',stage=phase+'_apply_claimed',result='Ожидаем подтверждения WB',
                       readback_attempts=attempts,next_readback_at=time.time()+min(30,2**min(attempts,5)))
            return
        if state=='not_submitted':
            self._release_unsubmitted(self._job(job_id))
            self._save(job_id,state='failed',stage='finished',error_code=phase+'_not_submitted',error='Запуск не подтверждён; повторная отправка заблокирована')
            return
        if phase=='scan':
            if state=='failed':
                detail=self.cleaner.run_detail(self._job(job_id)['scan_run_id'],self.owner)
                self._save(job_id,state='failed',stage='finished',error_code=detail['reason'] or 'scan_partial',
                           error='Ручная проверка не завершилась полностью')
                return
            if state not in {'no_change','applied'}:raise CleanerError('scan_failed','Не удалось проверить ключи',409)
            self._save(job_id,state='running',stage='classifying',scan_result=state)
            return
        if phase=='prepare':
            if state!='applied':
                self._save(job_id,state='failed',stage='finished',error_code='prepare_failed',
                           error='Не удалось подготовить точные фразы')
                return
            run_id=(receipt.get('submit') or {}).get('run_id') or (receipt.get('readback') or {}).get('run_id')
            if not run_id:raise CleanerError('prepare_run_missing','Не найдено подготовленное применение',409)
            self._save(job_id,state='running',stage='write_previewing',write_run_id=run_id)
            return
        if phase=='write':
            detail=self.cleaner.run_detail(self._job(job_id)['write_run_id'],self.owner)
            final='complete' if state=='applied' and detail['effective_state']=='complete' else 'partial'
            self._save(job_id,state=final,stage='finished',result=state,write_run_id=detail['run_id'])

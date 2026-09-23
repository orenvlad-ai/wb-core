"""Trusted SSH adapter for governed manual-only search-cluster cleaning."""
from __future__ import annotations
import json, os, shlex, subprocess, tempfile
from pathlib import Path
from typing import Any, Mapping
from apps.github_release_runner import RunnerError, configure_ssh, trusted_main_sha
from apps.production_apply_contract import AdapterError, AmbiguousSubmit

ROOT=Path(__file__).resolve().parents[1]
TARGET_PATH=ROOT/'artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json'
REMOTE_APP='/opt/wb-core-runtime/app/apps/search_cluster_cleaner_stage_e.py'
REMOTE_RUNTIME_DIR='/opt/wb-core-runtime/state'
REMOTE_ENV_FILE='/opt/wb-ai/.env'
REMOTE_ADMISSION_DIR='/var/lib/wb-core/search-cluster-cleaner-admission'

class SearchClusterCleanerProductionAdapter:
    def preview(self,request:dict[str,Any],operation_id:str)->dict[str,Any]:return self._invoke(action='preview',request=request,operation_id=operation_id)
    def apply(self,request:dict[str,Any],operation_id:str,preview:dict[str,Any])->dict[str,Any]:return self._invoke(action='apply',request=request,operation_id=operation_id,expected_prestate=str(preview.get('prestate_sha256') or ''),expected_candidate=str(preview.get('candidate_sha256') or ''))
    def readback(self,request:dict[str,Any],operation_id:str)->dict[str,Any]:return self._invoke(action='readback',request=request,operation_id=operation_id)
    def _invoke(self,*,action:str,request:Mapping[str,Any],operation_id:str,expected_prestate:str='',expected_candidate:str='')->dict[str,Any]:
        if not isinstance(request,Mapping):raise AdapterError('cleaner-manual-request-invalid')
        mode=request.get('mode');allowed={'bootstrap':{'mode'},'bootstrap_recover':{'mode','original_operation_id'},'manual':{'mode','run_id','targets'},'manual_prepare':{'mode','scan_run_id','targets'}}
        if mode not in allowed or set(request)!=allowed[mode]:raise AdapterError('cleaner-manual-request-invalid')
        target=json.loads(TARGET_PATH.read_text(encoding='utf-8'));destination=str(target.get('ssh_destination') or '').strip()
        if target.get('target_status')!='active' or target.get('target_role')!='primary_live' or target.get('target_lifecycle')!='current_live' or destination!='wb-core-eu-root':raise AdapterError('production-target-identity-invalid')
        envelope=dict(action=action,operation_id=operation_id,request=dict(request),expected_prestate=expected_prestate,expected_candidate=expected_candidate,expected_runtime_sha=trusted_main_sha(),actor='github-actions:'+str(os.environ.get('GITHUB_ACTOR') or 'unknown')[:120])
        with tempfile.TemporaryDirectory(prefix='search-cluster-cleaner-') as raw:
            try:configure_ssh(Path(raw))
            except RunnerError as exc:raise AdapterError(exc.reason) from exc
            command=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=10','-o','ServerAliveCountMax=2','-i',os.environ.get('WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE','').strip(),*shlex.split(os.environ.get('WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS','').strip()),destination,'python3',REMOTE_APP,'--runtime-dir',REMOTE_RUNTIME_DIR,'--env-file',REMOTE_ENV_FILE,'--admission-dir',REMOTE_ADMISSION_DIR]
            try:completed=subprocess.run(command,input=json.dumps(envelope,ensure_ascii=False,sort_keys=True,separators=(',',':')),text=True,capture_output=True,timeout=180,check=False)
            except subprocess.TimeoutExpired as exc:
                if action=='apply':raise AmbiguousSubmit('cleaner-manual-submit-timeout') from exc
                raise AdapterError('cleaner-manual-transport-timeout') from exc
        try:payload=json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            if action=='apply':raise AmbiguousSubmit('cleaner-manual-submit-output-ambiguous') from exc
            raise AdapterError('cleaner-manual-response-invalid') from exc
        if not isinstance(payload,dict) or completed.returncode!=0 or payload.get('status')=='blocked':
            code=str((payload.get('error') or {}).get('code') or 'cleaner-manual-remote-blocked') if isinstance(payload,dict) else 'cleaner-manual-response-invalid'
            # A remote failure after the submit boundary is never proof that
            # WB/SQLite stayed unchanged. Only readback of this exact operation
            # may resolve it.
            if action=='apply':raise AmbiguousSubmit('cleaner-manual-submit-ambiguous')
            raise AdapterError(code)
        return payload

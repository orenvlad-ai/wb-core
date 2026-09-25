#!/usr/bin/env python3
"""Synthetic release readback checks with no WB or production access."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import search_cluster_cleaner_release_probe as probe
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox,RUNTIME_SHA


class Handler(BaseHTTPRequestHandler):
    allow_auth=False
    def do_GET(self):  # noqa: N802
        code=200 if self.allow_auth else 401
        if self.path!=probe.SUMMARY_PATH:code=404
        self.send_response(code);self.end_headers()
    def log_message(self,*args):pass


def main():
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        server=HTTPServer(('127.0.0.1',0),Handler)
        original=probe.LISTENER_BASE
        probe.LISTENER_BASE=f'http://127.0.0.1:{server.server_port}'
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            health=box.admission/'self-service-worker-health.json'
            def write_health(state):health.write_text(json.dumps(dict(pid=os.getpid(),state=state,updated_at=time.time())))
            write_health('armed')
            (box.app/'.wb-core-deploy.json').write_text(json.dumps(dict(commit=RUNTIME_SHA,deployment_complete=False)))
            assert probe.check(phase='before_complete',expected_sha=RUNTIME_SHA,runtime_dir=box.runtime,admission_dir=box.admission,app_dir=box.app,timeout_seconds=1)['worker']=='armed'
            Handler.allow_auth=True
            try:probe.check(phase='before_complete',expected_sha=RUNTIME_SHA,runtime_dir=box.runtime,admission_dir=box.admission,app_dir=box.app,timeout_seconds=1)
            except RuntimeError as exc:assert 'auth boundary' in str(exc)
            else:raise AssertionError('unauthenticated access was admitted')
            Handler.allow_auth=False
            write_health('ready')
            completed=dict(commit=RUNTIME_SHA,deployment_complete=True,deployed_at='synthetic',schema_version='wb_core_deploy_metadata_v2')
            (box.app/'.wb-core-deploy.json').write_text(json.dumps(completed))
            assert probe.check(phase='after_complete',expected_sha=RUNTIME_SHA,runtime_dir=box.runtime,admission_dir=box.admission,app_dir=box.app,timeout_seconds=1)['worker']=='ready'
            with box.service().store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=1 WHERE account=?',(box.service().key,))
            try:probe.check(phase='after_complete',expected_sha=RUNTIME_SHA,runtime_dir=box.runtime,admission_dir=box.admission,app_dir=box.app,timeout_seconds=1)
            except RuntimeError as exc:assert 'setting mismatch' in str(exc)
            else:raise AssertionError('enabled schedule was admitted')
            assert json.loads((box.app/'.wb-core-deploy.json').read_text())==completed
        finally:
            probe.LISTENER_BASE=original
            server.shutdown();server.server_close();thread.join(2)
    print('search cluster cleaner release probe smoke: ok')


if __name__=='__main__':main()

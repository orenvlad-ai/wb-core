#!/usr/bin/env python3
"""Cleaner status remains responsive while the primary HTTP server is busy."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from http.server import HTTPServer
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from apps import registry_upload_http_entrypoint_live as live
from apps.search_cluster_cleaner_web_fixture import running_fixture
from packages.adapters.search_cluster_cleaner_http import PREFIX,handles
from packages.adapters.registry_upload_http_entrypoint import RegistryUploadHttpServer
from packages.contracts.search_cluster_cleaner import Account


def main() -> None:
    # An unrelated registry process must never try to bind the cleaner port.
    with patch.object(live,'RegistryUploadHttpServer',side_effect=AssertionError('8776 bind attempted')):
        empty=SimpleNamespace(cleaner_web=SimpleNamespace(cleaner=None))
        assert live.start_cleaner_contour(SimpleNamespace(),empty,Path('/other/runtime'))==(None,None,None)
        with TemporaryDirectory() as folder:
            config=Path(folder)/'stage-e-config.json'
            config.write_text('{"seller_id":"other","account_scope":"scope","generation":"monolith","owner_username":"owner","approved_package_path":"/private/package"}')
            foreign=SimpleNamespace(cleaner_web=SimpleNamespace(cleaner=SimpleNamespace(account=Account('seller','scope'),owner_username='owner'),generation='monolith'))
            with patch.object(live,'STAGE_E_CONFIG_PATH',config):
                assert live.start_cleaner_contour(SimpleNamespace(),foreign,Path('/other/runtime'))==(None,None,None)
    with running_fixture() as fixture:
        owner=fixture.login()
        original=fixture.server.RequestHandlerClass
        started=threading.Event()
        class SlowPrimary(original):
            def do_GET(self):  # noqa: N802
                if urlsplit(self.path).path=='/synthetic-slow-composition':
                    started.set();time.sleep(3)
                    self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok')
                    return
                super().do_GET()
        fixture.server.RequestHandlerClass=SlowPrimary
        class CleanerOnly(SlowPrimary):
            def do_GET(self):  # noqa: N802
                if not handles(urlsplit(self.path).path):self.send_error(404);return
                super().do_GET()
            def do_POST(self):  # noqa: N802
                if not handles(urlsplit(self.path).path):self.send_error(404);return
                super().do_POST()
        sidecar=RegistryUploadHttpServer(('127.0.0.1',0),CleanerOnly)
        thread=threading.Thread(target=sidecar.serve_forever,daemon=True);thread.start()
        try:
            slow=threading.Thread(target=lambda:urllib.request.urlopen(fixture.base_url+'/synthetic-slow-composition').read(),daemon=True)
            slow.start();assert started.wait(2)
            began=time.monotonic()
            with owner.open(f'http://127.0.0.1:{sidecar.server_port}{PREFIX}/summary',timeout=2) as response:
                assert response.status==200
                assert b'configuration' in response.read()
            assert time.monotonic()-began<1.0
            slow.join(5)
        finally:
            sidecar.shutdown();sidecar.server_close();thread.join(3)
    print('search cluster cleaner sidecar smoke: ok')


if __name__=='__main__':main()

"""Synthetic loopback WB and isolated operational/admission stores for D tests."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.parse import urlparse,parse_qs
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource,AccountLimiter
from packages.application.storage_registry import StoreRegistry
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner_admission import AdmissionGuard
from packages.application.search_cluster_cleaner_writer import CleanerWriter,CleanerReadback
from packages.application.search_cluster_cleaner_worker import CleanerWorker
from packages.contracts.search_cluster_cleaner import Account,Principal,Target,digest

OWNER=Principal('owner',True,True,True)
Q1='стекло iphone 15 pro max';Q2='Стекло iphone 14 pro max'
PROFILE=dict(nm_id=101,version=1,category='phone_screen_glass',models=['16 promax'],kind='clean',frame='black',source='synthetic D fixture',verified_at='2026-09-11T00:00:00Z')


class Clock:
    def __init__(self):self.seconds=0;self.base=datetime(2026,9,11,0,0,tzinfo=timezone.utc)
    def __call__(self):return (self.base+timedelta(seconds=self.seconds)).isoformat(timespec='microseconds')
    def advance(self,seconds):self.seconds+=seconds
    def monotonic(self):return self.seconds


class FakeWB:
    def __init__(self):
        self.targets={11:dict(nm=101,stats=[Q1,Q2],active=[],minus=[' Старое исключение  ','OLD'],archived=[],bid='manual',status=9,payment='cpm',members=[101])}
        self.calls=[];self.writes=[];self.mode='normal';self.codes={};self.slow=False;self.on_call=None
    def response(self,method,path,body):
        endpoint=urlparse(path).path;self.calls.append((method,endpoint,body))
        if self.on_call:self.on_call(endpoint,body)
        if endpoint in self.codes:return self.codes[endpoint],{'error':'fixture'}
        if endpoint.endswith('/count'):return 200,dict(all=len(self.targets),adverts=[dict(count=len(self.targets),type=9,status=9,advert_list=[dict(advertId=i) for i in self.targets])])
        if endpoint.endswith('/adverts'):
            ids=list(map(int,parse_qs(urlparse(path).query)['ids'][0].split(',')))
            return 200,dict(adverts=[dict(id=i,bid_type=v['bid'],status=v['status'],settings=dict(payment_type=v['payment'],name='Synthetic target'),nm_settings=[dict(nm_id=n) for n in v['members']]) for i,v in self.targets.items() if i in ids])
        if endpoint.endswith('/set-minus'):
            self.writes.append(body);v=self.targets[body['advert_id']];before=list(v['minus']);new=body['norm_queries']
            if self.mode=='normal':v['minus']=new
            elif self.mode=='partial':v['minus']=before+[q for q in new if q not in before][:1]
            elif self.mode=='missing_old':v['minus']=[q for q in new if q!=before[0]]
            elif self.mode=='extra':v['minus']=new+['Чужая точная строка']
            elif self.mode=='timeout':v['minus']=new;return 'disconnect',{}
            elif self.mode in {'429','500','401','403','302'}:return int(self.mode),{}
            return 200,{}
        pair=body['items'][0];aid=pair.get('advert_id',pair.get('advertId'));nm=pair.get('nm_id',pair.get('nmId'));v=self.targets[aid]
        if endpoint.endswith('/list'):return 200,dict(items=[dict(advertId=aid,nmId=nm,normQueries=dict(active=[q for q in v['active'] if q not in v['minus']],excluded=v['minus'],archived=v['archived']))])
        if endpoint.endswith('/stats'):return 200,dict(stats=[dict(advert_id=aid,nm_id=nm,stats=[dict(norm_query=q,views=1) for q in v['stats']])])
        if endpoint.endswith('/get-minus'):return 200,dict(items=[dict(advert_id=aid,nm_id=nm,norm_queries=v['minus'])])
        return 404,{}

    @contextmanager
    def server(self):
        fake=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.handle_request()
            def do_POST(self):self.handle_request()
            def handle_request(self):
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'null')
                status,value=fake.response(self.command,self.path,body)
                if status=='disconnect':self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                payload=json.dumps(value,ensure_ascii=False).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)))
                if status==429:self.send_header('Retry-After','7')
                if status==302:self.send_header('Location','/adv/v0/normquery/set-minus')
                self.end_headers()
                try:
                    if fake.slow:
                        for byte in payload:self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.03)
                    else:self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:yield 'http://127.0.0.1:'+str(server.server_port)
        finally:server.shutdown();server.server_close();thread.join(2)


class Fixture:
    def __init__(self,root,url,fake):
        self.root,self.fake=Path(root),fake;self.clock=Clock();self.account=Account('synthetic-seller','synthetic-ads')
        (self.root/'business').mkdir()
        self.store=CleanerStore(StoreRegistry(self.root/'business'))
        self.app=KeywordCleaner(self.store,self.account,owner_username='owner',clock=self.clock);self.app.initialize(generation='g1')
        ChangeRegistryRepository(self.store.registry.runtime_dir).initialize_schema()
        rows=[dict(advert_id=1,nm_id=101,query='стекло iphone 16 pro max',decision='allow',observed_state='active',provenance={'fixture':True})]
        self.app.import_baseline(rows,[PROFILE],provenance={'fixture':True},expected_digest=digest(rows),ready=True)
        with self.store.transaction() as c:c.execute('UPDATE cleaner_settings SET restore_hold=0 WHERE account=?',(self.app.key,))
        self.app.update_settings(dict(request_id='fixture-enable-0001',expected_revision=1,enabled=True),OWNER)
        self.guard=AdmissionGuard(self.root/'server-owned',self.store);self.guard.activate(account=self.account,generation='g1',evidence='synthetic fixture only')
        self.limiter=AccountLimiter(monotonic=self.clock.monotonic,sleep=self.clock.advance)
        self.source=CleanerWbSource(account=self.account,runtime=OfficialApiRuntimeConfig('synthetic-not-a-token',url,2),limiter=self.limiter,clock=self.clock,monotonic=self.clock.monotonic,fixture=True)
        self.counter=0
    def start(self):
        self.counter+=1;return self.app.start_run(dict(request_id='fixture-run-'+str(self.counter).zfill(4)),OWNER)['run_id']
    @contextmanager
    def worker(self,**options):
        with self.guard.session(account=self.account,generation='g1') as session:
            writer=CleanerWriter(self.app,self.source,session,generation='g1',hook=options.pop('hook',None),registry=options.pop('registry',None))
            readback=CleanerReadback(self.app,self.source,generation='g1')
            yield CleanerWorker(self.app,self.source,generation='g1',writer=writer,readback=readback,monotonic=self.clock.monotonic,**options),writer,readback
    def count(self,table):
        with self.store.read() as c:return c.execute('SELECT count(*) FROM '+table).fetchone()[0]
    def rows(self,table):
        with self.store.read() as c:return [dict(r) for r in c.execute('SELECT * FROM '+table)]


@contextmanager
def fixture():
    fake=FakeWB()
    with tempfile.TemporaryDirectory(prefix='cleaner-D-') as root, fake.server() as url:
        yield Fixture(root,url,fake)

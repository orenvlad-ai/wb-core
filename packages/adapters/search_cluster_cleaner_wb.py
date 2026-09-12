"""Validated manual-CPM WB port. set-minus has no redirect or automatic retry.

No adapter is instantiated by web routes. Production binding uses the canonical
server runtime and seller; the fixture constructor accepts only loopback URLs.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import http.client
import io
import os
import threading
import time
from urllib import parse
from packages.adapters.official_api_runtime import load_runtime_config, DEFAULT_WB_API_TOKEN_ENV
from packages.contracts.search_cluster_cleaner import Account, CleanerError, Target, Snapshot, query_hash, utcnow
from packages.domain.search_cluster_sources import union_snapshot


class WbReadError(CleanerError):
    def __init__(self,code,status=None,retry_after=0):
        super().__init__(code,'WB: '+code,503);self.status=status;self.retry_after=retry_after


@dataclass(frozen=True)
class WriteResponse:
    status: int | None
    retry_after: float = 0
    error: str = ''


class AccountLimiter:
    _instances={};_registry_lock=threading.Lock()
    @classmethod
    def shared(cls,account):
        with cls._registry_lock:return cls._instances.setdefault(account,cls())
    def __init__(self,*,monotonic=time.monotonic,sleep=time.sleep,interval=0.5):
        self.monotonic,self.sleep,self.interval=monotonic,sleep,interval;self.next_at=0;self.stats_at=0;self.lock=threading.Lock()
    def wait(self,deadline,*,statistics=False):
        with self.lock:
            now=self.monotonic();delay=max(0,self.next_at-now,(self.stats_at-now) if statistics else 0)
            if now+delay>=deadline:raise WbReadError('read_budget')
            if delay:self.sleep(delay)
            self.next_at=self.monotonic()+self.interval
            if statistics:self.stats_at=self.monotonic()+6.1
    def backoff(self,seconds):
        with self.lock:self.next_at=max(self.next_at,self.monotonic()+max(0,seconds))


class DeadlineRaw(io.RawIOBase):
    def __init__(self,sock,deadline,monotonic):
        self.sock,self.deadline,self.monotonic=sock,deadline,monotonic
        self.file=sock.makefile('rb',buffering=0)
    def close(self):
        self.file.close();super().close()
    def readable(self):return True
    def readinto(self,buffer):
        remaining=self.deadline-self.monotonic()
        if remaining<=0:raise TimeoutError('WB receive deadline')
        self.sock.settimeout(remaining)
        return self.file.readinto(buffer)


class DeadlineSocket:
    def __init__(self,sock,deadline,monotonic):self.sock,self.deadline,self.monotonic=sock,deadline,monotonic
    def makefile(self,*args,**kwargs):return io.BufferedReader(DeadlineRaw(self.sock,self.deadline,self.monotonic))


class CleanerWbSource:
    def __init__(self,*,account,runtime,limiter=None,clock=utcnow,monotonic=time.monotonic,fixture=False):
        url=parse.urlparse(runtime.base_url)
        valid=url.scheme=='https' and url.netloc=='advert-api.wildberries.ru' and not url.path
        if fixture:valid=url.scheme=='http' and url.hostname in {'127.0.0.1','::1'} and not url.path
        if not valid:raise CleanerError('account_mismatch','Неподтверждённый адрес WB')
        self.account,self.runtime,self.clock,self.monotonic=account,runtime,clock,monotonic
        self.limiter=limiter or AccountLimiter.shared(account.key)
        self._slot=None;self._target_deadline=None
        self.timeout=min(float(runtime.timeout_seconds),20)
        if self.timeout<=0:raise CleanerError('invalid_timeout','Неверный таймаут')

    @classmethod
    def from_env(cls,account):
        runtime=load_runtime_config(token_env_var=DEFAULT_WB_API_TOKEN_ENV,default_base_url='https://advert-api.wildberries.ru',base_url_env_var='WB_ADVERT_API_BASE_URL')
        try:
            body=runtime.token.split('.')[1];claims=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)))
            if (claims.get('sid')!=account.seller_id or os.environ.get('SELLER_PORTAL_CANONICAL_SUPPLIER_ID')!=account.seller_id
                    or os.environ.get('CLEANER_ACCOUNT_SCOPE')!=account.account_scope
                    or os.environ.get('CHANGE_REGISTRY_ACCOUNT_SCOPE',account.account_scope)!=account.account_scope):raise ValueError()
        except (ValueError,IndexError,TypeError):raise CleanerError('account_mismatch','Каноническая привязка токена не совпала',403)
        return cls(account=account,runtime=runtime)

    @contextmanager
    def target_attempt(self):
        previous=self._target_deadline
        self._target_deadline=min(previous,self.monotonic()+120) if previous is not None else self.monotonic()+120
        try:yield
        finally:self._target_deadline=previous

    def require_target_budget(self):
        if self._target_deadline is not None and self.monotonic()>=self._target_deadline:raise WbReadError('target_budget')

    def _retry_after(self,headers):
        raw=headers.get('Retry-After','0')
        try:return max(0,float(raw))
        except ValueError:
            try:return max(0,(parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds())
            except (ValueError,TypeError):return 60

    def _call(self,method,path,payload=None,*,deadline,write=False):
        if self._target_deadline is not None:deadline=min(deadline,self._target_deadline)
        if not write:self.limiter.wait(deadline,statistics=path.endswith("/normquery/stats"))
        deadline=min(deadline,self.monotonic()+self.timeout)
        timeout=min(self.timeout,deadline-self.monotonic())
        if timeout<=0:raise WbReadError('read_budget')
        parsed=parse.urlparse(self.runtime.base_url)
        connection_cls=http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection
        connection=connection_cls(parsed.hostname,parsed.port,timeout=timeout)
        # Absolute receive deadline applies to every recv, including headers and
        # trickling bodies; a per-socket timeout alone is insufficient.
        monotonic=self.monotonic
        connection.response_class=lambda sock,**kw: http.client.HTTPResponse(DeadlineSocket(sock,deadline,monotonic),**kw)
        body=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
        try:
            connection.request(method,path,body=body,headers={'Authorization':self.runtime.token,'Content-Type':'application/json','Accept':'application/json'})
            response=connection.getresponse()
            status=response.status;delay=self._retry_after(response.headers)
            if status==429:self.limiter.backoff(max(delay,1))
            if write:
                # Response headers are only receipt evidence; body is irrelevant.
                return WriteResponse(status,delay,'http_error' if status!=200 else '')
            if status!=200:
                raise WbReadError({401:'unauthorized',403:'forbidden',429:'rate_limited'}.get(status,'http_error'),status,delay)
            raw=response.read(16*1024*1024+1)
            if len(raw)>16*1024*1024 or self.monotonic()>deadline:raise WbReadError('read_budget')
            try:return json.loads(raw)
            except ValueError:raise WbReadError('response_malformed')
        except (TimeoutError,ConnectionError,OSError,http.client.HTTPException):
            if write:return WriteResponse(None,0,'transport_ambiguous')
            raise WbReadError('source_temporarily_unavailable') from None
        finally:connection.close()

    def _adverts(self,ids,deadline,*,strict=True):
        body=self._call('GET','/api/advert/v2/adverts?'+parse.urlencode({'ids':','.join(map(str,ids))}),deadline=deadline)
        if not isinstance(body,dict) or not isinstance(body.get('adverts'),list):raise WbReadError('adverts_malformed')
        result=[];seen=set()
        for ad in body['adverts']:
            if not isinstance(ad,dict) or type(ad.get('id')) is not int or ad['id'] not in ids or ad['id'] in seen:raise WbReadError('adverts_identity')
            seen.add(ad['id']);settings=ad.get('settings');nms=ad.get('nm_settings')
            if (not isinstance(settings,dict) or not isinstance(settings.get('payment_type'),str) or not isinstance(ad.get('bid_type'),str)
                    or type(ad.get('status')) is not int or not isinstance(nms,list) or not nms):raise WbReadError('adverts_malformed')
            nm_seen=set()
            for item in nms:
                if not isinstance(item,dict) or type(item.get('nm_id')) is not int or item['nm_id']<=0 or item['nm_id'] in nm_seen:raise WbReadError('adverts_nm_identity')
                nm_seen.add(item['nm_id'])
                result.append(Target(ad['id'],item['nm_id'],settings['payment_type'],ad['bid_type'],ad['status'],str(settings.get('name','')),ad['bid_type']=='manual'))
        missing=sorted(set(ids)-seen)
        if missing and strict:raise WbReadError('adverts_missing')
        return result if strict else (result,['adverts_missing:'+str(i) for i in missing])

    def catalog(self):
        deadline=self.monotonic()+120
        payload=self._call('GET','/adv/v1/promotion/count',deadline=deadline)
        if not isinstance(payload,dict) or type(payload.get('all')) is not int or not isinstance(payload.get('adverts'),list):raise WbReadError('count_malformed')
        ids=[]
        for group in payload['adverts']:
            if (not isinstance(group,dict) or type(group.get('count')) is not int or type(group.get('status')) is not int
                    or type(group.get('type')) is not int or not isinstance(group.get('advert_list'),list) or group['count']!=len(group['advert_list'])):raise WbReadError('count_malformed')
            for item in group['advert_list']:
                if not isinstance(item,dict) or type(item.get('advertId')) is not int or item['advertId']<=0:raise WbReadError('count_identity')
                ids.append(item['advertId'])
        if len(ids)!=len(set(ids)) or len(ids)!=payload['all']:raise WbReadError('count_incomplete')
        targets=[];errors=[]
        for offset in range(0,len(ids),50):
            try:
                values,missing=self._adverts(ids[offset:offset+50],deadline,strict=False)
                targets.extend(values);errors.extend(missing)
            except WbReadError as exc:
                if exc.code in {'unauthorized','forbidden'}:raise
                errors.append(exc.code)
        return targets,errors

    def refresh_target(self,target):
        values=self._adverts([target.advert_id],self.monotonic()+120)
        actual=next((v for v in values if v.nm_id==target.nm_id),None)
        if actual is None or actual.unsupported_reason:raise WbReadError('target_changed')
        # Full campaign membership is returned separately for exact preflight CAS.
        return actual,tuple(sorted(v.nm_id for v in values))

    def _pair(self,body,key,target,*,camel=False):
        aid,nid=('advertId','nmId') if camel else ('advert_id','nm_id')
        if not isinstance(body,dict) or not isinstance(body.get(key),list) or len(body[key])!=1:raise WbReadError('pair_missing_or_malformed')
        row=body[key][0]
        if not isinstance(row,dict) or type(row.get(aid)) is not int or type(row.get(nid)) is not int or row[aid]!=target.advert_id or row[nid]!=target.nm_id:raise WbReadError('pair_identity')
        return row

    def snapshot(self,target):
        deadline=self.monotonic()+120;times={}
        pair={'advert_id':target.advert_id,'nm_id':target.nm_id}
        listing=self._pair(self._call('POST','/adv/v0/normquery/list',{'items':[{'advertId':target.advert_id,'nmId':target.nm_id}]},deadline=deadline),'items',target,camel=True).get('normQueries');times['list']=self.clock()
        day=datetime.fromisoformat(self.clock().replace('Z','+00:00')).date()
        stats=self._pair(self._call('POST','/adv/v0/normquery/stats',{'from':(day-timedelta(days=1)).isoformat(),'to':day.isoformat(),'items':[pair]},deadline=deadline),'stats',target).get('stats');times['statistics']=self.clock()
        if not isinstance(stats,list) or any(not isinstance(v,dict) or not isinstance(v.get('norm_query'),str) for v in stats):raise WbReadError('statistics_malformed')
        minus=self._pair(self._call('POST','/adv/v0/normquery/get-minus',{'items':[pair]},deadline=deadline),'items',target).get('norm_queries');times['minus']=self.clock()
        return union_snapshot(target,list_entry=listing,stats_queries=[v['norm_query'] for v in stats],minus_queries=minus,observed_at=self.clock(),source_times=times)

    def read_minus(self,target):
        body=self._call('POST','/adv/v0/normquery/get-minus',{'items':[{'advert_id':target.advert_id,'nm_id':target.nm_id}]},deadline=self.monotonic()+20)
        rows=self._pair(body,'items',target).get('norm_queries')
        if not isinstance(rows,list):raise WbReadError('minus_missing_or_malformed')
        for q in rows:query_hash(q)
        if len(rows)!=len(set(rows)):raise WbReadError('minus_duplicate_query')
        return tuple(rows),self.clock()

    def reserve_write(self):
        deadline=self.monotonic()+120
        if self._target_deadline is not None:deadline=min(deadline,self._target_deadline)
        self.limiter.wait(deadline)
        self.require_target_budget()
        self._slot=object()
        return self._slot

    def set_minus_once(self,target,queries,slot):
        if slot is None or slot is not self._slot:raise CleanerError('write_slot_missing','Нет выделенного сетевого слота')
        self._slot=None
        if not queries or len(queries)>1000 or len(queries)!=len(set(queries)):raise CleanerError('invalid_full_set','Неверный полный список')
        for query in queries:query_hash(query)
        return self._call('POST','/adv/v0/normquery/set-minus',{'advert_id':target.advert_id,'nm_id':target.nm_id,'norm_queries':list(queries)},deadline=self.monotonic()+self.timeout,write=True)

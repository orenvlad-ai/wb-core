"""Setup and read-only UI projections for the existing cleaner service.

No worker, source adapter or background work is created by the web application.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import threading
import time

from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.storage_registry import StoreRegistry
from packages.contracts.search_cluster_cleaner import Account, CleanerError, MODEL_CATALOG, Principal, Target, digest

STAGE_E_CONFIG_PATH=Path('/var/lib/wb-core/search-cluster-cleaner-admission/stage-e-config.json')


class CleanerWeb:
    def __init__(self, cleaner: KeywordCleaner | None = None, *, generation: str = "", origin: str = "", approved_targets: list[dict] | None = None, batch_catalog_targets: list[Target] | None = None):
        self.cleaner = cleaner
        self.generation = generation
        self.origin = origin
        self._fixture_approved_targets = approved_targets
        if approved_targets is not None and batch_catalog_targets is None:
            # Existing synthetic fixtures supply exact approved pairs but no
            # network catalogue. Production always reads official WB detail.
            batch_catalog_targets=[Target(row['advert_id'],row['nm_id'],name=str(row.get('campaign_name') or ''),contract_verified=True)
                                   for row in approved_targets if row.get('state')=='verified']
        self._catalog_lock=threading.Lock()
        self._catalog_names={}
        self._catalog_error=None
        self._catalog_loading=False
        self._catalog_at=0.0
        self._batch_catalog_targets=batch_catalog_targets
        self._batch_catalog_unknown=False
        self._batch_catalog_error=None
        self._batch_catalog_loading=False
        self._batch_catalog_at=time.monotonic() if batch_catalog_targets is not None else 0.0
        self.worker_alive=None
        self.worker_status=None

    def _refresh_campaign_names(self, ids: list[int]) -> None:
        try:
            from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
            source=CleanerWbSource.from_env(self.require_service().account)
            found={}
            for offset in range(0,len(ids),50):
                for target in source._adverts(ids[offset:offset+50],source.monotonic()+30):
                    if target.name:found[target.key]=dict(name=target.name,unsupported_reason=target.unsupported_reason)
            if not found:raise ValueError('No exact campaign names returned')
            with self._catalog_lock:
                self._catalog_names=found;self._catalog_error=None;self._catalog_at=time.monotonic()
        except Exception:
            with self._catalog_lock:
                self._catalog_error='campaign_catalog_unavailable';self._catalog_at=time.monotonic()
        finally:
            with self._catalog_lock:self._catalog_loading=False

    def _campaign_catalog(self,ids:list[int],refresh:bool=False) -> tuple[dict,str|None,bool]:
        if self._fixture_approved_targets is not None:return {},None,False
        with self._catalog_lock:
            if not self._catalog_loading and (refresh or (not self._catalog_names and self._catalog_error is None)):
                self._catalog_loading=True
                threading.Thread(target=self._refresh_campaign_names,args=(ids,),daemon=True,name='cleaner-campaign-catalog').start()
            return dict(self._catalog_names),self._catalog_error,self._catalog_loading

    @classmethod
    def from_env(cls, runtime_dir: Path) -> "CleanerWeb":
        # No implicit identity, generation or admission from an old backup.
        values={key:os.environ.get(key,"").strip() for key in ('SELLER_PORTAL_CANONICAL_SUPPLIER_ID','CLEANER_ACCOUNT_SCOPE','CLEANER_OPERATIONAL_GENERATION','CLEANER_OWNER_USERNAME')}
        if not all(values.values()):
            try:
                stored=json.loads(STAGE_E_CONFIG_PATH.read_text(encoding='utf-8'))
                if set(stored)=={'seller_id','account_scope','generation','owner_username','approved_package_path'}:
                    values=dict(SELLER_PORTAL_CANONICAL_SUPPLIER_ID=stored['seller_id'],CLEANER_ACCOUNT_SCOPE=stored['account_scope'],CLEANER_OPERATIONAL_GENERATION=stored['generation'],CLEANER_OWNER_USERNAME=stored['owner_username'])
            except (OSError,ValueError,TypeError):pass
        seller,scope,generation=(values[k] for k in ('SELLER_PORTAL_CANONICAL_SUPPLIER_ID','CLEANER_ACCOUNT_SCOPE','CLEANER_OPERATIONAL_GENERATION'))
        if not all((seller, scope, generation)):
            return cls()
        cleaner = KeywordCleaner(CleanerStore(StoreRegistry(runtime_dir)), Account(seller, scope),
                                 owner_username=values['CLEANER_OWNER_USERNAME'])
        # Schema and baseline are installed by the governed Stage E bootstrap.
        # A web process must never turn a GET or a restart into a migration.
        return cls(cleaner, generation=generation, origin=os.environ.get("CLEANER_WEB_ORIGIN", "").strip())

    def require_service(self) -> KeywordCleaner:
        if self.cleaner is None:
            raise CleanerError("not_initialized", "Чистка ещё не настроена на сервере", 503)
        return self.cleaner

    def configuration(self, principal: Principal) -> dict:
        principal.require_read()
        cleaner = self.cleaner
        owner = bool(cleaner and cleaner.owner_username.strip())
        can_edit = owner and (principal.site_owner or principal.username.strip().casefold() == cleaner.owner_username.strip().casefold())
        generation_matches = False
        if cleaner:
            with cleaner.store.read() as c:
                settings = cleaner._settings(c)
                generation_matches = bool(self.generation and self.generation == settings["generation"])
        return dict(configured=bool(cleaner), owner_configured=owner,
                    generation_matches=generation_matches, can_edit=bool(can_edit and generation_matches),
                    command_scope=digest([cleaner.key if cleaner else "unconfigured", principal.username.strip().casefold()]))

    def require_mutation(self, principal: Principal) -> KeywordCleaner:
        cleaner = self.require_service()
        principal.require_owner(cleaner.owner_username)
        if not self.configuration(principal)["generation_matches"]:
            raise CleanerError("generation_mismatch", "Работа приостановлена до проверки восстановления", 409)
        return cleaner

    def summary(self, principal: Principal) -> dict:
        config = self.configuration(principal)
        if not self.cleaner:
            return dict(configuration=config, settings=dict(enabled=False, revision=None,
                        schedule_time="07:00", timezone="Asia/Yekaterinburg", baseline_ready=False, restore_hold=True),
                        last_scan=None, current_work=None, queued=[], pending_count=None, unresolved_count=None,
                        profile_required_count=None, target_holds=None, errors=["not_initialized"], indicator=True,
                        transport_enabled=False, profiles=[], models=sorted(MODEL_CATALOG))
        result = self.cleaner.summary(principal)
        with self.cleaner.store.read() as c:
            result["inflight_count"] = c.execute("SELECT count(*) FROM cleaner_write_operations WHERE account=? AND state IN ('dispatching','submitted')", (self.cleaner.key,)).fetchone()[0]
        if not config["owner_configured"] or not config["generation_matches"] or not result["settings"]["baseline_ready"]:
            result["settings"]["enabled"] = False
        result.update(configuration=config, profiles=self.profiles(principal), models=sorted(MODEL_CATALOG))
        result['manual_worker_alive']=bool(self.worker_alive and self.worker_alive())
        result['manual_worker_state']=self.worker_status() if self.worker_status else 'not_attached'
        result['last_manual_job']=None
        result['last_manual_batch']=None
        if config['can_edit']:
            with self.cleaner.store.read() as c:
                recent=c.execute("SELECT request_id FROM cleaner_requests WHERE account=? AND actor=? AND route='manual-clean' ORDER BY created_at DESC,rowid DESC LIMIT 1",(self.cleaner.key,principal.username.strip().casefold())).fetchone()
                recent_batch=c.execute("SELECT request_id FROM cleaner_requests WHERE account=? AND actor=? AND route='manual-batches' ORDER BY created_at DESC,rowid DESC LIMIT 1",(self.cleaner.key,principal.username.strip().casefold())).fetchone()
            if recent:
                result['last_manual_job']=self.cleaner.manual_job(recent['request_id'],principal)
            if recent_batch:
                batch=self.cleaner.manual_batch_snapshot(recent_batch['request_id'],principal)
                result['last_manual_batch']={key:batch[key] for key in ('batch_id','state','stage','created_at','updated_at')}
        result["indicator"] = bool(result["indicator"] or not config["owner_configured"] or not config["generation_matches"] or not result["settings"]["baseline_ready"])
        return result

    def profiles(self, principal: Principal) -> list[dict]:
        principal.require_read()
        cleaner = self.require_service()
        with cleaner.store.read() as c:
            rows = c.execute("""SELECT nm_id FROM cleaner_profile_heads WHERE account=?
                UNION SELECT nm_id FROM cleaner_observations WHERE account=? ORDER BY nm_id""", (cleaner.key, cleaner.key)).fetchall()
            result = []
            for row in rows:
                profile = cleaner._profile(c, row["nm_id"])
                result.append(dict(nm_id=row["nm_id"], title=profile_title(profile, row["nm_id"]),
                                   ready=bool(profile), active_version=profile.version if profile else None,
                                   source=profile.source if profile else None, verified_at=profile.verified_at if profile else None))
            return result

    def targets(self, principal: Principal, *, refresh: bool = False) -> dict:
        """Reuse the exact CPM catalog proof for individual and batch UI."""
        projection=self.batch_eligibility(principal,refresh=refresh)
        items=[dict(advert_id=row['advert_id'],nm_id=row['nm_id'],campaign_name=row['campaign_name'],
                    product_title=row['product_title'],profile_ready=row['profile_ready'],
                    held_reason=row['reason'],eligible=row['eligible'],status=row['status'])
               for row in projection['items']]
        return dict(items=items,error=projection['error'],loading=projection['loading'])

    def start_manual_clean(self, payload: dict, principal: Principal) -> dict:
        cleaner=self.require_mutation(principal)
        # A lost POST response is retried with the same request ID while the
        # worker may already be busy. Let the service's durable idempotency
        # record return the exact earlier outcome before any fresh gating.
        request_id=payload.get('request_id') if isinstance(payload,dict) else None
        if isinstance(request_id,str):
            with cleaner.store.read() as c:
                saved=c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND actor=? AND request_id=? AND route='manual-clean'",
                                (cleaner.key,principal.username.strip().casefold(),request_id)).fetchone()
            if saved:return cleaner.start_manual_clean(payload,principal)
        if self.worker_status and self.worker_status()!='ready':
            raise CleanerError('manual_worker_unavailable','Исполнитель ручной чистки сейчас недоступен',503)
        listed=self.targets(principal)
        if listed['error'] or listed['loading']:
            raise CleanerError('manual_admission_unavailable','Не удалось проверить допуск ручной чистки',409)
        selected=next((row for row in listed['items'] if row['advert_id']==payload.get('advert_id') and row['nm_id']==payload.get('nm_id')),None)
        if not selected or not selected['campaign_name'] or not selected['profile_ready'] or selected['held_reason']:
            raise CleanerError('manual_target_not_admitted','Эта пара кампании и товара недоступна для ручной чистки',409)
        return cleaner.start_manual_clean(payload,principal)

    def _refresh_batch_catalog(self) -> None:
        try:
            from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
            source=CleanerWbSource.from_env(self.require_service().account)
            targets,errors,statuses=source.catalog(with_statuses=True)
            if any(not (error.startswith('adverts_missing:') and error.removeprefix('adverts_missing:').isdigit()
                        and statuses.get(int(error.removeprefix('adverts_missing:'))) in {7,-1}) for error in errors):
                raise CleanerError('campaign_catalog_incomplete','Каталог WB вернул неполные данные',409)
            from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
            eligibility_rows(self.require_service(),self.generation,targets,fixture_admission=self._fixture_approved_targets)
            with self._catalog_lock:
                self._batch_catalog_targets=targets
                self._batch_catalog_unknown=bool(errors)
                self._batch_catalog_error=None
                self._batch_catalog_at=time.monotonic()
        except Exception:
            with self._catalog_lock:
                self._batch_catalog_targets=None
                self._batch_catalog_unknown=True
                self._batch_catalog_error='campaign_catalog_unavailable'
                self._batch_catalog_at=time.monotonic()
        finally:
            with self._catalog_lock:self._batch_catalog_loading=False

    def batch_eligibility(self, principal: Principal, *, refresh: bool = False) -> dict:
        principal.require_read()
        self.require_service()
        from packages.application.search_cluster_cleaner_batch_eligibility import category_contract, eligibility_rows
        with self._catalog_lock:
            stale=time.monotonic()-self._batch_catalog_at>120
            if not self._batch_catalog_loading and (refresh or stale) and self._fixture_approved_targets is None:
                self._batch_catalog_loading=True
                self._batch_catalog_targets=None
                threading.Thread(target=self._refresh_batch_catalog,daemon=True,name='cleaner-batch-catalog').start()
            targets=self._batch_catalog_targets
            unknown=self._batch_catalog_unknown
            loading=self._batch_catalog_loading
            error=self._batch_catalog_error
        empty_counts=dict(total=None,eligible=None,selectable_active=None,selectable_paused=None,
                          profile_required=None,ineligible=None,unknown=None)
        if loading or targets is None:
            return dict(items=[],counts=empty_counts,loading=loading,error=None if loading else error or 'campaign_catalog_unavailable',categories=category_contract())
        try:
            rows=eligibility_rows(self.require_service(),self.generation,targets,fixture_admission=self._fixture_approved_targets)
        except CleanerError:
            return dict(items=[],counts=empty_counts,loading=False,error='manual_admission_unavailable',categories=category_contract())
        rows.sort(key=lambda row:({'active':0,'paused':1,'completed':2,'archive':3}.get(row['status'],4),
                                  row['campaign_name'].casefold(),row['advert_id'],row['nm_id']))
        counts=dict(total=len(rows),eligible=sum(row['eligible'] for row in rows),
                    selectable_active=sum(row['eligible'] and row['status']=='active' for row in rows),
                    selectable_paused=sum(row['eligible'] and row['status']=='paused' for row in rows),
                    profile_required=sum(row['reason']=='profile_required' for row in rows),
                    ineligible=sum(not row['eligible'] and row['reason']!='profile_required' for row in rows),
                    unknown=None if unknown else 0)
        return dict(items=rows,counts=counts,loading=False,error=None,categories=category_contract())

    def start_manual_batch(self,payload:dict,principal:Principal) -> dict:
        cleaner=self.require_mutation(principal)
        request_id=payload.get('request_id') if isinstance(payload,dict) else None
        if isinstance(request_id,str):
            with cleaner.store.read() as c:
                saved=c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND actor=? AND request_id=? AND route='manual-batches'",(cleaner.key,principal.username.strip().casefold(),request_id)).fetchone()
            if saved:return cleaner.start_manual_batch(payload,principal)
        if self.worker_status and self.worker_status()!='ready':
            raise CleanerError('manual_worker_unavailable','Исполнитель ручной чистки сейчас недоступен',503)
        from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
        from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
        try:
            targets=payload.get('targets')
            if not isinstance(targets,list) or not targets:
                raise CleanerError('batch_selection_invalid','Выберите точные пары кампании и товара',422)
            identities=[]
            for row in targets:
                if (not isinstance(row,dict) or set(row)!={'advert_id','nm_id'}
                        or type(row['advert_id']) is not int or type(row['nm_id']) is not int
                        or row['advert_id']<=0 or row['nm_id']<=0):
                    raise CleanerError('batch_selection_invalid','Некорректная пара кампании и товара',422)
                identities.append((row['advert_id'],row['nm_id']))
            if len(identities)!=len(set(identities)):
                raise CleanerError('batch_selection_duplicate','Пара выбрана повторно',422)
            ids=sorted({advert_id for advert_id,_ in identities})
            source=CleanerWbSource.from_env(cleaner.account)
            deadline=source.monotonic()+120
            statuses=source.count_statuses(deadline)
            allowed_ids={advert_id for advert_id,status in statuses.items() if status in {9,11}}
            if not set(ids)<=allowed_ids:
                raise CleanerError('batch_target_ineligible','Выбранной кампании нет среди действующих или приостановленных WB',409)
            catalog=[]
            for offset in range(0,len(ids),50):catalog.extend(source._adverts(ids[offset:offset+50],deadline))
            snapshot=eligibility_rows(cleaner,self.generation,catalog,fixture_admission=self._fixture_approved_targets)
        except CleanerError:raise
        except Exception as exc:
            raise CleanerError('campaign_catalog_unavailable','Не удалось проверить текущие кампании WB',409) from exc
        return cleaner.start_manual_batch(payload,principal,snapshot=snapshot)

    def reviews(self, principal: Principal, **params) -> dict:
        cleaner = self.require_service()
        result = cleaner.reviews(principal, **params)
        with cleaner.store.read() as c:
            for item in result["items"]:
                profile = cleaner._profile(c, item["nm_id"])
                item.update(product_title=profile_title(profile, item["nm_id"]), profile_ready=bool(profile))
        return result

    def history(self, principal: Principal, **params) -> dict:
        cleaner = self.require_service()
        result = cleaner.history(principal, **params)
        with cleaner.store.read() as c:
            for item in result["items"]:
                review_id = item["facts"].get("review_id")
                if review_id:
                    row = c.execute("SELECT query,nm_id FROM cleaner_reviews WHERE account=? AND review_id=?", (cleaner.key, review_id)).fetchone()
                    if row:
                        item.update(query=row["query"], nm_id=row["nm_id"])
        return result


def profile_title(profile, nm_id: int) -> str:
    if profile is None:
        return f"Товар WB {nm_id}"
    models = ", ".join("iPhone " + model.replace("promax", "Pro Max").replace("pro", "Pro").replace("air", "Air") for model in profile.models)
    kind = {"clean": "Прозрачное стекло", "matte": "Матовое стекло", "anti": "Стекло антишпион"}[profile.kind]
    return f"{kind} · {models} · WB {nm_id}"

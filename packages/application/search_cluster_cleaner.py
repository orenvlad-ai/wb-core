"""Durable keyword-cleaner application; this module cannot send WB mutations.

HTTP callers only enqueue commands or read saved state. A separately invoked
worker uses the read-only source port. The write boundary is added in stage D.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.contracts.search_cluster_cleaner import (
    Account, CleanerError, COVERAGE_NOTICE, Principal, Profile, RULES_VERSION,
    Snapshot, Target, canonical, digest, query_hash, utcnow,
)
from packages.domain.search_cluster_classifier import classify
from packages.application.search_cluster_cleaner_rules_identity import executable_rules_digest

FINAL_OBSERVATION_STATES = {"allow", "confirmed", "baseline", "observed_excluded", "observed_archived", "external_state_drift", "already_excluded"}
PENDING_WRITE_STATES = "'dispatching','submitted','unresolved','validation_rejected','rate_limited','unauthorized','forbidden','transport_ambiguous','http_error'"
BLOCKING_WRITE_STATES = PENDING_WRITE_STATES+",'requires_review'"
BATCH_TERMINAL_STATES = {'complete','partial','failed'}


def batch_child_id(batch_id: str, index: int) -> str:
    """A stable, disjoint command identity for one frozen batch position."""
    return 'batch-'+digest([batch_id,index])[:40]


def new_id() -> str:
    return str(uuid.uuid4())


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def plus_seconds(value: str, seconds: int) -> str:
    return (timestamp(value) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


class KeywordCleaner:
    def __init__(self, store: CleanerStore, account: Account, *, owner_username: str = "", clock: Callable[[], str] = utcnow, rules_version: str = RULES_VERSION, classifier: Callable = classify):
        self.store, self.account, self.owner_username, self.clock = store, account, owner_username, clock
        self.key = account.key
        self.rules_version,self.classifier=rules_version,classifier
        self.rules_source_digest=hashlib.sha256(Path(classify.__code__.co_filename).read_bytes()).hexdigest()
        # Loaded bytecode and vocabulary remain guarded, while source/.pyc
        # import order and absolute checkout paths cannot change the digest.
        self.rules_digest=executable_rules_digest(self.classifier)

    def initialize(self, *, generation: str) -> None:
        if not generation: raise CleanerError("generation_required", "Не указано поколение operational")
        self.store.initialize()
        with self.store.transaction() as c:
            c.execute("""INSERT OR IGNORE INTO cleaner_settings(account,seller_id,account_scope,rules_version,generation,created_at)
              VALUES(?,?,?,?,?,?)""", (self.key,self.account.seller_id,self.account.account_scope,self.rules_version,generation,self.clock()))

    def _settings(self, c) -> sqlite3.Row:
        row = c.execute("SELECT * FROM cleaner_settings WHERE account=?", (self.key,)).fetchone()
        if row is None: raise CleanerError("not_initialized", "Чистка ещё не настроена", 503)
        return row

    def _event(self, c, kind: str, facts: Any, *, run_id=None, operation_id=None) -> None:
        c.execute("INSERT INTO cleaner_events(event_id,account,run_id,operation_id,kind,created_at,facts) VALUES(?,?,?,?,?,?,?)", (new_id(),self.key,run_id,operation_id,kind,self.clock(),canonical(facts)))

    def record_manual_stage_failure(self, *, run_id: str, operation_id: str, code: str, error_type: str) -> None:
        """Persist only a bounded diagnostic, never exception text or WB data."""
        safe=lambda value: re.sub(r'[^A-Za-z0-9_.:-]', '_', str(value))[:80]
        with self.store.transaction() as c:
            run=c.execute("SELECT kind,phase FROM cleaner_runs WHERE account=? AND run_id=?",
                          (self.key,run_id)).fetchone()
            if not run or run['kind']!='manual_apply':return
            facts=dict(failed_stage='manual_write_'+safe(run['phase']),code=safe(code),error_type=safe(error_type))
            previous=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND operation_id=? AND kind='stage_e_manual_failure' ORDER BY sequence DESC LIMIT 1",
                               (self.key,run_id,operation_id)).fetchone()
            if not previous or json.loads(previous[0])!=facts:
                self._event(c,'stage_e_manual_failure',facts,run_id=run_id,operation_id=operation_id)

    def _active_manual_batch(self,c) -> str|None:
        row=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested' ORDER BY sequence DESC LIMIT 1",(self.key,)).fetchone()
        if not row:return None
        batch_id=json.loads(row['facts'])['batch_id']
        latest=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence DESC LIMIT 1",(self.key,batch_id)).fetchone()
        state=json.loads(latest['facts']).get('state','queued') if latest else 'queued'
        return batch_id if state not in BATCH_TERMINAL_STATES else None

    def _command(self, principal: Principal, route: str, payload: Mapping, operation: Callable) -> dict:
        principal.require_owner(self.owner_username)
        request_id = payload.get("request_id")
        if not isinstance(request_id,str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,119}",request_id) is None:
            raise CleanerError("request_id_required", "Нужен идентификатор команды")
        actor = principal.username.strip().casefold()
        fingerprint = digest([route,dict(payload)])
        from packages.application.search_cluster_cleaner_store import CleanerTransactionRolledBack
        try:
            with self.store.transaction() as c:
                previous = c.execute("SELECT * FROM cleaner_requests WHERE account=? AND actor=? AND request_id=?",(self.key,actor,request_id)).fetchone()
                if previous:
                    if previous["digest"] != fingerprint: raise CleanerError("request_conflict", "Идентификатор уже использован другой командой",409)
                    return json.loads(previous["outcome"])
                outcome = operation(c,actor)
                c.execute("INSERT INTO cleaner_requests VALUES(?,?,?,?,?,?,?)",(self.key,actor,request_id,route,fingerprint,canonical(outcome),self.clock()))
                return outcome
        except CleanerTransactionRolledBack as exc:
            if route == 'manual-batches':
                raise CleanerError('storage_rolled_back','Команда не сохранилась из-за занятости хранилища',503) from exc
            raise

    def get_request(self, request_id: str, principal: Principal) -> dict:
        principal.require_read()
        with self.store.read() as c:
            row = c.execute("SELECT outcome FROM cleaner_requests WHERE account=? AND actor=? AND request_id=?",(self.key,principal.username.strip().casefold(),request_id)).fetchone()
            if not row: raise CleanerError("not_found", "Команда не найдена",404)
            return json.loads(row[0])

    def update_settings(self, payload: Mapping, principal: Principal) -> dict:
        def command(c,actor):
            s = self._settings(c)
            if payload.get("expected_revision") != s["revision"]: raise CleanerError("revision_conflict", "Настройки уже изменились",409)
            manual_only=bool(c.execute("SELECT 1 FROM cleaner_events WHERE account=? AND kind='stage_e_bootstrap' LIMIT 1",(self.key,)).fetchone())
            if manual_only and (payload.get("enabled") is True or "schedule_time" in payload):
                raise CleanerError("manual_only","По расписанию чистка не включается",409)
            enabled = payload.get("enabled",bool(s["enabled"]))
            time = payload.get("schedule_time",s["schedule_time"])
            if type(enabled) is not bool or not isinstance(time,str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",time) is None:
                raise CleanerError("invalid_settings", "Неверное время или состояние")
            if set(payload)-{"request_id","expected_revision","enabled","schedule_time"}:
                raise CleanerError("invalid_settings", "Неизвестное поле настроек")
            c.execute("UPDATE cleaner_settings SET enabled=?,schedule_time=?,revision=revision+1 WHERE account=?",(int(enabled),time,self.key))
            self._cancel_prepared(c,"settings_changed")
            self._event(c,"settings",dict(enabled=enabled,schedule_time=time,actor=actor))
            return dict(settings_revision=s["revision"]+1,enabled=enabled,schedule_time=time,admitted_operations=self._admitted(c))
        return self._command(principal,"settings",payload,command)

    def activate_rules(self,*,expected_revision:int,provenance:str) -> int:
        """Release-owned explicit activation; not a web settings field."""
        if not provenance: raise CleanerError("rules_provenance_required","Нет происхождения версии правил")
        with self.store.transaction() as c:
            s=self._settings(c)
            if s["revision"]!=expected_revision: raise CleanerError("revision_conflict","Настройки уже изменились",409)
            c.execute("UPDATE cleaner_settings SET rules_version=?,revision=revision+1 WHERE account=?",(self.rules_version,self.key))
            self._cancel_prepared(c,"rules_changed")
            self._event(c,"rules_activated",dict(rules_version=self.rules_version,provenance=provenance))
            return s["revision"]+1

    def _admitted(self,c) -> int:
        return c.execute(f"SELECT count(*) FROM cleaner_write_operations WHERE account=? AND state IN({PENDING_WRITE_STATES})",(self.key,)).fetchone()[0]

    def _cancel_prepared(self,c,reason,nm_id=None):
        rows = c.execute("SELECT operation_id,target FROM cleaner_write_operations WHERE account=? AND state='prepared'",(self.key,)).fetchall()
        for row in rows:
            if nm_id is not None and int(row["target"].split(":")[1]) != nm_id: continue
            c.execute("UPDATE cleaner_write_operations SET state='cancelled_before_send',updated_at=? WHERE operation_id=?",(self.clock(),row["operation_id"]))
            self._event(c,"candidate_cancelled",dict(reason=reason),operation_id=row["operation_id"])

    def get_profile(self,nm_id:int,principal:Principal) -> dict:
        principal.require_read()
        with self.store.read() as c:
            h = c.execute("SELECT * FROM cleaner_profile_heads WHERE account=? AND nm_id=?",(self.key,nm_id)).fetchone()
            versions = [json.loads(r[0]) for r in c.execute("SELECT payload FROM cleaner_profiles WHERE account=? AND nm_id=? ORDER BY version DESC",(self.key,nm_id))]
            return dict(nm_id=nm_id,revision=h["revision"] if h else 0,active_version=h["active_version"] if h else None,versions=versions)

    def create_profile(self,nm_id:int,payload:Mapping,principal:Principal) -> dict:
        def command(c,actor):
            h = c.execute("SELECT * FROM cleaner_profile_heads WHERE account=? AND nm_id=?",(self.key,nm_id)).fetchone()
            revision = h["revision"] if h else 0
            if payload.get("expected_revision") != revision: raise CleanerError("revision_conflict","Профиль уже изменился",409)
            version = c.execute("SELECT coalesce(max(version),0)+1 FROM cleaner_profiles WHERE account=? AND nm_id=?",(self.key,nm_id)).fetchone()[0]
            p = Profile.parse(dict(payload.get("profile",{}),nm_id=nm_id,version=version))
            c.execute("INSERT INTO cleaner_profiles VALUES(?,?,?,?,?,?,?)",(self.key,nm_id,version,p.semantic_fingerprint,canonical(p.as_dict()),self.clock(),actor))
            c.execute("INSERT INTO cleaner_profile_heads VALUES(?,?,NULL,?) ON CONFLICT(account,nm_id) DO UPDATE SET revision=excluded.revision",(self.key,nm_id,revision+1))
            self._event(c,"profile_draft",dict(nm_id=nm_id,version=version,actor=actor))
            return dict(profile_version=version,profile_revision=revision+1,active=False)
        return self._command(principal,f"profiles/{nm_id}/versions",payload,command)

    def activate_profile(self,nm_id:int,payload:Mapping,principal:Principal) -> dict:
        def command(c,actor):
            h = c.execute("SELECT * FROM cleaner_profile_heads WHERE account=? AND nm_id=?",(self.key,nm_id)).fetchone()
            if h is None or payload.get("expected_revision") != h["revision"]: raise CleanerError("revision_conflict","Профиль уже изменился",409)
            row = c.execute("SELECT * FROM cleaner_profiles WHERE account=? AND nm_id=? AND version=?",(self.key,nm_id,payload.get("version"))).fetchone()
            if row is None: raise CleanerError("profile_required","Версия профиля не найдена")
            p = Profile.parse(json.loads(row["payload"]))
            c.execute("UPDATE cleaner_profile_heads SET active_version=?,revision=revision+1 WHERE account=? AND nm_id=?",(p.version,self.key,nm_id))
            overrides = c.execute("""SELECT o.* FROM cleaner_manual_overrides o JOIN cleaner_override_heads h
              USING(account,nm_id,query_hash,revision) WHERE o.account=? AND o.nm_id=?""",(self.key,nm_id)).fetchall()
            for o in overrides:
                changed = o["fingerprint"] != p.semantic_fingerprint
                c.execute("UPDATE cleaner_override_heads SET needs_revalidation=? WHERE account=? AND nm_id=? AND query_hash=?",(int(changed),self.key,nm_id,o["query_hash"]))
                if changed:
                    observations = c.execute("SELECT * FROM cleaner_observations WHERE account=? AND nm_id=? AND query_hash=? AND state IN('review','profile_required','pending_exclude')",(self.key,nm_id,o["query_hash"])).fetchall()
                    for obs in observations: self._review(c,dict(obs),"Изменилась совместимость товара: проверьте решение")
            self._sync_reviews(c)
            self._cancel_prepared(c,"profile_changed",nm_id)
            self._event(c,"profile_activated",dict(nm_id=nm_id,version=p.version,actor=actor))
            return dict(profile_version=p.version,profile_revision=h["revision"]+1,active=True,admitted_operations=self._admitted(c))
        return self._command(principal,f"profiles/{nm_id}/activate",payload,command)

    def _profile(self,c,nm_id) -> Profile | None:
        row=c.execute("SELECT p.payload FROM cleaner_profiles p JOIN cleaner_profile_heads h ON p.account=h.account AND p.nm_id=h.nm_id AND p.version=h.active_version WHERE p.account=? AND p.nm_id=?",(self.key,nm_id)).fetchone()
        return Profile.parse(json.loads(row[0])) if row else None

    def import_baseline(self,rows:list[Mapping],profiles:list[Mapping],*,provenance:Mapping,expected_digest:str,ready:bool=False) -> dict:
        """Server-side, explicit import only. Never derives semantic labels from WB status.

        Caller supplies the independently approved reference and provenance. Real
        imports keep ready=False until the separate source reconciliation closes.
        """
        if digest(rows) != expected_digest or not provenance: raise CleanerError("baseline_digest","Не совпало происхождение исходной базы")
        parsed_profiles=[Profile.parse(p) for p in profiles]
        if len({p.nm_id for p in parsed_profiles})!=len(parsed_profiles): raise CleanerError("duplicate_profile","Повтор профиля")
        seen=set()
        for row in rows:
            identity=(Target(row["advert_id"],row["nm_id"]).key,query_hash(row["query"]))
            if identity in seen: raise CleanerError("duplicate_baseline","Дубликат исходной строки")
            seen.add(identity)
            if row.get("decision") not in {"allow","exclude"} or not row.get("provenance"):
                raise CleanerError("baseline_reference_missing","Нет согласованного решения или его происхождения")
            if row.get("observed_state") not in {"active","excluded","archived","unknown"}: raise CleanerError("baseline_state","Нет отдельного исходного состояния")
        now=self.clock()
        with self.store.transaction() as c:
            existing = c.execute("SELECT DISTINCT import_digest FROM cleaner_baselines WHERE account=?",(self.key,)).fetchall()
            if existing:
                if len(existing)==1 and existing[0][0]==expected_digest and c.execute("SELECT count(*) FROM cleaner_baselines WHERE account=?",(self.key,)).fetchone()[0]==len(rows):
                    return dict(imported=len(rows),replayed=True,digest=expected_digest)
                raise CleanerError("baseline_exists","Исходная база уже импортирована",409)
            for p in parsed_profiles:
                c.execute("INSERT INTO cleaner_profiles VALUES(?,?,?,?,?,?,?)",(self.key,p.nm_id,p.version,p.semantic_fingerprint,canonical(p.as_dict()),now,"baseline_import"))
                c.execute("INSERT INTO cleaner_profile_heads VALUES(?,?,?,1)",(self.key,p.nm_id,p.version))
            for row in rows:
                target=Target(row["advert_id"],row["nm_id"]).key;q=row["query"];qh=query_hash(q)
                p=self._profile(c,row["nm_id"])
                if p is None: raise CleanerError("profile_required","В исходной базе нет профиля товара")
                decision=self._decision(c,target,q,dict(verdict=row["decision"],rule="APPROVED_BASELINE",reason="Согласованная исходная база",facts=row["provenance"]),p,"baseline")
                c.execute("INSERT INTO cleaner_baselines VALUES(?,?,?,?,?,?,?,?,?,?,?)",(self.key,target,row["advert_id"],row["nm_id"],qh,q,row["observed_state"],decision,canonical(row["provenance"]),expected_digest,now))
                c.execute("INSERT INTO cleaner_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(self.key,target,row["advert_id"],row["nm_id"],qh,q,now,now,row["observed_state"],canonical(["baseline"]),canonical({}),"baseline",decision,None,None))
            c.execute("UPDATE cleaner_settings SET baseline_ready=? WHERE account=?",(int(ready),self.key))
            self._event(c,"baseline_import",dict(count=len(rows),digest=expected_digest,provenance=provenance,ready=ready))
            return dict(imported=len(rows),replayed=False,digest=expected_digest)

    def _decision(self,c,target,q,result,p,source="rules",override_revision=None) -> str:
        decision_id=new_id()
        c.execute("INSERT INTO cleaner_auto_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(decision_id,self.key,target,query_hash(q),q,result["verdict"],result["rule"],result["reason"],canonical(dict(result.get("facts",result),rules_hash=self.rules_digest,classifier_source_sha256=self.rules_source_digest)),self.rules_version,p.version if p else None,p.semantic_fingerprint if p else None,source,override_revision,self.clock()))
        return decision_id

    def _review(self,c,obs:Mapping,reason:str) -> str:
        now=self.clock()
        row=c.execute("SELECT * FROM cleaner_reviews WHERE account=? AND nm_id=? AND query_hash=?",(self.key,obs["nm_id"],obs["query_hash"])).fetchone()
        if row:
            rid=row["review_id"]
            if row["state"]!="open":
                c.execute("UPDATE cleaner_reviews SET state='open',revision=revision+1,reason=?,updated_at=? WHERE review_id=?",(reason,now,rid))
            elif row["reason"]!=reason:
                c.execute("UPDATE cleaner_reviews SET reason=?,updated_at=?,revision=revision+1 WHERE review_id=?",(reason,now,rid))
        else:
            rid=new_id();c.execute("INSERT INTO cleaner_reviews(review_id,account,nm_id,query_hash,query,revision,reason,state,created_at,updated_at) VALUES(?,?,?,?,?,1,?,'open',?,?)",(rid,self.key,obs["nm_id"],obs["query_hash"],obs["query"],reason,now,now))
        c.execute("UPDATE cleaner_observations SET review_id=?,state='review' WHERE account=? AND target=? AND query_hash=?",(rid,self.key,obs["target"],obs["query_hash"]))
        return rid

    def _sync_reviews(self,c) -> None:
        # Revision covers the exact campaigns/states shown with a question.
        # It invalidates a stale browser decision when a new occurrence arrives.
        for row in c.execute("SELECT * FROM cleaner_reviews WHERE account=? AND state='open'",(self.key,)).fetchall():
            members=[tuple(r) for r in c.execute("SELECT target,observed_state FROM cleaner_observations WHERE account=? AND review_id=? AND state IN('review','profile_required','pending_exclude') ORDER BY target",(self.key,row["review_id"]))]
            fp=digest(members)
            if not members:
                c.execute("UPDATE cleaner_reviews SET state='resolved',revision=revision+1,scope_digest=?,updated_at=? WHERE review_id=?",(fp,self.clock(),row["review_id"]))
            elif row["scope_digest"]!=fp:
                c.execute("UPDATE cleaner_reviews SET revision=revision+?,scope_digest=?,updated_at=? WHERE review_id=?",(int(bool(row["scope_digest"])),fp,self.clock(),row["review_id"]))

    def _classify_observation(self,c,obs:Mapping,run_id:str) -> str:
        p=self._profile(c,obs["nm_id"])
        if p is None:
            c.execute("UPDATE cleaner_observations SET state='profile_required' WHERE account=? AND target=? AND query_hash=?",(self.key,obs["target"],obs["query_hash"]))
            return "profile_required"
        override=c.execute("""SELECT o.*,h.needs_revalidation FROM cleaner_manual_overrides o JOIN cleaner_override_heads h
           USING(account,nm_id,query_hash,revision) WHERE o.account=? AND o.nm_id=? AND o.query_hash=?""",(self.key,obs["nm_id"],obs["query_hash"])).fetchone()
        source="rules";revision=None
        if override:
            if override["fingerprint"]!=p.semantic_fingerprint or override["needs_revalidation"]:
                result=dict(verdict="review",rule="OVERRIDE_REVALIDATION",reason="Проверьте решение после изменения товара")
            else:
                source="owner_decision";revision=override["revision"]
                result=dict(verdict=override["verdict"],rule="OWNER_EXACT",reason="Точное решение владельца для этого товара")
        else:
            if self._settings(c)["rules_version"]!=self.rules_version: raise CleanerError("rules_version_mismatch","Worker использует другую версию правил",409)
            result=self.classifier(obs["query"],p)
        decision_id=self._decision(c,obs["target"],obs["query"],result,p,source,revision)
        state={"allow":"allow","exclude":"pending_exclude","review":"review"}[result["verdict"]]
        c.execute("UPDATE cleaner_observations SET decision_id=?,state=?,last_run_id=? WHERE account=? AND target=? AND query_hash=?",(decision_id,state,run_id,self.key,obs["target"],obs["query_hash"]))
        if state=="review": self._review(c,obs,result["reason"])
        return result["verdict"]

    def _new_run(self,c,kind,trigger,*,request_id=None,targets=(),review_id=None,override_revision=None,apply_group_id=None,continuation_number=0) -> str:
        s=self._settings(c);rid=new_id();now=self.clock()
        c.execute("""INSERT INTO cleaner_runs(run_id,account,kind,trigger,state,phase,created_at,request_id,captured_versions,targets,review_id,override_revision,apply_group_id,continuation_number)
          VALUES(?,?,?,?,'queued','queued',?,?,?,?,?,?,?,?)""",(rid,self.key,kind,trigger,now,request_id,canonical(dict(settings=s["revision"],rules=s["rules_version"],rules_hash=self.rules_digest,classifier_source_sha256=self.rules_source_digest,generation=s["generation"])),canonical(list(targets)),review_id,override_revision,apply_group_id,continuation_number))
        self._event(c,"run_queued",dict(kind=kind,trigger=trigger),run_id=rid)
        return rid

    def start_run(self,payload:Mapping,principal:Principal) -> dict:
        def command(c,actor):
            if self._active_manual_batch(c):
                raise CleanerError('manual_batch_active','Сначала завершите массовую ручную чистку',409)
            s=self._settings(c)
            manual_target = payload.get("advert_id"), payload.get("nm_id")
            manual = manual_target != (None, None)
            if manual:
                if (type(manual_target[0]) is not int or manual_target[0] <= 0
                        or type(manual_target[1]) is not int or manual_target[1] <= 0):
                    raise CleanerError("target_invalid", "Нужна точная кампания и товар", 422)
                if s["enabled"] or not s["baseline_ready"]:
                    raise CleanerError("not_ready", "Ручной режим ещё не подготовлен", 409)
            elif not s["enabled"] or not s["baseline_ready"]:
                raise CleanerError("not_ready","Чистка выключена или исходная база не сверена",409)
            active=c.execute("SELECT run_id,kind,trigger,state,targets FROM cleaner_runs WHERE account=? AND state IN('queued','accepted','running') ORDER BY CASE WHEN state='queued' THEN 1 ELSE 0 END,created_at LIMIT 1",(self.key,)).fetchone()
            if active:
                if manual:
                    requested=[dict(target=Target(*manual_target).key,advert_id=manual_target[0],nm_id=manual_target[1])]
                    if active['state']=='queued' and active['kind']=='scan' and active['trigger']=='manual_exact':
                        if json.loads(active['targets'])==requested:
                            return dict(status=202,run_id=active['run_id'],kind='scan',reused=True,manual_target=requested[0])
                        c.execute("UPDATE cleaner_runs SET state='stopped',phase='finished',scan_finished_at=?,reason='manual_superseded' WHERE run_id=? AND state='queued'",(self.clock(),active['run_id']))
                        self._event(c,'run_finished',dict(state='stopped',reason='manual_superseded'),run_id=active['run_id'])
                    else: raise CleanerError("manual_queue_blocked", "Сначала завершите уже подготовленную проверку", 409)
                else:return dict(status=202,run_id=active["run_id"],active_run_id=active["run_id"],kind=active["kind"],reused=True)
            targets=[] if not manual else [dict(target=Target(*manual_target).key,advert_id=manual_target[0],nm_id=manual_target[1])]
            rid=self._new_run(c,"scan","manual_exact" if manual else "manual",request_id=payload["request_id"],targets=targets)
            return dict(status=202,run_id=rid,kind="scan",reused=False,manual_target=targets[0] if targets else None)
        return self._command(principal,"runs",payload,command)

    def start_manual_clean(self,payload:Mapping,principal:Principal,*,batch_id:str|None=None,batch_index:int|None=None) -> dict:
        """Persist one explicit owner intent; no worker or WB call runs here."""
        def command(c,actor):
            advert_id,nm_id=payload.get('advert_id'),payload.get('nm_id')
            if type(advert_id) is not int or advert_id<=0 or type(nm_id) is not int or nm_id<=0:
                raise CleanerError('target_invalid','Выберите точную кампанию и товар',422)
            target=Target(advert_id,nm_id)
            s=self._settings(c)
            if s['enabled'] or not s['baseline_ready']:
                raise CleanerError('manual_not_ready','Ручная чистка сейчас недоступна',409)
            if c.execute("SELECT 1 FROM cleaner_target_holds WHERE account=? AND target=?",(self.key,target.key)).fetchone():
                raise CleanerError('target_held','Чистка этой кампании приостановлена до разбора',409)
            latest_batch=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested' ORDER BY sequence DESC LIMIT 1",(self.key,)).fetchone()
            if latest_batch:
                batch_facts=json.loads(latest_batch['facts'])
                batch_state=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence DESC LIMIT 1",(self.key,batch_facts['batch_id'])).fetchone()
                state=json.loads(batch_state['facts']).get('state','queued') if batch_state else 'queued'
                if batch_id is None and state not in BATCH_TERMINAL_STATES:
                    raise CleanerError('manual_batch_active','Сначала завершите текущую массовую чистку',409)
                if batch_id is not None:
                    frozen=batch_facts.get('items',[])
                    batch_progress=json.loads(batch_state['facts']) if batch_state else batch_facts
                    if (batch_id!=batch_facts['batch_id'] or state in BATCH_TERMINAL_STATES
                            or type(batch_index) is not int or batch_index<0 or batch_index>=len(frozen)
                            or batch_progress.get('current_index',0)!=batch_index
                            or str(batch_index) in batch_progress.get('item_updates',{})
                            or frozen[batch_index]['advert_id']!=advert_id or frozen[batch_index]['nm_id']!=nm_id
                            or payload.get('request_id')!=batch_child_id(batch_id,batch_index)):
                        raise CleanerError('batch_child_invalid','Ручное задание не соответствует сохранённой группе',409)
                    if (batch_facts.get('actor')!=actor or batch_facts.get('account_key')!=self.key
                            or batch_facts.get('generation')!=s['generation']
                            or batch_facts.get('actor_authority')!=('bootstrap_operator' if principal.site_owner else 'configured_owner')):
                        raise CleanerError('batch_child_authority_mismatch','Владелец группы изменился',409)
            elif batch_id is not None:
                raise CleanerError('batch_child_invalid','Сохранённая группа не найдена',409)
            current=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_requested' ORDER BY sequence DESC LIMIT 1",(self.key,)).fetchone()
            if current:
                facts=json.loads(current['facts'])
                latest=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%' AND json_extract(facts,'$.job_id')=? ORDER BY sequence DESC LIMIT 1",(self.key,facts['job_id'])).fetchone()
                latest_facts=json.loads(latest['facts']) if latest else facts
                if latest_facts.get('state') not in {'complete','partial','failed','no_change'} or latest_facts.get('can_recheck'):
                    raise CleanerError('manual_job_active','Предыдущая ручная чистка ещё выполняется или требует сверки',409)
            active=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND state IN('queued','accepted','running') ORDER BY created_at LIMIT 1",(self.key,)).fetchone()
            declared=[dict(target=target.key,advert_id=advert_id,nm_id=nm_id)]
            if active:
                if active['state']!='queued' or active['kind']!='scan' or active['trigger']!='manual_exact' or json.loads(active['targets'])!=declared:
                    raise CleanerError('manual_queue_blocked','Есть другое незавершённое задание',409)
                run_id=active['run_id'];adopted=True
            else:
                run_id=self._new_run(c,'scan','manual_exact',request_id=payload['request_id'],targets=declared)
                adopted=False
            job_id=payload['request_id']
            self._event(c,'self_service_requested',dict(job_id=job_id,scan_run_id=run_id,advert_id=advert_id,nm_id=nm_id,actor=actor,adopted=adopted,
                                                        actor_authority='bootstrap_operator' if principal.site_owner else 'configured_owner',
                                                        account_key=self.key,generation=s['generation'],batch_id=batch_id,batch_index=batch_index),run_id=run_id)
            return dict(job_id=job_id,run_id=run_id,state='queued',stage='fetching',adopted=adopted,
                        advert_id=advert_id,nm_id=nm_id,batch_id=batch_id,batch_index=batch_index)
        return self._command(principal,'manual-clean',payload,command)

    def start_manual_batch(self,payload:Mapping,principal:Principal,*,snapshot:list[dict]|None=None) -> dict:
        """Freeze only caller-selected exact pairs admitted by one fresh catalog."""
        def command(c,actor):
            targets=payload.get('targets');categories=payload.get('selected_categories')
            if (not isinstance(targets,list) or not targets
                    or not isinstance(categories,list) or categories not in (['active'],['active','paused'])):
                raise CleanerError('batch_selection_invalid','Выберите точные пары и разрешённые статусы',422)
            identities=[]
            for row in targets:
                if (not isinstance(row,dict) or set(row)!={'advert_id','nm_id'}
                        or type(row['advert_id']) is not int or type(row['nm_id']) is not int):
                    raise CleanerError('batch_selection_invalid','Некорректная пара кампании и товара',422)
                identities.append(Target(row['advert_id'],row['nm_id']).key)
            if len(identities)!=len(set(identities)):
                raise CleanerError('batch_selection_duplicate','Пара выбрана повторно',422)
            s=self._settings(c)
            if s['enabled'] or not s['baseline_ready']:
                raise CleanerError('manual_not_ready','Ручная чистка сейчас недоступна',409)
            if snapshot is None:
                raise CleanerError('batch_catalog_stale','Обновите список кампаний перед запуском',409)
            indexed={f"{row['advert_id']}:{row['nm_id']}":row for row in snapshot}
            frozen=[]
            for identity in identities:
                row=indexed.get(identity)
                if not row or not row.get('eligible') or row.get('status') not in categories:
                    raise CleanerError('batch_target_ineligible','Одна из выбранных пар больше недоступна. Обновите список',409)
                frozen.append({key:row[key] for key in ('advert_id','nm_id','campaign_name','product_title','status','status_code')})
            latest=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested' ORDER BY sequence DESC LIMIT 1",(self.key,)).fetchone()
            if latest:
                prior=json.loads(latest['facts'])['batch_id']
                stage=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence DESC LIMIT 1",(self.key,prior)).fetchone()
                if json.loads(stage['facts']).get('state','queued') not in BATCH_TERMINAL_STATES:
                    raise CleanerError('manual_batch_active','Предыдущая массовая чистка не завершена',409)
            latest_job=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_requested' ORDER BY sequence DESC LIMIT 1",(self.key,)).fetchone()
            if latest_job:
                previous=json.loads(latest_job['facts'])['job_id']
                stage=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%' AND json_extract(facts,'$.job_id')=? ORDER BY sequence DESC LIMIT 1",(self.key,previous)).fetchone()
                facts=json.loads(stage['facts'])
                if facts.get('state') not in {'complete','partial','failed','no_change'} or facts.get('can_recheck'):
                    raise CleanerError('manual_job_active','Предыдущая ручная чистка ещё требует сверки',409)
            if c.execute("SELECT 1 FROM cleaner_runs WHERE account=? AND state IN('queued','accepted','running')",(self.key,)).fetchone():
                raise CleanerError('manual_queue_blocked','Есть другое незавершённое задание',409)
            batch_id=payload['request_id'];created_at=self.clock()
            self._event(c,'self_service_batch_requested',dict(batch_id=batch_id,items=frozen,selected_categories=categories,actor=actor,
                         actor_authority='bootstrap_operator' if principal.site_owner else 'configured_owner',
                         account_key=self.key,generation=s['generation'],state='queued',stage='queued',current_index=0,item_updates={}))
            return dict(batch_id=batch_id,state='queued',selected_count=len(frozen),created_at=created_at)
        return self._command(principal,'manual-batches',payload,command)

    def manual_batch_snapshot(self,batch_id:str,principal:Principal) -> dict:
        principal.require_read()
        with self.store.read() as c:
            request=c.execute("SELECT actor,outcome,created_at FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",(self.key,batch_id)).fetchone()
            if not request or request['actor']!=principal.username.strip().casefold():
                raise CleanerError('not_found','Массовая чистка не найдена',404)
            rows=c.execute("SELECT kind,created_at,facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence",(self.key,batch_id)).fetchall()
            if not rows:raise CleanerError('batch_journal_missing','Журнал массовой чистки недоступен',503)
            original=json.loads(rows[0]['facts']);latest=json.loads(rows[-1]['facts'])
            return dict(batch_id=batch_id,items=original['items'],selected_categories=original['selected_categories'],
                        state=latest.get('state','queued'),stage=latest.get('stage','queued'),
                        item_updates=latest.get('item_updates',{}),current_index=latest.get('current_index'),
                        error=latest.get('error'),error_code=latest.get('error_code'),created_at=request['created_at'],updated_at=rows[-1]['created_at'])

    def record_manual_batch(self,batch_id:str,**facts) -> None:
        with self.store.transaction(timeout_ms=30000) as c:
            request=c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",(self.key,batch_id)).fetchone()
            if not request:raise CleanerError('batch_missing','Массовая команда не найдена',404)
            previous=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence DESC LIMIT 1",(self.key,batch_id)).fetchone()
            value=dict(json.loads(previous['facts']) if previous else {})
            value.update(facts)
            value['batch_id']=batch_id
            if 'item_update' in value:
                index,update=value.pop('item_update')
                value['item_updates']=dict(value.get('item_updates') or {},**{str(index):update})
            self._event(c,'self_service_batch_stage',value)

    def resume_drift_batch(self,batch_id:str,payload:Mapping,principal:Principal) -> dict:
        """Explicitly continue the frozen tail after one proven scan-only drift.

        The old terminal event and held target remain immutable. This command
        changes only the parent cursor; each later pair still receives its own
        fresh WB/admission checks and deterministic child identity.
        """
        def command(c,actor):
            from packages.application.search_cluster_cleaner_batch import _terminal_drift_resume_plan,_drift_item_update
            plan=_terminal_drift_resume_plan(c,self,batch_id,actor,
                'bootstrap_operator' if principal.site_owner else 'configured_owner')
            if not plan:raise CleanerError('batch_resume_unavailable','Эту группу нельзя безопасно продолжить',409)
            previous=plan['previous'];index=plan['index']
            updates={key:value for key,value in previous['item_updates'].items() if int(key)<index}
            updates[str(index)]=_drift_item_update(plan['job'],plan['proof'])
            value=dict(previous,state='running',stage='next_target',current_index=index+1,
                       item_updates=updates,error=None,error_code=None,
                       resumed_from_index=index,resume_request_id=payload['request_id'])
            self._event(c,'self_service_batch_resumed',value)
            return dict(batch_id=batch_id,state='running',current_index=index+1,
                        review_required_target=plan['proof']['target'])
        return self._command(principal,f'manual-batches/{batch_id}/resume',payload,command)

    def manual_job(self,job_id:str,principal:Principal) -> dict:
        principal.require_read()
        with self.store.read() as c:
            request=c.execute("SELECT actor,outcome FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-clean'",(self.key,job_id)).fetchone()
            if not request or request['actor']!=principal.username.strip().casefold():
                raise CleanerError('not_found','Ручная чистка не найдена',404)
            initial=json.loads(request['outcome'])
            events=[dict(row) for row in c.execute("SELECT kind,created_at,facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%' AND json_extract(facts,'$.job_id')=? ORDER BY sequence",(self.key,job_id))]
            latest=json.loads(events[-1]['facts']) if events else {}
            result=dict(initial)
            result.update(latest)
            result.update(updated_at=events[-1]['created_at'] if events else None,
                          state=latest.get('state','queued'),stage=latest.get('stage','fetching'))
            result['scan_decisions']=[dict(row) for row in c.execute("""SELECT o.query,o.state,o.observed_state,
              d.verdict,d.rule_id,d.reason,d.source FROM cleaner_observations o
              LEFT JOIN cleaner_auto_decisions d ON d.decision_id=o.decision_id
              WHERE o.account=? AND o.last_run_id=? AND o.target=? ORDER BY o.query""",
              (self.key,result['scan_run_id'],f"{result['advert_id']}:{result['nm_id']}"))]
            return result

    def record_manual_job(self,job_id:str,**facts) -> None:
        with self.store.transaction(timeout_ms=30000) as c:
            request=c.execute("SELECT outcome FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-clean'",(self.key,job_id)).fetchone()
            if not request:raise CleanerError('manual_job_missing','Ручная команда не найдена',404)
            initial=json.loads(request['outcome'])
            previous=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%' AND json_extract(facts,'$.job_id')=? ORDER BY sequence DESC LIMIT 1",(self.key,job_id)).fetchone()
            value=dict(json.loads(previous['facts']) if previous else initial,**facts,job_id=job_id)
            kind='self_service_finished' if value.get('state') in {'complete','partial','failed','no_change'} else 'self_service_stage'
            self._event(c,kind,value,run_id=initial['run_id'])

    def recheck_manual_job(self,job_id:str,payload:Mapping,principal:Principal) -> dict:
        def command(c,actor):
            original=c.execute("SELECT outcome FROM cleaner_requests WHERE account=? AND actor=? AND request_id=? AND route='manual-clean'",(self.key,actor,job_id)).fetchone()
            if not original:raise CleanerError('not_found','Ручная чистка не найдена',404)
            latest=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%' AND json_extract(facts,'$.job_id')=? ORDER BY sequence DESC LIMIT 1",(self.key,job_id)).fetchone()
            facts=json.loads(latest['facts']) if latest else {}
            if facts.get('state')!='partial' or not facts.get('can_recheck') or not str(facts.get('stage','')).endswith('_apply_claimed'):
                raise CleanerError('recheck_unavailable','Для этой операции повторное чтение не требуется',409)
            value=dict(facts,state='ambiguous',can_recheck=False,readback_attempts=0,next_readback_at=0,
                       local_retry_attempts=0)
            self._event(c,'self_service_stage',value,run_id=json.loads(original['outcome'])['run_id'])
            return dict(job_id=job_id,state='ambiguous',stage=value['stage'])
        return self._command(principal,f'manual-clean/{job_id}/recheck',payload,command)

    def stop_unsubmitted_manual_run(self,run_id:str) -> bool:
        """Release only a queued exact run with no binding or write intent."""
        with self.store.transaction(timeout_ms=30000) as c:
            run=c.execute("SELECT state,trigger FROM cleaner_runs WHERE account=? AND run_id=?",(self.key,run_id)).fetchone()
            if not run or run['state']!='queued' or run['trigger'] not in {'manual_exact','manual_exact_candidates'}:return False
            if c.execute('SELECT 1 FROM cleaner_write_operations WHERE account=? AND run_id=?',(self.key,run_id)).fetchone():return False
            if c.execute("SELECT 1 FROM cleaner_events WHERE account=? AND run_id=? AND kind='stage_e_manual_binding'",(self.key,run_id)).fetchone():return False
            c.execute("UPDATE cleaner_runs SET state='stopped',phase='finished',scan_finished_at=?,reason='manual_not_submitted' WHERE run_id=?",(self.clock(),run_id))
            self._event(c,'run_finished',dict(state='stopped',reason='manual_not_submitted'),run_id=run_id)
            return True

    def exact_manual_run_unclaimed(self,run_id:str,operation_id:str,target:Target) -> bool:
        """Prove that a saved apply claim never gained dispatch rights.

        The run claim and Stage E binding commit in one transaction before any
        guarded WB call. A queued run may repeat a fresh preview with the same
        operation ID either before that claim or after the held guard has
        immutably cancelled every proven-unsent preparation.
        """
        with self.store.read() as c:
            run=c.execute("SELECT state,trigger,targets,worker_token,started_at FROM cleaner_runs WHERE account=? AND run_id=?",
                          (self.key,run_id)).fetchone()
            if (not run or run['state']!='queued' or run['trigger'] not in {'manual_exact','manual_exact_candidates'}
                    or run['worker_token'] or run['started_at']):return False
            declared={str(row.get('target') or '') for row in json.loads(run['targets'])}
            if declared!={target.key}:return False
            bindings=[json.loads(row[0]) for row in c.execute(
                "SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='stage_e_manual_binding'",
                (self.key,run_id))]
            operations=c.execute('SELECT operation_id,state,dispatch_count FROM cleaner_write_operations WHERE account=? AND run_id=?',
                                 (self.key,run_id)).fetchall()
            if not bindings and not operations:
                return not c.execute("SELECT 1 FROM cleaner_events WHERE account=? AND kind='stage_e_manual_binding' AND json_extract(facts,'$.operation_id')=?",
                                     (self.key,operation_id)).fetchone()
            if (not bindings or any(row.get('operation_id')!=operation_id or set(row.get('targets') or [])!=declared for row in bindings)
                    or any(row['state']!='cancelled_before_send' or row['dispatch_count']!=0 for row in operations)
                    or any(c.execute('SELECT 1 FROM cleaner_readback_jobs WHERE operation_id=?',(row['operation_id'],)).fetchone() for row in operations)):
                return False
            events=[json.loads(event[0]) for event in c.execute(
                "SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='stage_e_unsent_recovered'",
                (self.key,run_id))]
            recovered={op for event in events if event.get('production_operation_id')==operation_id
                       for op in event.get('cancelled_operations',[])}
            return bool(events) and all(row['operation_id'] in recovered for row in operations)

    def decide(self,review_id:str,payload:Mapping,principal:Principal) -> dict:
        def command(c,actor):
            if self._active_manual_batch(c):
                raise CleanerError('manual_batch_active','Сначала завершите массовую ручную чистку',409)
            s=self._settings(c);verdict=payload.get("decision")
            if verdict not in {"allow","exclude"}: raise CleanerError("invalid_decision","Выберите оставить или исключить")
            if verdict=="exclude" and not s["enabled"] and not s["baseline_ready"]:
                raise CleanerError("disabled","Ручной режим ещё не подготовлен",409)
            r=c.execute("SELECT * FROM cleaner_reviews WHERE account=? AND review_id=?",(self.key,review_id)).fetchone()
            if r is None: raise CleanerError("not_found","Вопрос не найден",404)
            if r["state"]!="open" or r["revision"]!=payload.get("expected_revision"): raise CleanerError("revision_conflict","Вопрос уже изменился",409)
            p=self._profile(c,r["nm_id"])
            if p is None: raise CleanerError("profile_required","Нужна настройка товара")
            h=c.execute("SELECT revision FROM cleaner_override_heads WHERE account=? AND nm_id=? AND query_hash=?",(self.key,r["nm_id"],r["query_hash"])).fetchone()
            revision=(h[0] if h else 0)+1
            c.execute("INSERT INTO cleaner_manual_overrides VALUES(?,?,?,?,?,?,?,?,?)",(self.key,r["nm_id"],r["query_hash"],r["query"],revision,verdict,p.semantic_fingerprint,actor,self.clock()))
            c.execute("INSERT INTO cleaner_override_heads VALUES(?,?,?,?,0) ON CONFLICT(account,nm_id,query_hash) DO UPDATE SET revision=excluded.revision,needs_revalidation=0",(self.key,r["nm_id"],r["query_hash"],revision))
            observations=c.execute("SELECT * FROM cleaner_observations WHERE account=? AND review_id=? AND state IN('review','profile_required','pending_exclude')",(self.key,review_id)).fetchall()
            targets=[];already_excluded=[];execution_blocked=[]
            for obs in observations:
                did=self._decision(c,obs["target"],obs["query"],dict(verdict=verdict,rule="OWNER_EXACT",reason="Решение владельца"),p,"owner_decision",revision)
                excluded=obs["observed_state"]=="excluded"
                state="already_excluded" if excluded else ("allow" if verdict=="allow" else "pending_exclude")
                c.execute("UPDATE cleaner_observations SET state=?,decision_id=? WHERE account=? AND target=? AND query_hash=?",(state,did,self.key,obs["target"],obs["query_hash"]))
                if excluded: already_excluded.append(obs["target"])
                elif verdict=="exclude":
                    # list.active is coverage only.  A saved owner decision is
                    # retained, but it is not eligible for set-minus until the
                    # same exact query is fresh in statistics.
                    if 'statistics' in json.loads(obs['sources']):
                        targets.append(dict(target=obs["target"],query_hash=obs["query_hash"],decision_id=did))
                    else:
                        execution_blocked.append(obs['target'])
            c.execute("UPDATE cleaner_reviews SET state='resolved',revision=revision+1,updated_at=? WHERE review_id=?",(self.clock(),review_id))
            self._cancel_prepared(c,"override_changed",r["nm_id"])
            rid=self._new_run(c,"manual_apply","owner_decision",request_id=payload["request_id"],targets=targets,review_id=review_id,override_revision=revision,apply_group_id=new_id()) if targets else None
            self._event(c,"owner_decision",dict(review_id=review_id,decision=verdict,revision=revision,actor=actor,targets=targets,already_excluded=already_excluded,execution_blocked=execution_blocked),run_id=rid)
            return dict(decision_revision=revision,review_revision=r["revision"]+1,run_id=rid,status=202 if rid else 200,already_excluded=already_excluded,execution_blocked=execution_blocked,admitted_operations=self._admitted(c))
        return self._command(principal,f"reviews/{review_id}/decision",payload,command)

    def _manual_apply_preview(self, c, scan_run_id: str, target: Target) -> dict:
        """Read the exact write scope discovered by one completed manual scan.

        A direct approved rule may yield ``pending_exclude`` without creating a
        review.  The Stage E owner action deliberately turns that already-read
        scope into an ordinary ``manual_apply`` run; it never invents a review
        or lets the scheduler consume it.
        """
        run=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND run_id=?",(self.key,scan_run_id)).fetchone()
        if not run or run['kind']!='scan' or run['trigger']!='manual_exact' or run['state'] not in {'complete','partial'}:
            raise CleanerError('manual_scan_not_ready','Точная ручная проверка ещё не завершена',409)
        declared=json.loads(run['targets'])
        if declared != [dict(target=target.key,advert_id=target.advert_id,nm_id=target.nm_id)]:
            raise CleanerError('manual_scope_drift','Область ручной проверки изменилась',409)
        checked=c.execute("SELECT complete FROM cleaner_run_targets WHERE run_id=? AND target=?",(scan_run_id,target.key)).fetchone()
        if not checked or not checked['complete']:
            raise CleanerError('manual_scan_incomplete','Нет полного свежего снимка кампании',409)
        rows=c.execute("""SELECT o.*,d.rules_version,d.profile_version,d.fingerprint,d.override_revision,d.source
          FROM cleaner_observations o JOIN cleaner_auto_decisions d ON o.decision_id=d.decision_id
          WHERE o.account=? AND o.last_run_id=? AND o.target=? AND o.state='pending_exclude'""",(self.key,scan_run_id,target.key)).fetchall()
        candidates=[]
        for row in rows:
            if 'statistics' not in json.loads(row['sources']): continue
            if c.execute("SELECT 1 FROM cleaner_target_holds WHERE account=? AND target=?",(self.key,target.key)).fetchone(): continue
            if c.execute(f"SELECT 1 FROM cleaner_write_operations WHERE account=? AND target=? AND state IN({BLOCKING_WRITE_STATES})",(self.key,target.key)).fetchone(): continue
            p=self._profile(c,row['nm_id'])
            if not p or p.version!=row['profile_version'] or p.semantic_fingerprint!=row['fingerprint'] or row['rules_version']!=self._settings(c)['rules_version']: continue
            candidates.append(dict(row))
        rows=[dict(target=v['target'],query_hash=v['query_hash'],decision_id=v['decision_id']) for v in candidates]
        if not rows: raise CleanerError('manual_candidates_empty','Нет подтверждённых свежей статистикой исключений',409)
        basis=dict(scan_run_id=scan_run_id,target=target.key,candidates=sorted(rows,key=lambda v:(v['query_hash'],v['decision_id'])))
        return dict(scan_run_id=scan_run_id,target=target.key,candidates=rows,
                    prestate_sha256='sha256:'+digest(dict(run_id=scan_run_id,target=target.key,state=run['state'],checked=bool(checked['complete']))),
                    candidate_sha256='sha256:'+digest(basis))

    def manual_apply_preview(self, scan_run_id: str, target: Target) -> dict:
        with self.store.read() as c:
            return self._manual_apply_preview(c,scan_run_id,target)

    def prepare_manual_apply(self, scan_run_id: str, target: Target, expected_candidate: str, request_id: str, principal: Principal) -> dict:
        def command(c,actor):
            s=self._settings(c)
            if s['enabled'] or not s['baseline_ready']:
                raise CleanerError('manual_not_ready','Ручной режим недоступен',409)
            # Recompute under the command transaction, so a changed decision,
            # target hold or a second run cannot be rebound to an old preview.
            preview=self._manual_apply_preview(c,scan_run_id,target)
            if preview['candidate_sha256'] != expected_candidate:
                raise CleanerError('manual_prepare_drift','Кандидат ручного применения изменился',409)
            active=c.execute("SELECT 1 FROM cleaner_runs WHERE account=? AND state IN('queued','accepted','running')",(self.key,)).fetchone()
            if active: raise CleanerError('manual_queue_blocked','Есть другое незавершённое задание',409)
            rid=self._new_run(c,'manual_apply','manual_exact_candidates',request_id=request_id,targets=preview['candidates'],apply_group_id=new_id())
            self._event(c,'manual_apply_prepared',dict(production_operation_id=request_id,scan_run_id=scan_run_id,target=target.key,candidate_sha256=expected_candidate,count=len(preview['candidates']),actor=actor),run_id=rid)
            return dict(status=202,run_id=rid,target=target.key,candidate_sha256=expected_candidate)
        return self._command(principal,f"manual-scans/{scan_run_id}/prepare-apply",dict(request_id=request_id,target=target.key,expected_candidate=expected_candidate),command)

    def scheduler_tick(self) -> str | None:
        """One current local date, including a due obligation on an in-flight scan."""
        now=self.clock()
        with self.store.transaction() as c:
            s=self._settings(c)
            if not s["enabled"] or not s["baseline_ready"]: return None
            local=timestamp(now).astimezone(ZoneInfo(s["timezone"]))
            hour,minute=map(int,s["schedule_time"].split(":"))
            due=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
            if local<due: return None
            date=local.date().isoformat()
            old=c.execute("SELECT run_id FROM cleaner_schedule_dates WHERE account=? AND local_date=?",(self.key,date)).fetchone()
            if old: return old[0]
            scan=c.execute("SELECT run_id FROM cleaner_runs WHERE account=? AND kind='scan' AND state IN('queued','accepted','running') ORDER BY created_at LIMIT 1",(self.key,)).fetchone()
            rid=scan[0] if scan else self._new_run(c,"scan","scheduled")
            c.execute("INSERT INTO cleaner_schedule_dates VALUES(?,?,?,?)",(self.key,date,rid,due.astimezone(timezone.utc).isoformat(timespec="microseconds")))
            self._event(c,"schedule_due",dict(local_date=date,due_at=due.isoformat()),run_id=rid)
            return rid

    def heartbeat(self,*,generation:str) -> None:
        with self.store.transaction() as c:
            s=self._settings(c)
            if s["generation"]!=generation: raise CleanerError("generation_conflict","Поколение worker не совпало",409)
            c.execute("UPDATE cleaner_settings SET heartbeat_at=? WHERE account=?",(self.clock(),self.key))

    def claim_run(self,*,generation:str,lease_seconds:int=180) -> dict | None:
        now=self.clock()
        with self.store.transaction() as c:
            s=self._settings(c)
            if s["generation"]!=generation: raise CleanerError("generation_conflict","Поколение worker не совпало",409)
            if not s["enabled"] or not s["baseline_ready"]: return None
            active=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND state IN('accepted','running')",(self.key,)).fetchone()
            if active:
                if active["lease_expires_at"] and timestamp(active["lease_expires_at"])>timestamp(now): return None
                # Lease expiry restores only read/preparation work. It never transfers
                # a committed dispatch right; durable operations stay blocked/readback.
                c.execute("UPDATE cleaner_runs SET state='queued',phase='recovered',worker_token=NULL,lease_expires_at=NULL WHERE run_id=?",(active["run_id"],))
                for op in c.execute(f"SELECT operation_id FROM cleaner_write_operations WHERE run_id=? AND state IN({PENDING_WRITE_STATES})",(active["run_id"],)):
                    c.execute("INSERT OR IGNORE INTO cleaner_readback_jobs(operation_id,account) VALUES(?,?)",(op[0],self.key))
                c.execute("""UPDATE cleaner_run_targets SET state='resume_required' WHERE run_id=? AND EXISTS(
                    SELECT 1 FROM cleaner_observations o WHERE o.account=? AND o.target=cleaner_run_targets.target
                    AND o.last_run_id=? AND o.state='pending_exclude') AND NOT EXISTS(
                    SELECT 1 FROM cleaner_write_operations w WHERE w.account=? AND w.target=cleaner_run_targets.target
                    AND w.state IN('dispatching','submitted','unresolved','validation_rejected','rate_limited','unauthorized','forbidden','transport_ambiguous','http_error','requires_review'))""",(active['run_id'],self.key,active['run_id'],self.key))
                self._event(c,"worker_recovered",dict(previous_worker=active["worker_token"]),run_id=active["run_id"])
            run=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND state='queued' ORDER BY created_at,run_id LIMIT 1",(self.key,)).fetchone()
            if not run: return None
            token=new_id()
            c.execute("UPDATE cleaner_runs SET state='running',phase='starting',worker_token=?,lease_expires_at=?,worker_generation=?,started_at=coalesce(started_at,?) WHERE run_id=? AND state='queued'",(token,plus_seconds(now,lease_seconds),generation,now,run["run_id"]))
            self._event(c,"run_claimed",dict(worker_token=token,generation=generation),run_id=run["run_id"])
            return dict(c.execute("SELECT * FROM cleaner_runs WHERE run_id=?",(run["run_id"],)).fetchone())

    def claim_exact_manual_run(self, *, run_id: str, targets: list[Target], generation: str, lease_seconds: int = 180,
                               production_operation_id: str = '', prestate_sha256: str = '', candidate_sha256: str = '') -> dict:
        """Claim one declared manual scope without scheduler/FIFO fallback.

        This is deliberately separate from ``claim_run``: the ordinary worker
        remains unable to execute while ``enabled`` is false.
        """
        if not targets or len({t.key for t in targets}) != len(targets):
            raise CleanerError("manual_scope_invalid", "Нужна непустая точная область ручной проверки", 422)
        now=self.clock(); wanted={t.key for t in targets}
        with self.store.transaction() as c:
            s=self._settings(c)
            if s["generation"] != generation: raise CleanerError("generation_conflict","Поколение worker не совпало",409)
            if s["enabled"] or not s["baseline_ready"]: raise CleanerError("manual_not_ready","Ручной режим недоступен",409)
            run=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND run_id=?",(self.key,run_id)).fetchone()
            if run is None or run["state"] != "queued" or run["trigger"] not in {"manual_exact","manual_exact_candidates","owner_decision"}:
                raise CleanerError("manual_run_not_ready","Ручное задание уже обработано или не соответствует допуску",409)
            others=c.execute("SELECT run_id FROM cleaner_runs WHERE account=? AND run_id<>? AND state IN('queued','accepted','running')",(self.key,run_id)).fetchone()
            if others: raise CleanerError("manual_queue_blocked","Есть другое незавершённое задание",409)
            scope=json.loads(run["targets"])
            if run["kind"] == "scan":
                declared={str(v.get("target") or "") for v in scope}
            else:
                declared={str(v.get("target") or "") for v in scope}
            if declared != wanted:
                raise CleanerError("manual_scope_drift","Область ручного запуска изменилась",409)
            token=new_id()
            c.execute("UPDATE cleaner_runs SET state='running',phase='manual_starting',worker_token=?,lease_expires_at=?,worker_generation=?,started_at=coalesce(started_at,?) WHERE run_id=? AND state='queued'",(token,plus_seconds(now,lease_seconds),generation,now,run_id))
            self._event(c,"run_claimed",dict(worker_token=token,generation=generation,manual=True),run_id=run_id)
            if production_operation_id:
                self._event(c,'stage_e_manual_binding',dict(operation_id=production_operation_id,targets=sorted(wanted),
                    prestate_sha256=prestate_sha256,candidate_sha256=candidate_sha256),run_id=run_id)
            return dict(c.execute("SELECT * FROM cleaner_runs WHERE run_id=?",(run_id,)).fetchone())

    def _lease(self,c,run_id,token,generation,*,require_enabled=False) -> sqlite3.Row:
        row=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND run_id=?",(self.key,run_id)).fetchone()
        s=self._settings(c)
        if row is None or row["state"]!="running" or row["worker_token"]!=token or row["worker_generation"]!=generation or s["generation"]!=generation or not row["lease_expires_at"] or timestamp(row["lease_expires_at"])<=timestamp(self.clock()):
            raise CleanerError("lease_lost","Право исполнения задания потеряно",409)
        if require_enabled and not s["enabled"]: raise CleanerError("disabled","Чистка выключена",409)
        return row

    def renew_lease(self,run_id:str,token:str,generation:str,*,phase:str,lease_seconds:int=180,manual_only:bool=False) -> None:
        with self.store.transaction() as c:
            self._lease(c,run_id,token,generation)
            if manual_only and self._settings(c)["enabled"]: raise CleanerError("manual_mode_changed","Авточистка включена",409)
            c.execute("UPDATE cleaner_runs SET lease_expires_at=?,phase=? WHERE run_id=?",(plus_seconds(self.clock(),lease_seconds),phase,run_id))
            if not manual_only:
                c.execute("UPDATE cleaner_settings SET heartbeat_at=? WHERE account=?",(self.clock(),self.key))

    def sync_catalog(self,run_id:str,token:str,generation:str,targets:list[Target],errors:list[str]) -> None:
        keys=[t.key for t in targets]
        if len(keys)!=len(set(keys)): raise CleanerError("duplicate_target","Повтор пары в каталоге")
        with self.store.transaction() as c:
            self._lease(c,run_id,token,generation,require_enabled=True)
            # A partial catalogue never marks unseen prior targets unavailable.
            if not errors: c.execute("UPDATE cleaner_scan_queue SET available=0 WHERE account=?",(self.key,))
            order=c.execute("SELECT coalesce(max(scan_order),0) FROM cleaner_scan_queue WHERE account=?",(self.key,)).fetchone()[0]
            for t in targets:
                old=c.execute("SELECT scan_order FROM cleaner_scan_queue WHERE account=? AND target=?",(self.key,t.key)).fetchone()
                if not old: order+=1
                c.execute("INSERT INTO cleaner_scan_queue(account,target,advert_id,nm_id,scan_order,metadata,available) VALUES(?,?,?,?,?,?,1) ON CONFLICT(account,target) DO UPDATE SET metadata=excluded.metadata,available=1",(self.key,t.key,t.advert_id,t.nm_id,old[0] if old else order,canonical(asdict(t))))
            c.execute("UPDATE cleaner_runs SET reason=? WHERE run_id=?",("; ".join(errors),run_id))
            self._event(c,"catalog_observed",dict(pairs=len(targets),errors=errors),run_id=run_id)

    def next_scan_target(self,run_id:str,token:str,generation:str) -> Target | None:
        """Fair persisted circular queue; failed targets advance the same cursor."""
        with self.store.transaction() as c:
            self._lease(c,run_id,token,generation,require_enabled=True)
            s=self._settings(c);now=self.clock()
            due=c.execute("SELECT max(due_at) FROM cleaner_schedule_dates WHERE run_id=?",(run_id,)).fetchone()[0]
            rows=c.execute("SELECT * FROM cleaner_scan_queue WHERE account=? AND available=1 ORDER BY CASE WHEN scan_order>? THEN 0 ELSE 1 END,scan_order",(self.key,s["cursor"])).fetchall()
            for r in rows:
                done=c.execute("SELECT observed_at,state FROM cleaner_run_targets WHERE run_id=? AND target=?",(run_id,r["target"])).fetchone()
                # An early manual read cannot satisfy an obligation acquired later.
                if done and done["state"]!="resume_required" and (not due or (done[0] and timestamp(done[0])>=timestamp(due))): continue
                if r["retry_not_before"] and timestamp(r["retry_not_before"])>timestamp(now): continue
                return Target(**json.loads(r["metadata"]))
            return None

    def record_snapshot(self,run_id:str,token:str,generation:str,snapshot:Snapshot,*,manual_only:bool=False) -> dict:
        t=snapshot.target;counts=dict(new_checked=0,allow=0,would_exclude=0,review=0,profile_required=0,excluded_not_executed=0)
        with self.store.transaction() as c:
            self._lease(c,run_id,token,generation)
            if manual_only and self._settings(c)["enabled"]: raise CleanerError("manual_mode_changed","Авточистка включена",409)
            old_result=c.execute("SELECT counters FROM cleaner_run_targets WHERE run_id=? AND target=?",(run_id,t.key)).fetchone()
            if old_result: counts.update(json.loads(old_result[0]))
            reason=t.unsupported_reason or "; ".join(snapshot.reasons)
            if not snapshot.complete or reason:
                self._record_target(c,run_id,t,"partial",False,reason or "incomplete_snapshot",snapshot.observed_at,counts,snapshot.source_times)
                return counts
            if len(snapshot.minus)!=len(set(snapshot.minus)) or any(q not in snapshot.queries or snapshot.queries[q]!="excluded" for q in snapshot.minus):
                raise CleanerError("invalid_snapshot","Некорректный полный список исключений")
            now=self.clock()
            previous_minus=c.execute("SELECT query_hash,query FROM cleaner_observations WHERE account=? AND target=? AND observed_state='excluded'",(self.key,t.key)).fetchall()
            for prior in previous_minus:
                if prior["query"] not in snapshot.minus:
                    c.execute("INSERT OR IGNORE INTO cleaner_target_holds VALUES(?,?,?,?)",(self.key,t.key,"external_state_drift",now))
                    c.execute("UPDATE cleaner_observations SET state='external_state_drift' WHERE account=? AND target=? AND query_hash=?",(self.key,t.key,prior["query_hash"]))
                    self._event(c,"external_state_drift",dict(target=t.key,query_hash=prior["query_hash"]),run_id=run_id)
            for q,observed in sorted(snapshot.queries.items()):
                if observed not in {"active","excluded","archived","statistics"}: raise CleanerError("invalid_snapshot","Неизвестное состояние кластера")
                qh=query_hash(q)
                old=c.execute("SELECT * FROM cleaner_observations WHERE account=? AND target=? AND query_hash=?",(self.key,t.key,qh)).fetchone()
                if old and old["query"]!=q: raise CleanerError("query_collision","Нарушена точная идентичность запроса",409)
                if old:
                    c.execute("UPDATE cleaner_observations SET last_seen=?,observed_state=?,sources=?,source_times=? WHERE account=? AND target=? AND query_hash=?",(snapshot.observed_at,observed,canonical(snapshot.sources.get(q,())),canonical(snapshot.source_times),self.key,t.key,qh))
                    if old["observed_state"]=="excluded" and observed in {"active","statistics"}:
                        c.execute("INSERT OR IGNORE INTO cleaner_target_holds VALUES(?,?,?,?)",(self.key,t.key,"external_state_drift",now))
                        c.execute("UPDATE cleaner_observations SET state='external_state_drift' WHERE account=? AND target=? AND query_hash=?",(self.key,t.key,qh))
                        self._event(c,"external_state_drift",dict(target=t.key,query_hash=qh),run_id=run_id)
                        continue
                    if old["state"] in FINAL_OBSERVATION_STATES: continue
                else:
                    c.execute("INSERT INTO cleaner_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(self.key,t.key,t.advert_id,t.nm_id,qh,q,snapshot.observed_at,snapshot.observed_at,observed,canonical(snapshot.sources.get(q,())),canonical(snapshot.source_times),"new",None,None,run_id))
                obs=dict(account=self.key,target=t.key,nm_id=t.nm_id,query_hash=qh,query=q)
                if observed in {"excluded","archived"}:
                    state="observed_excluded" if observed=="excluded" else "observed_archived"
                    c.execute("UPDATE cleaner_observations SET state=? WHERE account=? AND target=? AND query_hash=?",(state,self.key,t.key,qh))
                    continue
                # Until dispatch a decision is a candidate, so a changed rule or
                # semantic profile may be recalculated. Final prior decisions pin.
                verdict=self._classify_observation(c,obs,run_id)
                if old is None or old["decision_id"] is None:
                    if verdict=="profile_required": counts["profile_required"]+=1
                    else:
                        counts["new_checked"]+=1
                        counts[{"allow":"allow","exclude":"would_exclude","review":"review"}[verdict]]+=1
                if verdict=='exclude' and 'statistics' not in snapshot.sources.get(q,()):
                    counts['excluded_not_executed']+=1
                    self._event(c,'execution_blocked',dict(target=t.key,query_hash=qh,reason='list_only'),run_id=run_id)
            self._sync_reviews(c)
            state="partial" if counts["profile_required"] else "done"
            hold=c.execute("SELECT reason FROM cleaner_target_holds WHERE account=? AND target=?",(self.key,t.key)).fetchone()
            if hold: state="partial";reason=hold[0]
            self._record_target(c,run_id,t,state,True,reason,snapshot.observed_at,counts,snapshot.source_times)
            return counts

    def record_target_error(self,run_id,token,generation,target,reason,*,retry_after_seconds=0):
        with self.store.transaction() as c:
            self._lease(c,run_id,token,generation)
            previous=c.execute("SELECT counters,source_times FROM cleaner_run_targets WHERE run_id=? AND target=?",(run_id,target.key)).fetchone()
            counts=json.loads(previous["counters"]) if previous else {}
            source_times=json.loads(previous["source_times"]) if previous else {}
            self._record_target(c,run_id,target,"partial",False,reason,self.clock(),counts,source_times)
            if retry_after_seconds:
                c.execute("UPDATE cleaner_scan_queue SET retry_not_before=? WHERE account=? AND target=?",(plus_seconds(self.clock(),retry_after_seconds),self.key,target.key))

    def _record_target(self,c,run_id,t,state,complete,reason,observed_at,counts,source_times):
        c.execute("INSERT INTO cleaner_run_targets VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,target) DO UPDATE SET state=excluded.state,complete=excluded.complete,reason=excluded.reason,observed_at=excluded.observed_at,counters=excluded.counters,source_times=excluded.source_times",(run_id,t.key,canonical(asdict(t)),state,int(complete),reason,observed_at,canonical(counts),canonical(source_times)))
        row=c.execute("SELECT scan_order FROM cleaner_scan_queue WHERE account=? AND target=?",(self.key,t.key)).fetchone()
        if row:
            c.execute("UPDATE cleaner_scan_queue SET last_attempted_at=?,retry_not_before=NULL WHERE account=? AND target=?",(self.clock(),self.key,t.key))
            c.execute("UPDATE cleaner_settings SET cursor=? WHERE account=?",(row[0],self.key))

    def pending_candidates(self,run_id:str) -> list[dict]:
        with self.store.read() as c:
            run=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND run_id=?",(self.key,run_id)).fetchone()
            if run is None: raise CleanerError("not_found","Запуск не найден",404)
            scope=json.loads(run["targets"])
            rows=c.execute("""SELECT o.*,d.rules_version,d.profile_version,d.fingerprint,d.override_revision,d.source FROM cleaner_observations o
              JOIN cleaner_auto_decisions d ON o.decision_id=d.decision_id
              WHERE o.account=? AND o.state='pending_exclude'""",(self.key,)).fetchall()
            allowed={(v["target"],v["query_hash"],v["decision_id"]) for v in scope} if run["kind"]=="manual_apply" else None
            results=[]
            for row in rows:
                if allowed is not None and (row["target"],row["query_hash"],row["decision_id"]) not in allowed: continue
                if allowed is None and row["last_run_id"]!=run_id: continue
                if c.execute("SELECT 1 FROM cleaner_target_holds WHERE account=? AND target=?",(self.key,row["target"])).fetchone(): continue
                if c.execute(f"SELECT 1 FROM cleaner_write_operations WHERE account=? AND target=? AND state IN({BLOCKING_WRITE_STATES})",(self.key,row["target"])).fetchone(): continue
                p=self._profile(c,row["nm_id"])
                if p is None or p.version!=row["profile_version"] or p.semantic_fingerprint!=row["fingerprint"] or row["rules_version"]!=self._settings(c)["rules_version"]: continue
                override=c.execute("SELECT revision,needs_revalidation FROM cleaner_override_heads WHERE account=? AND nm_id=? AND query_hash=?",(self.key,row["nm_id"],row["query_hash"])).fetchone()
                if override and (override["needs_revalidation"] or row["override_revision"]!=override["revision"]): continue
                if not override and row["override_revision"] is not None: continue
                item=dict(row)
                item['execution_eligibility']='statistics_fresh' if 'statistics' in json.loads(row['sources']) else 'list_only'
                results.append(item)
            return results

    def finish_run(self,run_id:str,token:str,generation:str,*,reason:str="",remaining:list[Mapping] | None=None,manual_only:bool=False) -> dict:
        with self.store.transaction() as c:
            run=self._lease(c,run_id,token,generation)
            s=self._settings(c);state="complete"
            if manual_only and s["enabled"]: raise CleanerError("manual_mode_changed","Авточистка включена",409)
            targets=c.execute("SELECT * FROM cleaner_run_targets WHERE run_id=?",(run_id,)).fetchall()
            summary=dict(new_checked=0,allow=0,would_exclude=0,review=0,profile_required=0,excluded_not_executed=0,confirmed_automatic=0,confirmed_manual=0,confirmed_pilot=0,pairs=len(targets),campaigns=len({json.loads(t["metadata"])["advert_id"] for t in targets}),dry_run=not bool(run["transport_enabled"]))
            for t in targets:
                for k,v in json.loads(t["counters"]).items():
                    if k in summary: summary[k]+=v
            for event in c.execute("SELECT facts FROM cleaner_events WHERE run_id=? AND kind='readback_result'",(run_id,)):
                counts=json.loads(event[0])
                for field in ('confirmed_automatic','confirmed_manual','confirmed_pilot'): summary[field]+=counts.get(field,0)
            summary['unresolved_operations']=c.execute(f"SELECT count(*) FROM cleaner_write_operations WHERE run_id=? AND state IN({PENDING_WRITE_STATES})",(run_id,)).fetchone()[0]
            summary['requires_review_operations']=c.execute("SELECT count(*) FROM cleaner_write_operations WHERE run_id=? AND state='requires_review'",(run_id,)).fetchone()[0]
            summary['rejected_not_executed']=c.execute("""SELECT count(*) FROM cleaner_write_items i JOIN cleaner_write_operations o USING(operation_id)
                WHERE o.run_id=? AND o.state='rejected' AND i.confirmed_at IS NULL""",(run_id,)).fetchone()[0]
            if summary['unresolved_operations'] or summary['requires_review_operations'] or summary['rejected_not_executed'] or summary['excluded_not_executed']: state='partial'
            if any(t["state"]!="done" for t in targets) or reason or run["reason"]: state="partial"
            if not s["enabled"] and not manual_only: state="stopped"
            if run["kind"]=="scan" and run["trigger"]!="manual_exact":
                due=c.execute("SELECT max(due_at) FROM cleaner_schedule_dates WHERE run_id=?",(run_id,)).fetchone()[0]
                available=c.execute("SELECT target FROM cleaner_scan_queue WHERE account=? AND available=1",(self.key,)).fetchall()
                completed={r["target"] for r in targets if not due or (r["observed_at"] and timestamp(r["observed_at"])>=timestamp(due))}
                missing=[r[0] for r in available if r[0] not in completed]
                summary["unread_pairs"]=len(missing)
                if missing: state="partial" if (s["enabled"] or manual_only) else "stopped"
            if remaining and run["kind"]=="manual_apply":
                original={(v["target"],v["query_hash"],v["decision_id"]) for v in json.loads(run["targets"])}
                retained=[]
                for item in remaining:
                    identity=(item["target"],item["query_hash"],item["decision_id"])
                    if identity not in original: raise CleanerError("invalid_continuation","Цель не принадлежит исходному заданию",409)
                    obs=c.execute("SELECT state,decision_id FROM cleaner_observations WHERE account=? AND target=? AND query_hash=?",(self.key,item["target"],item["query_hash"])).fetchone()
                    uncertain=c.execute(f"SELECT 1 FROM cleaner_write_operations WHERE account=? AND target=? AND state IN({BLOCKING_WRITE_STATES})",(self.key,item["target"])).fetchone()
                    if obs and obs["state"]=="pending_exclude" and obs["decision_id"]==item["decision_id"] and not uncertain and item not in retained: retained.append(item)
                remaining=retained
            if remaining and run["kind"]=="manual_apply":
                # Every continuation retains exact decision identities. The next
                # worker filters changed overrides and any prior dispatch intent.
                n=c.execute("SELECT coalesce(max(continuation_number),0)+1 FROM cleaner_runs WHERE apply_group_id=?",(run["apply_group_id"],)).fetchone()[0]
                child=self._new_run(c,"manual_apply","continuation",targets=remaining,review_id=run["review_id"],override_revision=run["override_revision"],apply_group_id=run["apply_group_id"],continuation_number=n)
                summary["continuation_run_id"]=child;state="partial" if s["enabled"] else "stopped"
            c.execute("UPDATE cleaner_runs SET state=?,phase='finished',scan_finished_at=?,summary=?,reason=?,worker_token=NULL,lease_expires_at=NULL WHERE run_id=?",(state,self.clock(),canonical(summary),reason or run["reason"],run_id))
            self._event(c,"run_finished",dict(state=state,summary=summary),run_id=run_id)
            return dict(run_id=run_id,state=state,summary=summary)

    def set_manual_transport(self, *, enabled: bool, generation: str) -> None:
        """Runner-only marker; never enables scheduler or a generic worker."""
        with self.store.transaction() as c:
            s=self._settings(c)
            if s["enabled"] or (enabled and s["generation"] != generation):
                raise CleanerError("manual_mode_changed","Авточистка включена или поколение изменилось",409)
            c.execute("UPDATE cleaner_settings SET transport_enabled=? WHERE account=?",(int(enabled),self.key))

    def set_manual_restore_hold(self, *, held: bool, generation: str) -> None:
        """Temporary runner-only release; automatic execution stays disabled."""
        with self.store.transaction() as c:
            s=self._settings(c)
            if s["enabled"] or s["generation"] != generation or not s["baseline_ready"]:
                raise CleanerError("manual_mode_changed","Ручной допуск изменился",409)
            c.execute("UPDATE cleaner_settings SET restore_hold=? WHERE account=?",(int(held),self.key))

    def close_manual_window(self) -> None:
        """Fail closed in one transaction, including an exception path."""
        with self.store.transaction(timeout_ms=30000) as c:
            c.execute("UPDATE cleaner_settings SET transport_enabled=0,restore_hold=1 WHERE account=?",(self.key,))

    def finalize_recovered_manual_run(self, *, run_id: str, production_operation_id: str, allow_no_operations: bool = False) -> str:
        """Close only the exact crashed manual run after terminal readback."""
        with self.store.transaction() as c:
            s=self._settings(c)
            if s['enabled']: raise CleanerError('manual_mode_changed','Авточистка включена',409)
            run=c.execute('SELECT * FROM cleaner_runs WHERE account=? AND run_id=?',(self.key,run_id)).fetchone()
            if not run or run['state'] not in {'running','accepted'}: return run['state'] if run else 'missing'
            bindings=[json.loads(row[0]) for row in c.execute("SELECT facts FROM cleaner_events WHERE run_id=? AND kind='stage_e_manual_binding'",(run_id,))]
            if not any(v.get('operation_id')==production_operation_id for v in bindings): raise CleanerError('manual_binding_missing','Нет точной привязки ручного запуска',409)
            operations=c.execute("SELECT state FROM cleaner_write_operations WHERE account=? AND run_id=? AND state!='cancelled_before_send'",(self.key,run_id)).fetchall()
            if not operations:
                if not allow_no_operations or not run['lease_expires_at'] or timestamp(run['lease_expires_at'])>timestamp(self.clock()):
                    raise CleanerError('manual_readback_pending','Результат WB ещё не завершён',409)
                state='partial'
                summary=dict(recovered_manual=True,confirmed_pilot=0)
                if run['kind']=='scan':
                    targets=c.execute('SELECT target,state,complete,reason,counters FROM cleaner_run_targets WHERE run_id=?',(run_id,)).fetchall()
                    declared={str(item.get('target') or '') for item in json.loads(run['targets'])}
                    if {item['target'] for item in targets}==declared and targets:
                        summary.update(new_checked=0,allow=0,would_exclude=0,review=0,profile_required=0,
                                       excluded_not_executed=0,confirmed_automatic=0,confirmed_manual=0,
                                       unresolved_operations=0,requires_review_operations=0,rejected_not_executed=0,
                                       dry_run=True,pairs=len(targets),campaigns=len({item['target'].split(':')[0] for item in targets}))
                        for target in targets:
                            for key,value in json.loads(target['counters']).items():
                                if key in summary:summary[key]+=value
                        if (all(item['state']=='done' and item['complete'] and not item['reason'] for item in targets)
                                and not summary['excluded_not_executed'] and not run['reason']):
                            state='complete'
            elif any(row['state'] not in {'confirmed','rejected','requires_review'} for row in operations):
                raise CleanerError('manual_readback_pending','Результат WB ещё не завершён',409)
            else:
                state='complete' if all(row['state']=='confirmed' for row in operations) else 'partial'
                summary=dict(recovered_manual=True,confirmed_pilot=c.execute("SELECT count(*) FROM cleaner_write_items i JOIN cleaner_write_operations o USING(operation_id) WHERE o.run_id=? AND i.confirmed_at IS NOT NULL",(run_id,)).fetchone()[0])
            c.execute("UPDATE cleaner_runs SET state=?,phase='finished',scan_finished_at=coalesce(scan_finished_at,?),summary=?,worker_token=NULL,lease_expires_at=NULL WHERE run_id=?",(state,self.clock(),canonical(summary),run_id))
            self._event(c,'run_finished',dict(state=state,summary=summary,recovered=True),run_id=run_id)
            return state

    def queue_readback(self,operation_id:str) -> None:
        with self.store.transaction() as c:
            op=c.execute("SELECT state FROM cleaner_write_operations WHERE account=? AND operation_id=?",(self.key,operation_id)).fetchone()
            if op is None or op["state"] not in {"dispatching","submitted","unresolved"}: raise CleanerError("readback_not_applicable","Нет допущенной операции для сверки",409)
            c.execute("INSERT OR IGNORE INTO cleaner_readback_jobs(operation_id,account) VALUES(?,?)",(operation_id,self.key))

    def claim_readback(self,*,generation:str,lease_seconds:int=90,operation_id:str|None=None) -> dict | None:
        # Independent of the account scan slot and enabled toggle. No submit
        # authority is returned even after expiry of this read-only lease.
        with self.store.transaction() as c:
            if self._settings(c)["generation"]!=generation: raise CleanerError("generation_conflict","Поколение worker не совпало",409)
            now=self.clock()
            if operation_id:
                row=c.execute("SELECT * FROM cleaner_readback_jobs WHERE account=? AND operation_id=? AND state!='done' AND (retry_not_before IS NULL OR retry_not_before<=?) AND (lease_expires_at IS NULL OR lease_expires_at<=?)",(self.key,operation_id,now,now)).fetchone()
            else:
                row=c.execute("SELECT * FROM cleaner_readback_jobs WHERE account=? AND state!='done' AND (retry_not_before IS NULL OR retry_not_before<=?) AND (lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY coalesce(retry_not_before,''),operation_id LIMIT 1",(self.key,now,now)).fetchone()
            if not row: return None
            token=new_id()
            c.execute("UPDATE cleaner_readback_jobs SET state='running',worker_token=?,lease_expires_at=?,attempts=attempts+1 WHERE operation_id=?",(token,plus_seconds(now,lease_seconds),row["operation_id"]))
            return dict(operation_id=row["operation_id"],worker_token=token,rights="readback_only",attempt=row["attempts"]+1)

    def run_detail(self,run_id:str,principal:Principal) -> dict:
        principal.require_read()
        with self.store.read() as c:
            row=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND run_id=?",(self.key,run_id)).fetchone()
            if row is None: raise CleanerError("not_found","Запуск не найден",404)
            result=self._run_payload(row)
            result["settlement"]=dict(confirmed_automatic=0,confirmed_manual=0)
            for event in c.execute("SELECT facts FROM cleaner_events WHERE run_id=? AND kind='late_confirmation'",(run_id,)):
                facts=json.loads(event[0])
                for field in result['settlement']:result['settlement'][field]+=facts.get(field,0)
            result["targets"]=[dict(r) for r in c.execute("SELECT * FROM cleaner_run_targets WHERE run_id=? ORDER BY target",(run_id,))]
            operations=[]
            for operation in c.execute("SELECT operation_id,target,state,additions,evidence FROM cleaner_write_operations WHERE account=? AND run_id=? ORDER BY created_at,operation_id",(self.key,run_id)):
                items=[dict(item) for item in c.execute("""SELECT i.query,i.query_hash,i.decision_id,i.state,i.confirmed_at,
                  d.rule_id,d.reason,d.source FROM cleaner_write_items i
                  JOIN cleaner_auto_decisions d ON d.decision_id=i.decision_id
                  WHERE i.operation_id=? ORDER BY i.query""",(operation['operation_id'],))]
                operations.append(dict(operation_id=operation['operation_id'],target=operation['target'],state=operation['state'],items=items))
            result['write_operations']=operations
            effective_operations=[operation for operation in operations if operation['state']!='cancelled_before_send']
            written={(operation['target'],item['query_hash'],item['decision_id']):item
                     for operation in effective_operations for item in operation['items']}
            phrases=[]
            if row['kind']=='manual_apply':
                for candidate in json.loads(row['targets']):
                    identity=(candidate['target'],candidate['query_hash'],candidate['decision_id'])
                    decision=c.execute("SELECT query,rule_id,reason,source FROM cleaner_auto_decisions WHERE decision_id=?",(candidate['decision_id'],)).fetchone()
                    if decision:
                        item=written.get(identity)
                        phrases.append(dict(target=candidate['target'],query=decision['query'],query_hash=candidate['query_hash'],
                                            decision_id=candidate['decision_id'],rule_id=decision['rule_id'],
                                            reason=decision['reason'],source=decision['source'],state=item['state'] if item else 'not_submitted',
                                            confirmed_at=item['confirmed_at'] if item else None))
            result['phrases']=phrases
            # A completed run is an immutable snapshot. Late readback changes
            # the effective outcome, not that historical run summary.
            if effective_operations:
                states={operation['state'] for operation in effective_operations}
                actual={(operation['target'],item['query_hash'],item['decision_id']) for operation in effective_operations for item in operation['items']}
                expected={(candidate['target'],candidate['query_hash'],candidate['decision_id']) for candidate in json.loads(row['targets'])} if row['kind']=='manual_apply' else actual
                if (expected and expected==actual and states=={'confirmed'}
                        and all(item['confirmed_at'] for operation in effective_operations for item in operation['items'])
                        and all(v['state']=='confirmed' for v in phrases)):
                    result['effective_state']='complete'
                elif states & {'rejected','requires_review'}:
                    result['effective_state']='partial'
                else:
                    result['effective_state']='unresolved'
            else:
                result['effective_state']=result['state']
            return result

    @staticmethod
    def _run_payload(row) -> dict | None:
        if row is None: return None
        result={k:row[k] for k in ("run_id","kind","trigger","state","phase","created_at","started_at","scan_finished_at","settled_at","reason","apply_group_id","continuation_number")}
        result["summary"]=json.loads(row["summary"])
        return result

    def reviews(self,principal:Principal,*,cursor:str="",limit:int=50) -> dict:
        principal.require_read();limit=max(1,min(int(limit),100))
        with self.store.read() as c:
            rows=c.execute("SELECT * FROM cleaner_reviews WHERE account=? AND state='open' AND review_id>? ORDER BY review_id LIMIT ?",(self.key,cursor,limit+1)).fetchall()
            items=[]
            for r in rows[:limit]:
                item=dict(r)
                item["targets"]=[dict(o) for o in c.execute("SELECT target,advert_id,nm_id,observed_state,state FROM cleaner_observations WHERE account=? AND review_id=? AND state IN('review','profile_required','pending_exclude') ORDER BY target",(self.key,r["review_id"]))]
                item["campaign_count"]=len({t["advert_id"] for t in item["targets"]})
                items.append(item)
            return dict(items=items,next_cursor=items[-1]["review_id"] if len(rows)>limit else None)

    def history(self,principal:Principal,*,cursor:int=0,limit:int=50) -> dict:
        principal.require_read();limit=max(1,min(int(limit),100))
        with self.store.read() as c:
            rows=c.execute("SELECT * FROM cleaner_events WHERE account=? AND (?=0 OR sequence<?) ORDER BY sequence DESC LIMIT ?",(self.key,cursor,cursor,limit+1)).fetchall()
            items=[dict(r,facts=json.loads(r["facts"])) for r in rows[:limit]]
            return dict(items=items,next_cursor=items[-1]["sequence"] if len(rows)>limit else None)

    def _confirmation_totals(self,c):
        result=dict(automatic=0,manual=0,late_automatic=0,late_manual=0)
        for row in c.execute("SELECT kind,facts FROM cleaner_events WHERE account=? AND kind IN('readback_result','late_confirmation')",(self.key,)):
            facts=json.loads(row['facts'])
            for field in ('automatic','manual'):
                result[field]+=facts.get('confirmed_'+field,0)
                if row['kind']=='late_confirmation':result['late_'+field]+=facts.get('confirmed_'+field,0)
            if facts.get('confirmed_pilot',0):
                result['pilot']=result.get('pilot',0)+facts['confirmed_pilot']
                if row['kind']=='late_confirmation':result['late_pilot']=result.get('late_pilot',0)+facts['confirmed_pilot']
        return result

    def summary(self,principal:Principal) -> dict:
        principal.require_read();now=self.clock()
        with self.store.read() as c:
            s=self._settings(c)
            last=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND kind='scan' ORDER BY created_at DESC LIMIT 1",(self.key,)).fetchone()
            active=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND state IN('accepted','running')",(self.key,)).fetchone()
            queued=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND state='queued' ORDER BY created_at LIMIT 20",(self.key,)).fetchall()
            pending=c.execute("SELECT count(*) FROM cleaner_reviews WHERE account=? AND state='open'",(self.key,)).fetchone()[0]
            unresolved=self._admitted(c)
            requires_review=c.execute("SELECT count(*) FROM cleaner_write_operations WHERE account=? AND state='requires_review'",(self.key,)).fetchone()[0]
            rejected_items=c.execute("""SELECT count(*) FROM cleaner_write_items i JOIN cleaner_write_operations o USING(operation_id)
                WHERE o.account=? AND o.state='rejected' AND i.confirmed_at IS NULL""",(self.key,)).fetchone()[0]
            execution_blocked=0
            for row in c.execute("SELECT sources FROM cleaner_observations WHERE account=? AND state='pending_exclude'",(self.key,)):
                if 'statistics' not in json.loads(row['sources']): execution_blocked+=1
            profile_required=c.execute("SELECT count(DISTINCT nm_id) FROM cleaner_observations WHERE account=? AND state='profile_required'",(self.key,)).fetchone()[0]
            holds=c.execute("SELECT count(*) FROM cleaner_target_holds WHERE account=?",(self.key,)).fetchone()[0]
            errors=[]
            if s["enabled"] and s["baseline_ready"]:
                if not s["heartbeat_at"] or timestamp(now)-timestamp(s["heartbeat_at"])>timedelta(minutes=5): errors.append("worker_heartbeat_missing")
                local=timestamp(now).astimezone(ZoneInfo(s["timezone"]));h,m=map(int,s["schedule_time"].split(":"));due=local.replace(hour=h,minute=m,second=0,microsecond=0)
                if local>due+timedelta(minutes=5) and not c.execute("SELECT 1 FROM cleaner_schedule_dates WHERE account=? AND local_date=?",(self.key,local.date().isoformat())).fetchone(): errors.append("scheduled_run_overdue")
            if last and last["state"] in {"partial","failed"}: errors.append("last_scan_partial")
            return dict(settings=dict(enabled=bool(s["enabled"]),revision=s["revision"],schedule_time=s["schedule_time"],timezone=s["timezone"],baseline_ready=bool(s["baseline_ready"]),restore_hold=bool(s["restore_hold"])),last_scan=self._run_payload(last),current_work=self._run_payload(active),queued=[self._run_payload(r) for r in queued],pending_count=pending,unresolved_count=unresolved,requires_review_count=requires_review,rejected_item_count=rejected_items,execution_blocked_count=execution_blocked,profile_required_count=profile_required,target_holds=holds,errors=errors,indicator=bool(pending or unresolved or requires_review or rejected_items or execution_blocked or profile_required or holds or errors),coverage=COVERAGE_NOTICE,transport_enabled=bool(s["transport_enabled"]),dry_run=not bool(s["transport_enabled"]),confirmed=self._confirmation_totals(c))

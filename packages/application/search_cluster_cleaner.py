"""Durable keyword-cleaner application; this module cannot send WB mutations.

HTTP callers only enqueue commands or read saved state. A separately invoked
worker uses the read-only source port. The write boundary is added in stage D.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import marshal
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
from packages.domain import search_cluster_classifier as rules_package
from packages.contracts.search_cluster_cleaner import MODEL_CATALOG

FINAL_OBSERVATION_STATES = {"allow", "confirmed", "baseline", "observed_excluded", "observed_archived", "external_state_drift", "already_excluded"}


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
        # Hash loaded executable code, vocabulary and profile validator, not only
        # a mutable path on disk or a caller-supplied version label.
        executable=(self.classifier.__code__,rules_package.norm.__code__,rules_package.models.__code__,Profile.parse.__func__.__code__,query_hash.__code__,rules_package.BRANDS,rules_package.PRODUCT,rules_package.VOCAB,rules_package.BROAD_WORDS,tuple(sorted(MODEL_CATALOG)))
        self.rules_digest=hashlib.sha256(marshal.dumps(executable)).hexdigest()

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

    def _command(self, principal: Principal, route: str, payload: Mapping, operation: Callable) -> dict:
        principal.require_owner(self.owner_username)
        request_id = payload.get("request_id")
        if not isinstance(request_id,str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,119}",request_id) is None:
            raise CleanerError("request_id_required", "Нужен идентификатор команды")
        actor = principal.username.strip().casefold()
        fingerprint = digest([route,dict(payload)])
        with self.store.transaction() as c:
            previous = c.execute("SELECT * FROM cleaner_requests WHERE account=? AND actor=? AND request_id=?",(self.key,actor,request_id)).fetchone()
            if previous:
                if previous["digest"] != fingerprint: raise CleanerError("request_conflict", "Идентификатор уже использован другой командой",409)
                return json.loads(previous["outcome"])
            outcome = operation(c,actor)
            c.execute("INSERT INTO cleaner_requests VALUES(?,?,?,?,?,?,?)",(self.key,actor,request_id,route,fingerprint,canonical(outcome),self.clock()))
            return outcome

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
        return c.execute("SELECT count(*) FROM cleaner_write_operations WHERE account=? AND state IN('dispatching','submitted','unresolved')",(self.key,)).fetchone()[0]

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
            s=self._settings(c)
            if not s["enabled"] or not s["baseline_ready"]: raise CleanerError("not_ready","Чистка выключена или исходная база не сверена",409)
            active=c.execute("SELECT run_id,kind FROM cleaner_runs WHERE account=? AND state IN('queued','accepted','running') ORDER BY CASE WHEN state='queued' THEN 1 ELSE 0 END,created_at LIMIT 1",(self.key,)).fetchone()
            if active: return dict(status=202,run_id=active["run_id"],active_run_id=active["run_id"],kind=active["kind"],reused=True)
            rid=self._new_run(c,"scan","manual",request_id=payload["request_id"])
            return dict(status=202,run_id=rid,kind="scan",reused=False)
        return self._command(principal,"runs",payload,command)

    def decide(self,review_id:str,payload:Mapping,principal:Principal) -> dict:
        def command(c,actor):
            s=self._settings(c);verdict=payload.get("decision")
            if verdict not in {"allow","exclude"}: raise CleanerError("invalid_decision","Выберите оставить или исключить")
            if verdict=="exclude" and not s["enabled"]: raise CleanerError("disabled","Чистка выключена",409)
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
            targets=[];already_excluded=[]
            for obs in observations:
                did=self._decision(c,obs["target"],obs["query"],dict(verdict=verdict,rule="OWNER_EXACT",reason="Решение владельца"),p,"owner_decision",revision)
                excluded=obs["observed_state"]=="excluded"
                state="already_excluded" if excluded else ("allow" if verdict=="allow" else "pending_exclude")
                c.execute("UPDATE cleaner_observations SET state=?,decision_id=? WHERE account=? AND target=? AND query_hash=?",(state,did,self.key,obs["target"],obs["query_hash"]))
                if excluded: already_excluded.append(obs["target"])
                elif verdict=="exclude": targets.append(dict(target=obs["target"],query_hash=obs["query_hash"],decision_id=did))
            c.execute("UPDATE cleaner_reviews SET state='resolved',revision=revision+1,updated_at=? WHERE review_id=?",(self.clock(),review_id))
            self._cancel_prepared(c,"override_changed",r["nm_id"])
            rid=self._new_run(c,"manual_apply","owner_decision",request_id=payload["request_id"],targets=targets,review_id=review_id,override_revision=revision,apply_group_id=new_id()) if targets else None
            self._event(c,"owner_decision",dict(review_id=review_id,decision=verdict,revision=revision,actor=actor,targets=targets,already_excluded=already_excluded),run_id=rid)
            return dict(decision_revision=revision,review_revision=r["revision"]+1,run_id=rid,status=202 if rid else 200,already_excluded=already_excluded,admitted_operations=self._admitted(c))
        return self._command(principal,f"reviews/{review_id}/decision",payload,command)

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
                for op in c.execute("SELECT operation_id FROM cleaner_write_operations WHERE run_id=? AND state IN('dispatching','submitted','unresolved')",(active["run_id"],)):
                    c.execute("INSERT OR IGNORE INTO cleaner_readback_jobs(operation_id,account) VALUES(?,?)",(op[0],self.key))
                c.execute("""UPDATE cleaner_run_targets SET state='resume_required' WHERE run_id=? AND EXISTS(
                    SELECT 1 FROM cleaner_observations o WHERE o.account=? AND o.target=cleaner_run_targets.target
                    AND o.last_run_id=? AND o.state='pending_exclude') AND NOT EXISTS(
                    SELECT 1 FROM cleaner_write_operations w WHERE w.account=? AND w.target=cleaner_run_targets.target
                    AND w.state IN('dispatching','submitted','unresolved'))""",(active['run_id'],self.key,active['run_id'],self.key))
                self._event(c,"worker_recovered",dict(previous_worker=active["worker_token"]),run_id=active["run_id"])
            run=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND state='queued' ORDER BY created_at,run_id LIMIT 1",(self.key,)).fetchone()
            if not run: return None
            token=new_id()
            c.execute("UPDATE cleaner_runs SET state='running',phase='starting',worker_token=?,lease_expires_at=?,worker_generation=?,started_at=coalesce(started_at,?) WHERE run_id=? AND state='queued'",(token,plus_seconds(now,lease_seconds),generation,now,run["run_id"]))
            self._event(c,"run_claimed",dict(worker_token=token,generation=generation),run_id=run["run_id"])
            return dict(c.execute("SELECT * FROM cleaner_runs WHERE run_id=?",(run["run_id"],)).fetchone())

    def _lease(self,c,run_id,token,generation,*,require_enabled=False) -> sqlite3.Row:
        row=c.execute("SELECT * FROM cleaner_runs WHERE account=? AND run_id=?",(self.key,run_id)).fetchone()
        s=self._settings(c)
        if row is None or row["state"]!="running" or row["worker_token"]!=token or row["worker_generation"]!=generation or s["generation"]!=generation or not row["lease_expires_at"] or timestamp(row["lease_expires_at"])<=timestamp(self.clock()):
            raise CleanerError("lease_lost","Право исполнения задания потеряно",409)
        if require_enabled and not s["enabled"]: raise CleanerError("disabled","Чистка выключена",409)
        return row

    def renew_lease(self,run_id:str,token:str,generation:str,*,phase:str,lease_seconds:int=180) -> None:
        with self.store.transaction() as c:
            self._lease(c,run_id,token,generation)
            c.execute("UPDATE cleaner_runs SET lease_expires_at=?,phase=? WHERE run_id=?",(plus_seconds(self.clock(),lease_seconds),phase,run_id))
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

    def record_snapshot(self,run_id:str,token:str,generation:str,snapshot:Snapshot) -> dict:
        t=snapshot.target;counts=dict(new_checked=0,allow=0,would_exclude=0,review=0,profile_required=0)
        with self.store.transaction() as c:
            self._lease(c,run_id,token,generation)
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
                if c.execute("SELECT 1 FROM cleaner_write_operations WHERE account=? AND target=? AND state IN('dispatching','submitted','unresolved')",(self.key,row["target"])).fetchone(): continue
                p=self._profile(c,row["nm_id"])
                if p is None or p.version!=row["profile_version"] or p.semantic_fingerprint!=row["fingerprint"] or row["rules_version"]!=self._settings(c)["rules_version"]: continue
                override=c.execute("SELECT revision,needs_revalidation FROM cleaner_override_heads WHERE account=? AND nm_id=? AND query_hash=?",(self.key,row["nm_id"],row["query_hash"])).fetchone()
                if override and (override["needs_revalidation"] or row["override_revision"]!=override["revision"]): continue
                if not override and row["override_revision"] is not None: continue
                results.append(dict(row))
            return results

    def finish_run(self,run_id:str,token:str,generation:str,*,reason:str="",remaining:list[Mapping] | None=None) -> dict:
        with self.store.transaction() as c:
            run=self._lease(c,run_id,token,generation)
            s=self._settings(c);state="complete"
            targets=c.execute("SELECT * FROM cleaner_run_targets WHERE run_id=?",(run_id,)).fetchall()
            summary=dict(new_checked=0,allow=0,would_exclude=0,review=0,profile_required=0,confirmed_automatic=0,confirmed_manual=0,pairs=len(targets),campaigns=len({json.loads(t["metadata"])["advert_id"] for t in targets}),dry_run=not bool(run["transport_enabled"]))
            for t in targets:
                for k,v in json.loads(t["counters"]).items():
                    if k in summary: summary[k]+=v
            for event in c.execute("SELECT facts FROM cleaner_events WHERE run_id=? AND kind='readback_result'",(run_id,)):
                counts=json.loads(event[0])
                for field in ('confirmed_automatic','confirmed_manual'): summary[field]+=counts.get(field,0)
            summary['unresolved_operations']=c.execute("SELECT count(*) FROM cleaner_write_operations WHERE run_id=? AND state IN('dispatching','submitted','unresolved')",(run_id,)).fetchone()[0]
            if summary['unresolved_operations']: state='partial'
            if any(t["state"]!="done" for t in targets) or reason or run["reason"]: state="partial"
            if not s["enabled"]: state="stopped"
            if run["kind"]=="scan":
                due=c.execute("SELECT max(due_at) FROM cleaner_schedule_dates WHERE run_id=?",(run_id,)).fetchone()[0]
                available=c.execute("SELECT target FROM cleaner_scan_queue WHERE account=? AND available=1",(self.key,)).fetchall()
                completed={r["target"] for r in targets if not due or (r["observed_at"] and timestamp(r["observed_at"])>=timestamp(due))}
                missing=[r[0] for r in available if r[0] not in completed]
                summary["unread_pairs"]=len(missing)
                if missing: state="partial" if s["enabled"] else "stopped"
            if remaining and run["kind"]=="manual_apply":
                original={(v["target"],v["query_hash"],v["decision_id"]) for v in json.loads(run["targets"])}
                retained=[]
                for item in remaining:
                    identity=(item["target"],item["query_hash"],item["decision_id"])
                    if identity not in original: raise CleanerError("invalid_continuation","Цель не принадлежит исходному заданию",409)
                    obs=c.execute("SELECT state,decision_id FROM cleaner_observations WHERE account=? AND target=? AND query_hash=?",(self.key,item["target"],item["query_hash"])).fetchone()
                    uncertain=c.execute("SELECT 1 FROM cleaner_write_operations WHERE account=? AND target=? AND state IN('dispatching','submitted','unresolved')",(self.key,item["target"])).fetchone()
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

    def queue_readback(self,operation_id:str) -> None:
        with self.store.transaction() as c:
            op=c.execute("SELECT state FROM cleaner_write_operations WHERE account=? AND operation_id=?",(self.key,operation_id)).fetchone()
            if op is None or op["state"] not in {"dispatching","submitted","unresolved"}: raise CleanerError("readback_not_applicable","Нет допущенной операции для сверки",409)
            c.execute("INSERT OR IGNORE INTO cleaner_readback_jobs(operation_id,account) VALUES(?,?)",(operation_id,self.key))

    def claim_readback(self,*,generation:str,lease_seconds:int=90) -> dict | None:
        # Independent of the account scan slot and enabled toggle. No submit
        # authority is returned even after expiry of this read-only lease.
        with self.store.transaction() as c:
            if self._settings(c)["generation"]!=generation: raise CleanerError("generation_conflict","Поколение worker не совпало",409)
            now=self.clock()
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
            profile_required=c.execute("SELECT count(DISTINCT nm_id) FROM cleaner_observations WHERE account=? AND state='profile_required'",(self.key,)).fetchone()[0]
            holds=c.execute("SELECT count(*) FROM cleaner_target_holds WHERE account=?",(self.key,)).fetchone()[0]
            errors=[]
            if s["enabled"] and s["baseline_ready"]:
                if not s["heartbeat_at"] or timestamp(now)-timestamp(s["heartbeat_at"])>timedelta(minutes=5): errors.append("worker_heartbeat_missing")
                local=timestamp(now).astimezone(ZoneInfo(s["timezone"]));h,m=map(int,s["schedule_time"].split(":"));due=local.replace(hour=h,minute=m,second=0,microsecond=0)
                if local>due+timedelta(minutes=5) and not c.execute("SELECT 1 FROM cleaner_schedule_dates WHERE account=? AND local_date=?",(self.key,local.date().isoformat())).fetchone(): errors.append("scheduled_run_overdue")
            if last and last["state"] in {"partial","failed"}: errors.append("last_scan_partial")
            return dict(settings=dict(enabled=bool(s["enabled"]),revision=s["revision"],schedule_time=s["schedule_time"],timezone=s["timezone"],baseline_ready=bool(s["baseline_ready"]),restore_hold=bool(s["restore_hold"])),last_scan=self._run_payload(last),current_work=self._run_payload(active),queued=[self._run_payload(r) for r in queued],pending_count=pending,unresolved_count=unresolved,profile_required_count=profile_required,target_holds=holds,errors=errors,indicator=bool(pending or unresolved or profile_required or holds or errors),coverage=COVERAGE_NOTICE,transport_enabled=bool(s["transport_enabled"]),dry_run=not bool(s["transport_enabled"]),confirmed=self._confirmation_totals(c))

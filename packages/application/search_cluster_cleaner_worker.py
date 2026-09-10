"""Explicitly invoked read-only worker tick; never starts a thread or a timer."""
from __future__ import annotations
import time
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.contracts.search_cluster_cleaner import CleanerError, ReadSource


class CleanerWorker:
    def __init__(self, cleaner: KeywordCleaner, source: ReadSource, *, generation: str,
                 max_seconds: float = 1800, max_targets: int = 10000, monotonic=time.monotonic):
        self.cleaner,self.source,self.generation=cleaner,source,generation
        self.max_seconds,self.max_targets,self.monotonic=max_seconds,max_targets,monotonic

    def tick(self) -> dict | None:
        """Called by a future service dispatcher. No scheduler is installed here."""
        app=self.cleaner;app.heartbeat(generation=self.generation);app.scheduler_tick()
        run=app.claim_run(generation=self.generation)
        if not run: return None
        rid,token=run["run_id"],run["worker_token"]
        start=self.monotonic();attempted=0;reason=""
        if run["kind"]=="manual_apply":
            candidates=app.pending_candidates(rid)
            # B records only the durable candidate set. D adds fresh preflight /
            # one-submit/readback here; no callable WB mutation exists in B.
            with app.store.transaction() as c:
                app._lease(c,rid,token,self.generation)
                app._event(c,"dry_run_manual_candidates",dict(count=len(candidates)),run_id=rid)
            return app.finish_run(rid,token,self.generation)
        try:
            targets,errors=self.source.catalog()  # outside operational transaction
            app.sync_catalog(rid,token,self.generation,targets,errors)
            while self.monotonic()-start<self.max_seconds and attempted<self.max_targets:
                # Acquire today's due obligation even if a manual scan crossed it.
                app.scheduler_tick()
                target=app.next_scan_target(rid,token,self.generation)
                if target is None: break
                app.renew_lease(rid,token,self.generation,phase="reading")
                attempted+=1
                if target.unsupported_reason:
                    app.record_target_error(rid,token,self.generation,target,target.unsupported_reason)
                    continue
                try:
                    snapshot=self.source.snapshot(target)
                    if snapshot.target!=target: raise CleanerError("wrong_target","Ответ относится к другой цели")
                    app.record_snapshot(rid,token,self.generation,snapshot)
                except CleanerError as exc:
                    if exc.code in {"lease_lost","generation_conflict","disabled"}: raise
                    app.record_target_error(rid,token,self.generation,target,exc.code)
                    if exc.code in {"unauthorized","account_mismatch","forbidden"}:
                        reason=exc.code;break
                except (TimeoutError,ConnectionError,OSError):
                    app.record_target_error(rid,token,self.generation,target,"source_temporarily_unavailable",retry_after_seconds=60)
            if (self.monotonic()-start>=self.max_seconds or attempted>=self.max_targets) and app.next_scan_target(rid,token,self.generation) is not None: reason="scan_budget_reached"
            return app.finish_run(rid,token,self.generation,reason=reason)
        except CleanerError as exc:
            if exc.code=="disabled": return app.finish_run(rid,token,self.generation,reason="disabled")
            if exc.code in {"lease_lost","generation_conflict"}: return dict(run_id=rid,state="lease_lost")
            return app.finish_run(rid,token,self.generation,reason=exc.code)
        except (TimeoutError,ConnectionError,OSError):
            return app.finish_run(rid,token,self.generation,reason="catalog_unavailable")

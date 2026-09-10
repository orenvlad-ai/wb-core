"""Setup and read-only UI projections for the existing cleaner service.

No worker, source adapter or background work is created by the web application.
"""
from __future__ import annotations

import os
from pathlib import Path

from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.storage_registry import StoreRegistry
from packages.contracts.search_cluster_cleaner import Account, CleanerError, MODEL_CATALOG, Principal, digest


class CleanerWeb:
    def __init__(self, cleaner: KeywordCleaner | None = None, *, generation: str = "", origin: str = ""):
        self.cleaner = cleaner
        self.generation = generation
        self.origin = origin

    @classmethod
    def from_env(cls, runtime_dir: Path) -> "CleanerWeb":
        # No implicit identity, generation or admission from an old backup.
        seller = os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID", "").strip()
        scope = os.environ.get("CLEANER_ACCOUNT_SCOPE", "").strip()
        generation = os.environ.get("CLEANER_OPERATIONAL_GENERATION", "").strip()
        if not all((seller, scope, generation)):
            return cls()
        cleaner = KeywordCleaner(CleanerStore(StoreRegistry(runtime_dir)), Account(seller, scope),
                                 owner_username=os.environ.get("CLEANER_OWNER_USERNAME", "").strip())
        cleaner.initialize(generation=generation)  # Explicit application setup, never GET.
        return cls(cleaner, generation=generation, origin=os.environ.get("CLEANER_WEB_ORIGIN", "").strip())

    def require_service(self) -> KeywordCleaner:
        if self.cleaner is None:
            raise CleanerError("not_initialized", "Чистка ещё не настроена на сервере", 503)
        return self.cleaner

    def configuration(self, principal: Principal) -> dict:
        principal.require_read()
        cleaner = self.cleaner
        owner = bool(cleaner and cleaner.owner_username.strip())
        can_edit = owner and principal.username.strip().casefold() == cleaner.owner_username.strip().casefold()
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

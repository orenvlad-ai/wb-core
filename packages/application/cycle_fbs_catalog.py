"""One source-only repair before warehouse planning; never replay business effects."""
from packages.application.ready_publication import readonly
from packages.application.stock_catalog_scope import read_stock_catalog_scope, active_catalog_digest
from packages.application.sheet_vitrina_v1_cycle import CycleStageFailure, require_cycle_business_date


class CycleFbsCatalogChanged(CycleStageFailure):
    def __init__(self, reason='catalog_changed_after_preflight'):
        super().__init__('cycle_fbs_catalog_changed')
        self.reason = reason

    def diagnostic(self):
        return {'error_code': str(self), 'source_reason': self.reason,
                'recovery_policy': 'fresh_full_cycle_after_terminal'}


class CycleFbsCatalogGuard:
    def __init__(self, entry, store, receipt, fbs):
        self.entry, self.store, self.receipt, self.fbs = entry, store, receipt, fbs
        self.armed = False

    def _matches(self):
        with readonly(self.entry.runtime.db_path) as conn:
            scope = read_stock_catalog_scope(conn)
        if not scope['complete']:
            raise CycleFbsCatalogChanged('current_catalog_incomplete')
        if not self.fbs.get('fbs_active_scope'):
            raise CycleFbsCatalogChanged('bound_catalog_proof_missing')
        return active_catalog_digest(scope) == self.fbs['fbs_active_scope']

    def check(self, now):
        require_cycle_business_date(self.receipt['business_date'], now)
        if self.armed and not self._matches():
            raise CycleFbsCatalogChanged()

    def before_plan(self):
        self.check(self.entry.now_factory())
        if not self._matches():
            if self.receipt.get('fbs_catalog_refresh'):
                raise CycleFbsCatalogChanged('catalog_refresh_limit')
            evidence = {'status': 'collecting', 'prior_versions': dict(self.fbs)}
            self.receipt['fbs_catalog_refresh'] = evidence
            self.store.write(self.receipt)  # Unknown collection is never replayable.
            proof = self.entry._cycle_fbs_generation()
            require_cycle_business_date(self.receipt['business_date'], self.entry.now_factory())
            if proof.versions.get('fbs_business_date') != self.receipt['business_date']:
                raise CycleFbsCatalogChanged('refreshed_generation_date_mismatch')
            self.fbs.update(proof.versions)
            if not self._matches():
                raise CycleFbsCatalogChanged('catalog_changed_during_refresh')
            evidence.update(status='complete', versions=dict(proof.versions))
            # Original completed stage remains immutable evidence of the first
            # collection. Only the active predecessor binding is superseded.
            self.receipt['final_versions'].update(proof.versions)
            self.store.write(self.receipt)
        self.armed = True
        self.check(self.entry.now_factory())

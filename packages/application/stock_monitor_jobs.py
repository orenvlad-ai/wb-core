"""Persisted monitoring preference and cycle/manual snapshot publication.

Only explicit refresh and the owned three-hour cycle write this namespace.
Reading the screen never loads source history or starts a producer.
"""
from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import threading
from typing import Any

from packages.application.business_data_heavy_admission import heavy_admitted, HeavyAdmissionBusy
from packages.application.web_vitrina_snapshot_admission import _atomic


def period_days(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError('Период должен быть целым положительным числом дней.')
    try:
        parsed = int(str(value))
    except (ValueError, TypeError):
        raise ValueError('Период должен быть целым положительным числом дней.') from None
    if not 1 <= parsed <= 3650:
        raise ValueError('Период должен быть от 1 до 3650 дней.')
    return parsed


class StockMonitorJobs:
    def __init__(self, *, service, operator_jobs):
        self.service = service
        self.operator_jobs = operator_jobs
        self.root = Path(service.runtime.runtime_dir) / 'stock_monitor'
        self._lock = threading.RLock()

    def _market_scope(self):
        # Lightweight legacy/fake services need no market enrichment context.
        scope = getattr(self.service, 'market_refresh_scope', None)
        return scope() if scope is not None else nullcontext()

    def preferred_period(self) -> int:
        try:
            value = json.loads((self.root / 'settings.json').read_text(encoding='utf-8'))
            return period_days(value['period_days'])
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return 14

    def request_refresh(self, value: Any) -> dict:
        days = period_days(value)
        with self._lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic(self.root / 'settings.json', {'period_days': days})
            active = self.operator_jobs.active_job(operations=('stock_monitor_refresh', 'cycle'))
            if active:
                return {'status': 'queued', 'period_days': days,
                        'job_id': active['job_id'],
                        'message': 'Снимок будет обновлён после текущей работы или в следующем трёхчасовом цикле.'}

            def worker(log):
                try:
                    with heavy_admitted(self.service.runtime.runtime_dir, operation='stock_monitor_refresh'), self._market_scope():
                        target = self.preferred_period()
                        result = self.service.refresh_snapshot(period_days=target)
                        # A preference changed while building gets one bounded follow-up.
                        latest = self.preferred_period()
                        if latest != target:
                            result = self.service.refresh_snapshot(period_days=latest)
                        return {'status': 'complete', 'period_days': latest,
                                'snapshot_id': result.get('source_fingerprint', '')}
                except HeavyAdmissionBusy:
                    return {'status': 'queued', 'period_days': self.preferred_period(),
                            'message': 'Пересчёт ожидает следующего общего цикла.'}

            job = self.operator_jobs.start(operation='stock_monitor_refresh', runner=worker)
            return {'status': 'refreshing', 'period_days': days, 'job_id': job['job_id']}

    def refresh_cycle(self) -> dict:
        # Called once at the terminal attempt tail, under the still-live cycle
        # worker heavy/maintenance ownership, including failed core stages.
        outcomes = []
        with self._market_scope():
            for days in sorted({14, self.preferred_period()}):
                try:
                    snapshot = self.service.refresh_snapshot(period_days=days)
                    outcomes.append({'period_days': days, 'status': 'published',
                                     'snapshot_id': snapshot.get('source_fingerprint', ''),
                                     'generated_at': snapshot.get('generated_at', '')})
                except Exception as exc:
                    # Last-good files remain intact. Source and warehouse cycle results
                    # must not be discarded because this derived view could not refresh.
                    outcomes.append({'period_days': days, 'status': 'retained',
                                     'error_code': type(exc).__name__})
        return {'snapshots': outcomes}

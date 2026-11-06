"""Bounded, payload-free stocks failure evidence shared by IPC and journals."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_TYPES = frozenset({
    'Exception', 'RuntimeError', 'ValueError', 'TypeError', 'KeyError', 'AttributeError',
    'AssertionError', 'IndexError', 'OverflowError', 'NameError', 'UnboundLocalError',
    'ZeroDivisionError', 'NotImplementedError', 'OSError', 'URLError', 'HTTPError',
    'JSONDecodeError', 'UnicodeDecodeError', 'TimeoutError', 'ConnectionRefusedError',
    'ConnectionResetError', 'ConnectionAbortedError', 'BrokenPipeError', 'gaierror',
    'SSLError', 'SSLCertVerificationError', 'StocksFetchIncompleteError',
    '_StocksHttpStatusError', '_StocksValidationError',
})
STAGES = frozenset({'initialization', 'rate_limit', 'request', 'body_read', 'decode',
    'validation', 'normalization', 'serialization', 'worker_wait'})
REASONS = frozenset({'http_status', 'transport', 'invalid_json', 'invalid_encoding',
    'program_error', 'deadline_exhausted', 'resource_limit', 'cancelled', 'incomplete',
    'invalid_payload', 'invalid_data', 'invalid_items', 'invalid_item', 'invalid_nm_id',
    'unexpected_nm_id', 'invalid_chrt_id', 'invalid_warehouse_id', 'invalid_quantity',
    'duplicate_row', 'page_limit', 'pagination_limit', 'repeated_page'})


def exception_type(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    name = type(exc).__name__
    return name if name in _TYPES else 'Exception'


def sanitize_stocks_failure(value: Any) -> dict[str, Any]:
    """Exact fields and enums only. Never stringify provider-controlled objects."""
    data = value if isinstance(value, Mapping) else {}
    result = {'schema_version': 1}
    for key in ('origin_type', 'cause_type', 'root_cause_type', 'transport_reason_type'):
        item = data.get(key)
        result[key] = item if type(item) is str and item in _TYPES else None
    for key, allowed, fallback in (('stage', STAGES, 'initialization'),
            ('reason', REASONS, 'program_error')):
        item = data.get(key)
        result[key] = item if type(item) is str and item in allowed else fallback
    for key, low, high in (('http_status', 100, 599), ('errno', -65535, 65535),
            ('tls_verify_code', 0, 65535), ('attempt', 0, 3),
            ('attempt_elapsed_ms', 0, 86_400_000), ('total_elapsed_ms', 0, 86_400_000)):
        item = data.get(key)
        result[key] = item if type(item) is int and low <= item <= high else None
    return result


@dataclass(frozen=True)
class StocksFailureDiagnostic:
    """Typed marker: arbitrary exception attributes are never journal evidence."""
    fields: Mapping[str, Any]

    def diagnostic(self) -> dict[str, Any]:
        return sanitize_stocks_failure(self.fields)


def attach_stocks_failure(exc: BaseException, fields: Mapping[str, Any]) -> None:
    """Diagnostic work must never replace an original failure."""
    try:
        if not isinstance(getattr(exc, '_stocks_failure', None), StocksFailureDiagnostic):
            exc._stocks_failure = StocksFailureDiagnostic(sanitize_stocks_failure(fields))
    except Exception:
        pass


def stocks_failure_diagnostic(exc: BaseException) -> dict[str, Any] | None:
    evidence = getattr(exc, '_stocks_failure', None)
    return evidence.diagnostic() if isinstance(evidence, StocksFailureDiagnostic) else None

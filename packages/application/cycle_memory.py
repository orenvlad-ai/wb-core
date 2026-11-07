"""Release unreachable cycle operands before the owned History child starts."""
from __future__ import annotations

import gc
import sys


def _rss_kib() -> int | None:
    if sys.platform != 'linux':
        return None
    try:
        with open('/proc/self/status', encoding='ascii') as source:
            for line in source.read(65536).splitlines():
                fields = line.split()
                if len(fields) == 3 and fields[0] == 'VmRSS:' and fields[2] == 'kB':
                    return int(fields[1])
    except (OSError, ValueError):
        pass
    return None


def _trim_allocator() -> str:
    if sys.platform != 'linux':
        return 'unsupported_platform'
    try:
        import ctypes
        # Fixed process-library symbols only. The glibc marker excludes other
        # allocators; no configured path, pointer, address or argument is used.
        library = ctypes.CDLL(None)
        getattr(library, 'gnu_get_libc_version')
        trim = library.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return 'released' if trim(0) else 'no_release'
    except (AttributeError, ImportError, OSError):
        return 'unavailable'


def release_cycle_memory() -> dict:
    # Source handles have already been dropped by their owning cycle. Collect
    # cyclic payloads and closed SQLite wrappers too; live evidence and runtime
    # caches remain owned by their callers. Native trim is an optional request
    # to return free allocator pages, never an admission or freshness proof.
    before = _rss_kib()
    result = {'gc_collected': gc.collect(), 'allocator_trim': _trim_allocator()}
    after = _rss_kib()
    if before is not None:
        result['rss_before_kib'] = before
    if after is not None:
        result['rss_after_kib'] = after
    return result

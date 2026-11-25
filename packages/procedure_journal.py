"""Bounded diagnostic events. Business receipts remain authoritative."""
from __future__ import annotations

import atexit
import contextvars
import functools
import json
import os
import re
import socket
import subprocess
import sys
import time
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

_CONTEXT = contextvars.ContextVar("wbc_journal", default=None)
_DELIVERY = threading.local()
_ROOT = Path(__file__).resolve().parents[1]
_ID = re.compile(r"(?:[a-f0-9]{12,64}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|(?:whur|cycle|ffreq|ffpdr)_[a-f0-9]{12,64})\Z")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_COUNTS = {"elapsed_ms", "returncode", "http_status", "count", "warnings", "budget_ms",
           "inner_ms", "before_cleanup_rc", "completed", "pid_child", "attempt", "line", "rss_peak_kib", "errno", "dropped_before"}
_IDS = {"cycle_id", "job_id", "invocation", "warehouse_id", "receipt_id", "proof_sha256"}


def enabled():
    return os.environ.get("WBC_PROCEDURE_LOG") != "0"


def _fields(values):
    result = {}
    for key, value in values.items():
        if key in _COUNTS and type(value) is int:
            result[key] = value
        elif key in _IDS and isinstance(value, str) and _ID.fullmatch(value):
            result[key] = value
        elif key == "error_kind" and value in {"timeout", "memory", "permission", "os", "value", "key", "type", "exit", "exception"}:
            result[key] = value
        elif key == "site" and isinstance(value, str) and re.fullmatch(r"(apps|packages)/[a-zA-Z_0-9/]+\.py", value):
            result[key] = value
        elif key == "day" and isinstance(value, str) and _DAY.fullmatch(value):
            result[key] = value
    return result


@functools.lru_cache(maxsize=1)
def _revision():
    try:
        sha = (_ROOT / ".wb-core-runtime-sha").read_text().strip()
        return {"revision": sha} if re.fullmatch(r"[0-9a-f]{40,64}", sha) else {}
    except (OSError, UnicodeError):
        return {}


def _failure(exc):
    # No exception message, custom type name or arbitrary traceback path.
    category = next((label for cls, label in ((TimeoutError, "timeout"), (MemoryError, "memory"),
        (PermissionError, "permission"), (OSError, "os"), (ValueError, "value"),
        (KeyError, "key"), (TypeError, "type"), (SystemExit, "exit"))
        if isinstance(exc, cls)), "exception")
    fields = {"error_kind": category}
    tb = exc.__traceback__
    while tb:
        filename = Path(tb.tb_frame.f_code.co_filename)
        if filename.is_relative_to(_ROOT):
            relative = filename.relative_to(_ROOT).as_posix()
            if re.fullmatch(r"(apps|packages)/[a-zA-Z_0-9/]+\.py", relative):
                fields.update(site=relative, line=tb.tb_lineno)
        tb = tb.tb_next
    if isinstance(exc, OSError) and type(exc.errno) is int:
        fields["errno"] = exc.errno
    return fields


_channel = None
_writer = None
_guard = threading.Lock()


def _after_fork():
    global _channel, _writer, _guard
    if _channel is not None:
        _channel.close()
    _channel = _writer = None
    _guard = threading.Lock()


def _close_writer():
    if _channel is not None:
        _channel.close()
    if _writer is not None:
        try:
            _writer.wait(timeout=0.25)
        except (OSError, subprocess.TimeoutExpired):
            pass  # EOF already ends the helper, including after an abrupt parent exit.


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
atexit.register(_close_writer)


def _send(record):
    global _channel, _writer
    if sys.platform != "linux":
        return
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(payload) > 4096:
        raise ValueError("diagnostic event too large")
    with _guard:
        if _channel is None:
            # Journal enriches native packets with the sender's full command line.
            # The fixed helper inherits only its private channel, never business locks.
            with _open_pair() as pair:
                sender, reader = pair
                sender.setblocking(False)
                _writer = subprocess.Popen(
                    [sys.executable, "-I", "-S", str(_ROOT / "packages/procedure_journal_writer.py")],
                    stdin=reader, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={}, close_fds=True,
                )
                _channel = sender.dup()
        try:
            _channel.send(payload)
        except (BrokenPipeError, ConnectionResetError):
            _channel.close()
            _channel = None
            _writer.poll()
            raise


@contextmanager
def _open_pair():
    sender, reader = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with sender, reader:
        yield sender, reader


def event(kind, name, state, **values):
    """kind, name and state are code-owned labels, never request or response text."""
    if not enabled():
        return
    try:
        context = _CONTEXT.get() or {}
        lost = getattr(_DELIVERY, "lost", 0)
        _send({"v": 1, **({"dropped_before": lost} if lost else {}), **_revision(), "unix_ms": time.time_ns() // 1_000_000, "pid": os.getpid(),
               **context, "kind": kind, "name": name, "state": state, **_fields(values)})
        if lost:
            _DELIVERY.lost = 0
    except Exception:
        # Do not retry or disturb the operation, even after a commit.
        _DELIVERY.lost = min(getattr(_DELIVERY, "lost", 0) + 1, 2**31 - 1)


@contextmanager
def span(kind, name, *, root=False, **values):
    fields = {}
    if not enabled():
        yield fields
        return
    previous = {} if root else (_CONTEXT.get() or {})
    scope = {**previous, "trace": previous.get("trace") or uuid.uuid4().hex,
             "span": uuid.uuid4().hex, **_fields(values)}
    if previous.get("span"):
        scope["parent"] = previous["span"]
    token = _CONTEXT.set(scope)
    start = time.monotonic_ns()
    try:
        event(kind, name, "started")
        try:
            yield fields
        except BaseException as exc:
            if not isinstance(exc, SystemExit):
                try:
                    fields.update(_failure(exc))
                except Exception:
                    pass
            fields["state"] = "exited" if isinstance(exc, SystemExit) else "raised"
            raise
    finally:
        try:
            state = fields.pop("state", "returned")
            event(kind, name, state, elapsed_ms=(time.monotonic_ns() - start) // 1_000_000, **fields)
        finally:
            _CONTEXT.reset(token)


def bind(fn):
    """Propagate diagnostics only. Application leases never cross this boundary."""
    captured = _CONTEXT.get()
    if not captured:
        return fn

    @functools.wraps(fn)
    def run(*args, **kwargs):
        token = _CONTEXT.set(captured)
        try:
            return fn(*args, **kwargs)
        finally:
            _CONTEXT.reset(token)
    return run


def carrier():
    context = _CONTEXT.get() or {}
    return {k: context[k] for k in ("trace", "span", "cycle_id", "job_id") if k in context}


@contextmanager
def child_scope(parent, mode, invocation):
    # Received only after the History child authenticated its inherited capability.
    parent = parent if isinstance(parent, dict) else {}
    inherited = {k: v for k, v in parent.items() if k in {"trace", "span", "cycle_id", "job_id"}
                 and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v)}
    token = _CONTEXT.set(inherited)
    try:
        with span("history_worker", mode, invocation=invocation):
            yield
    finally:
        _CONTEXT.reset(token)


def worker(fn):
    # Labels come from the function's compiled source, never from thread names or arguments.
    code = getattr(fn, "__code__", None)
    name = "other"
    if code:
        origin = Path(code.co_filename)
        if origin.is_relative_to(_ROOT) and re.fullmatch(r"[a-zA-Z_0-9]+", origin.stem) and re.fullmatch(r"[a-zA-Z_0-9]+", code.co_name):
            name = origin.stem + "." + code.co_name

    @functools.wraps(fn)
    def observed(*args, **kwargs):
        with span("worker", name):
            return fn(*args, **kwargs)
    return bind(observed)


@contextmanager
def cli_scope(name):
    with span("cli", name) as result:
        try:
            yield
        except SystemExit as exc:
            result["returncode"] = exc.code if type(exc.code) is int else (0 if exc.code is None else 1)
            raise
        else:
            result["returncode"] = 0


@functools.lru_cache(maxsize=1)
def _routes():
    path = Path(__file__).resolve().parents[1] / "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json"
    try:
        items = json.loads(path.read_text())["routes"]
        exact = {i["path"]: i["name"] for i in items if i["match"] == "exact"}
        prefixes = sorted(((i["path"], i["name"]) for i in items if i["match"] == "prefix"),
                          key=lambda pair: -len(pair[0]))
        return exact, prefixes
    except (OSError, ValueError, KeyError, TypeError):
        return {}, []


def route_name(path):
    exact, prefixes = _routes()
    path = path.partition("?")[0]
    return exact.get(path) or next((name for prefix, name in prefixes if path.startswith(prefix)), "other")


class JournalHTTPMixin:
    """Wrap the real handler including its exception-to-response handling."""
    def journal_route(self):
        return route_name(self.path)

    def parse_request(self):
        parsed = super().parse_request()
        if parsed and enabled():
            method = self.command if self.command in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
            self._journal_scope = span("http", method + "." + self.journal_route(), root=True)
            self._journal_fields = self._journal_scope.__enter__()
        return parsed

    def send_response(self, code, message=None):
        if getattr(self, "_journal_scope", None) is not None:
            self._journal_fields["http_status"] = int(code)
            exc = sys.exc_info()[1]
            if int(code) >= 400 and exc is not None:
                try:
                    self._journal_fields.update(_failure(exc))
                except Exception:
                    pass
        return super().send_response(code, message)

    def handle_one_request(self):
        self._journal_scope = None
        try:
            return super().handle_one_request()
        except BaseException as exc:
            if self._journal_scope is not None:
                self._journal_scope.__exit__(type(exc), exc, exc.__traceback__)
                self._journal_scope = None
            raise
        finally:
            if self._journal_scope is not None:
                self._journal_scope.__exit__(None, None, None)
                self._journal_scope = None

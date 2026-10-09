"""Read-only bounded operator projection of the SAME native complaint JSON.

Automatic records are fully validated but never materialized. Native automatic
writers, reports, retention and dedup still use the original complete source.
Memory is bounded by one 64KiB chunk, depth/key/token limits and 64MiB retained
projection, independently of physical diagnostic volume. No constructor/repair.
"""
import codecs
import json
import math
import os
import re
import stat
from packages.application.operator_feedback_analysis_settings import safe_path

CHUNK = 64 * 1024
MAX_DEPTH = 64
MAX_KEYS = 4096
MAX_KEY_BYTES = 1024
MAX_NUMBER_BYTES = 128
MAX_VALUE_BYTES = 8 * 1024 * 1024
MAX_PROJECTION_BYTES = 64 * 1024 * 1024
META = 'operator_manual_command'
STRING_SPECIAL = re.compile(b'["\\\\\x00-\x1f]')
NUMBER = re.compile(rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')


def rejected(code='invalid_json'):
    raise ValueError('complaint_source_projection_' + code)


class Scanner:
    def __init__(self, stream, projection_limit=None):
        self.projection_limit = min(MAX_PROJECTION_BYTES, projection_limit if projection_limit is not None else MAX_PROJECTION_BYTES)
        self.stream = stream
        self.buffer = b''
        self.index = 0
        self.offset = 0

    def position(self):
        return self.offset + self.index

    def peek(self):
        if self.index == len(self.buffer):
            self.offset += self.index
            self.buffer = self.stream.read(CHUNK)
            self.index = 0
        return self.buffer[self.index:self.index + 1]

    def take(self):
        char = self.peek()
        if not char:
            rejected('truncated')
        self.index += 1
        return char

    def expect(self, char):
        if self.take() != char:
            rejected()

    def whitespace(self):
        while self.peek() and self.peek() in (b' ', b'\t', b'\r', b'\n'):
            self.index += 1

    def raw(self, start, end, limit):
        if end - start > limit:
            rejected('value_bound')
        data = os.pread(self.stream.fileno(), end - start, start)
        if len(data) != end - start:
            rejected('source_changed')
        try:
            return json.loads(data)
        except (UnicodeError, ValueError) as exc:
            raise ValueError('complaint_source_projection_invalid_json') from exc

    def string(self, key=False):
        start = self.position()
        self.expect(b'"')
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        while True:
            if not self.peek():
                rejected('truncated')
            match = STRING_SPECIAL.search(self.buffer, self.index)
            end = match.start() if match else len(self.buffer)
            decoder.decode(self.buffer[self.index:end], final=False)
            self.index = end
            if key and self.position() - start > MAX_KEY_BYTES:
                rejected('key_bound')
            if match is None:
                continue
            special = self.take()
            decoder.decode(b'', final=True)
            if special == b'"':
                return self.raw(start, self.position(), MAX_KEY_BYTES) if key else None
            if special != b'\\':
                rejected()
            escaped = self.take()
            if escaped == b'u':
                for _ in range(4):
                    if self.take() not in b'0123456789abcdefABCDEF':
                        rejected()
            elif escaped not in (b'"', b'\\', b'/', b'b', b'f', b'n', b'r', b't'):
                rejected()
            decoder = codecs.getincrementaldecoder('utf-8')('strict')

    def value(self, depth=0):
        if depth > MAX_DEPTH:
            rejected('depth_bound')
        self.whitespace()
        char = self.peek()
        if char == b'{':
            self.expect(b'{'); self.whitespace(); keys = set(); manual = False
            if self.peek() == b'}':
                self.take(); return manual
            while True:
                self.whitespace(); key = self.string(key=True)
                if key in keys:
                    rejected('duplicate_key')
                keys.add(key)
                if len(keys) > MAX_KEYS:
                    rejected('key_count_bound')
                manual |= key == META
                self.whitespace(); self.expect(b':'); self.value(depth + 1); self.whitespace()
                delimiter = self.take()
                if delimiter == b'}':
                    return manual
                if delimiter != b',':
                    rejected()
        if char == b'[':
            self.take(); self.whitespace()
            if self.peek() == b']':
                self.take(); return False
            while True:
                self.value(depth + 1); self.whitespace(); delimiter = self.take()
                if delimiter == b']':
                    return False
                if delimiter != b',':
                    rejected()
        elif char == b'"':
            self.string()
        elif char in (b't', b'f', b'n'):
            for expected in {b't': b'true', b'f': b'false', b'n': b'null'}[char]:
                self.expect(bytes([expected]))
        elif char and char in b'-0123456789':
            token = bytearray()
            while self.peek() and self.peek() not in (b' ', b'\t', b'\r', b'\n', b',', b'}', b']'):
                token.extend(self.take())
                if len(token) > MAX_NUMBER_BYTES:
                    rejected('number_bound')
            if not NUMBER.fullmatch(token):
                rejected()
            number = json.loads(token)
            if isinstance(number, float) and not math.isfinite(number):
                rejected('number_bound')
        else:
            rejected()
        return False

    def projection(self):
        self.whitespace(); self.expect(b'{'); self.whitespace()
        result = {}; used = 2
        if self.peek() == b'}':
            self.take()
        else:
            while True:
                self.whitespace(); key = self.string(key=True)
                if key in result:
                    rejected('duplicate_key')
                if len(result) >= MAX_KEYS:
                    rejected('key_count_bound')
                self.whitespace(); self.expect(b':'); self.whitespace()
                if key == 'runs':
                    self.expect(b'['); self.whitespace(); rows = []
                    if self.peek() == b']':
                        self.take()
                    else:
                        while True:
                            self.whitespace(); start = self.position()
                            if self.peek() != b'{':
                                rejected('run_shape')
                            manual = self.value(1)
                            if manual:
                                used += self.position() - start + 1
                                if used > self.projection_limit:
                                    rejected('projection_bound')
                                rows.append(self.raw(start, self.position(), MAX_VALUE_BYTES))
                            self.whitespace(); delimiter = self.take()
                            if delimiter == b']':
                                break
                            if delimiter != b',':
                                rejected()
                    result[key] = rows
                else:
                    start = self.position(); self.value(1)
                    used += self.position() - start + len(key.encode('utf-8')) + 4
                    if used > self.projection_limit:
                        rejected('projection_bound')
                    result[key] = self.raw(start, self.position(), MAX_VALUE_BYTES)
                self.whitespace(); delimiter = self.take()
                if delimiter == b'}':
                    break
                if delimiter != b',':
                    rejected()
        self.whitespace()
        if self.peek():
            rejected('trailing_data')
        return result


def fingerprint(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def during_read():
    """Inert test seam; no writes or repair in this reader."""


def read(path, *, max_projection_bytes=None):
    safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, 'rb') as stream:
        descriptor = os.fstat(fd)
        if not stat.S_ISREG(descriptor.st_mode):
            rejected('source_not_regular')
        before = fingerprint(descriptor)
        value = Scanner(stream, max_projection_bytes).projection()
        during_read()
        try:
            after_path = fingerprint(os.stat(path, follow_symlinks=False))
        except FileNotFoundError:
            rejected('source_changed')
        if before != fingerprint(os.fstat(fd)) or before != after_path:
            rejected('source_changed')
        return value

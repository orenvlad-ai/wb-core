"""Stable identity of the loaded cleaner rules, independent of .pyc warm-up.

`marshal.dumps(code)` is unsuitable for a persisted guard: Python may encode
equivalent code objects differently when loading source and cached bytecode.
Serialize execution-relevant code fields and unordered constants canonically;
source paths, line tables and marshal's object-reference memo are excluded.
"""

from __future__ import annotations

import hashlib
import math
import types
from typing import Any

from packages.contracts.search_cluster_cleaner import MODEL_CATALOG, Profile, canonical, query_hash
from packages.domain import search_cluster_classifier as rules


def _value(value: Any) -> Any:
    if isinstance(value, types.CodeType):
        return {
            "type": "code",
            "argcount": value.co_argcount,
            "posonlyargcount": value.co_posonlyargcount,
            "kwonlyargcount": value.co_kwonlyargcount,
            "nlocals": value.co_nlocals,
            "stacksize": value.co_stacksize,
            "flags": value.co_flags,
            "code": value.co_code.hex(),
            "consts": [_value(item) for item in value.co_consts],
            "names": list(value.co_names),
            "varnames": list(value.co_varnames),
            "freevars": list(value.co_freevars),
            "cellvars": list(value.co_cellvars),
            "exceptiontable": value.co_exceptiontable.hex(),
        }
    if value is None or value is Ellipsis:
        return {"type": "none" if value is None else "ellipsis"}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value.hex() if math.isfinite(value) else repr(value)}
    if isinstance(value, complex):
        return {"type": "complex", "real": _value(value.real), "imag": _value(value.imag)}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [_value(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        items = [_value(item) for item in value]
        return {"type": "frozenset" if isinstance(value, frozenset) else "set",
                "items": sorted(items, key=canonical)}
    if isinstance(value, dict):
        items = [(_value(key), _value(item)) for key, item in value.items()]
        return {"type": "dict", "items": sorted(items, key=lambda pair: canonical(pair[0]))}
    raise TypeError(f"unsupported rule constant: {type(value).__name__}")


def executable_rules_digest(classifier=rules.classify) -> str:
    executable = (
        classifier.__code__, rules.norm.__code__, rules.models.__code__,
        Profile.parse.__func__.__code__, query_hash.__code__,
        rules.BRANDS, rules.PRODUCT, rules.VOCAB, rules.BROAD_WORDS,
        tuple(sorted(MODEL_CATALOG)),
    )
    payload = {"schema": "cleaner-executable-rules/v2", "executable": _value(executable)}
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()

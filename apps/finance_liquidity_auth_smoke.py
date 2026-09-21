#!/usr/bin/env python3
"""Canonical-session and pinned operational-store checks for Finance auth."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.finance_liquidity_auth import (
    FinanceAuthDenied,
    FinanceAuthUnavailable,
    FinanceOperationalAuth,
)
from packages.adapters.registry_upload_http_entrypoint import _build_session_cookie
from packages.application.storage_registry import atomic_write_manifest, build_manifest


class FixtureHandler:
    headers = {"Host": "127.0.0.1"}


def session_cookie(
    secret: str,
    *,
    username: str = "operator",
    role: str = "operator",
    max_age: int = 60,
) -> str:
    header = _build_session_cookie(
        FixtureHandler(),
        username,
        {"session_secret": secret, "max_age": max_age},
        role=role,
        display_name=f"Fixture {role}",
    )
    return header.split(";", 1)[0]


def expect(error_type: type[Exception], action: object) -> None:
    try:
        action()  # type: ignore[operator]
    except error_type:
        return
    raise AssertionError(f"expected {error_type.__name__}")


def make_operational_store(runtime: Path) -> Path:
    db_path = runtime / "registry_upload_runtime.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sheet_vitrina_v1_users("
            "username TEXT PRIMARY KEY, role TEXT NOT NULL, "
            "allowed_sections_json TEXT NOT NULL, is_active INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_users VALUES(?,?,?,?)",
            ("operator", "operator", '["finance_operate"]', 1),
        )
    return db_path


def make_split_store(runtime: Path) -> None:
    epoch, fingerprint = "epoch-fixture", "f" * 64
    manifest = build_manifest(
        state="cutover",
        canonical_source="split",
        generation_epoch=epoch,
        raw_generation_id="raw-fixture",
        raw_relative_path="raw.sqlite3",
        raw_watermark="0",
        operational_generation_id="op-fixture",
        operational_relative_path="op.sqlite3",
        operational_watermark="0",
        rollback_generation_id="op-old",
        source_fingerprint=fingerprint,
    )
    for path, table, store, generation, revision in (
        (
            runtime / "raw.sqlite3",
            "finance_raw_schema_meta",
            "finance_raw",
            "raw-fixture",
            "finance_raw_v1",
        ),
        (
            runtime / "op.sqlite3",
            "finance_operational_schema_meta",
            "operational",
            "op-fixture",
            "operational_v1",
        ),
    ):
        with sqlite3.connect(path) as conn:
            conn.execute(
                f"CREATE TABLE {table}(singleton INTEGER PRIMARY KEY,schema_revision TEXT,logical_store TEXT,generation_id TEXT,generation_epoch TEXT,source_fingerprint TEXT)"
            )
            conn.execute(
                f"INSERT INTO {table} VALUES(?,?,?,?,?,?)",
                (1, revision, store, generation, epoch, fingerprint),
            )
            if store == "operational":
                conn.execute(
                    "CREATE TABLE sheet_vitrina_v1_users(username TEXT PRIMARY KEY,role TEXT,allowed_sections_json TEXT,is_active INTEGER)"
                )
                conn.execute(
                    "INSERT INTO sheet_vitrina_v1_users VALUES('operator','operator','[\"finance\"]',1)"
                )
    atomic_write_manifest(runtime / "storage_generation_manifest.json", manifest)


def main() -> None:
    with TemporaryDirectory() as directory:
        runtime = Path(directory)
        db_path = make_operational_store(runtime)
        secret = "fixture-secret"
        headers = {"Cookie": session_cookie(secret)}
        auth = FinanceOperationalAuth(runtime, session_secret=secret)
        principal = auth.authenticate(headers)
        assert principal["capabilities"] == ["finance", "finance_operate"]

        # Current grants are reread: a revoked user cannot retain a prior decision.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_users SET is_active=0 WHERE username='operator'"
            )
        expect(FinanceAuthDenied, lambda: auth.authenticate(headers))

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_users SET is_active=1 WHERE username='operator'"
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_users VALUES(?,?,?,?)",
                ("supplier", "supplier", '["finance_admin"]', 1),
            )

        expect(
            FinanceAuthDenied,
            lambda: auth.authenticate(
                {"Cookie": session_cookie(secret, username="supplier", role="supplier")}
            ),
        )
        expect(
            FinanceAuthDenied,
            lambda: auth.authenticate({"Cookie": headers["Cookie"] + "x"}),
        )
        expect(
            FinanceAuthDenied,
            lambda: auth.authenticate({"Cookie": session_cookie(secret, max_age=-1)}),
        )
        # A lock/read failure is local to Finance authorization and fails closed.
        locker = sqlite3.connect(db_path, timeout=0.01)
        locker.execute("BEGIN EXCLUSIVE")
        ordinary_connect = auth.registry.connect

        def short_auth_read(*args: object, **kwargs: object):
            return ordinary_connect(*args, timeout_ms=10, **kwargs)

        auth.registry.connect = short_auth_read  # type: ignore[method-assign]
        try:
            expect(FinanceAuthUnavailable, lambda: auth.authenticate(headers))
        finally:
            locker.rollback()
            locker.close()
            auth.registry.connect = ordinary_connect  # type: ignore[method-assign]

        # Replacing the selected path before post-read revalidation is not a
        # reason to reuse old grants. This proves the path identity guard; it
        # intentionally does not claim fstat-level opened-descriptor evidence.
        original_connect = auth.registry.connect

        @contextmanager
        def replace_after_read(*args: object, **kwargs: object):
            with original_connect(*args, **kwargs) as connection:
                yield connection
            replacement = runtime / "replacement.sqlite3"
            shutil.copyfile(db_path, replacement)
            replacement.replace(db_path)

        auth.registry.connect = replace_after_read  # type: ignore[method-assign]
        expect(FinanceAuthUnavailable, lambda: auth.authenticate(headers))
    with TemporaryDirectory() as directory:
        runtime = Path(directory)
        make_split_store(runtime)
        secret, headers = "fixture-secret", {"Cookie": session_cookie("fixture-secret")}
        auth = FinanceOperationalAuth(runtime, session_secret=secret)
        assert auth.authenticate(headers)["capabilities"] == ["finance"]
        with sqlite3.connect(runtime / "op.sqlite3") as conn:
            conn.execute("UPDATE sheet_vitrina_v1_users SET allowed_sections_json='[]'")
        assert auth.authenticate(headers)["capabilities"] == []
        (runtime / "op.sqlite3").unlink()
        expect(FinanceAuthUnavailable, lambda: auth.authenticate(headers))
        (runtime / "storage_generation_manifest.json").write_text(
            "{broken", encoding="utf-8"
        )
        expect(FinanceAuthUnavailable, lambda: auth.authenticate(headers))
        expect(FinanceAuthDenied, lambda: auth.authenticate({"X-User": "operator"}))
    print("finance_liquidity_auth_smoke: ok")


if __name__ == "__main__":
    main()

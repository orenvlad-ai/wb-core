"""Isolated tests for refresh persistence, scope binding and process races."""

from __future__ import annotations

import multiprocessing
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.webcore_oauth_refresh import (  # noqa: E402
    REFRESH_TOKEN_TTL_SECONDS, RefreshStorageError, RefreshTokenError, RefreshTokenStore,
)


def _race_worker(path: str, token: str, gate, result) -> None:
    store = RefreshTokenStore(Path(path), "generation-a", now=lambda: 1000)
    gate.wait(10)
    try:
        grant = store.rotate(token, client_id="client", resource="https://resource",
                             allowed_scopes=("read", "cash"), expected_subject="owner")
        result.put(("ok", grant.refresh_token))
    except RefreshTokenError as exc:
        result.put((exc.code, ""))


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="oauth-refresh-smoke-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / "private" / "tokens.sqlite3"
        self.clock = [1000]
        self.store = RefreshTokenStore(self.path, "generation-a", now=lambda: self.clock[0])

    def mint(self, scopes=("read", "cash")):
        return self.store.mint(subject="owner", client_id="client",
                               resource="https://resource", scopes=scopes)

    def rotate(self, token, **changes):
        args = dict(client_id="client", resource="https://resource",
                    allowed_scopes=("read", "cash"), expected_subject="owner")
        args.update(changes)
        return self.store.rotate(token, **args)

    def assert_error(self, code, call):
        with self.assertRaises(RefreshTokenError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_hash_only_private_storage_and_restart(self):
        first = self.mint()
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(first.refresh_token, repr(first))
        self.assertNotIn(first.refresh_token.encode(), self.path.read_bytes())
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            value = conn.execute("SELECT token_hash FROM refresh_tokens").fetchone()[0]
            self.assertEqual(len(value), 64)
        restarted = RefreshTokenStore(self.path, "generation-a", now=lambda: 1001)
        second = restarted.rotate(first.refresh_token, client_id="client", resource="https://resource",
                                  allowed_scopes=("read", "cash"), expected_subject="owner")
        self.assertNotEqual(first.refresh_token, second.refresh_token)
        self.assertEqual(first.expires_at, second.expires_at)
        self.assertNotIn(second.refresh_token.encode(), self.path.read_bytes())

    def test_absolute_expiry_and_pruning(self):
        first = self.mint()
        self.clock[0] = first.expires_at - 1
        second = self.rotate(first.refresh_token)
        self.assertEqual(second.expires_at, 1000 + REFRESH_TOKEN_TTL_SECONDS)
        self.clock[0] += 1
        self.assert_error("invalid_grant", lambda: self.rotate(second.refresh_token))
        # Successful mint commits pruning even when an invalid refresh rolled back.
        self.mint()
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM refresh_families").fetchone()[0], 1)

    def test_replay_revokes_family_only(self):
        first, unrelated = self.mint(), self.mint()
        successor = self.rotate(first.refresh_token)
        self.assert_error("invalid_grant", lambda: self.rotate(first.refresh_token))
        self.assert_error("invalid_grant", lambda: self.rotate(successor.refresh_token))
        self.rotate(unrelated.refresh_token)

    def test_wrong_bindings_do_not_consume_or_revoke(self):
        first = self.mint()
        for changes in ({"client_id": "other"}, {"resource": "https://other"},
                        {"expected_subject": "other"}):
            self.assert_error("invalid_grant", lambda c=changes: self.rotate(first.refresh_token, **c))
        successor = self.rotate(first.refresh_token)
        self.assert_error("invalid_grant", lambda: self.rotate(first.refresh_token, client_id="other"))
        self.rotate(successor.refresh_token)

    def test_scopes_cannot_expand_and_can_narrow(self):
        first = self.mint(("read",))
        self.assert_error("invalid_scope", lambda: self.rotate(first.refresh_token, requested_scopes=("cash",)))
        self.assert_error("invalid_scope", lambda: self.rotate(first.refresh_token, requested_scopes=()))
        self.assert_error("invalid_scope", lambda: self.rotate(first.refresh_token, requested_scopes="read"))
        second = self.rotate(first.refresh_token, requested_scopes=("read",))
        self.assertEqual(second.scopes, ("read",))
        self.assert_error("invalid_scope", lambda: self.rotate(second.refresh_token, requested_scopes=("read", "cash")))
        third = self.mint()
        fourth = self.rotate(third.refresh_token, allowed_scopes=("read",))
        self.assertEqual(fourth.scopes, ("read",))
        self.assert_error("invalid_scope", lambda: self.rotate(fourth.refresh_token, requested_scopes=("cash",)))

    def test_unknown_token_and_manual_revocation(self):
        first, other = self.mint(), self.mint()
        unknown = "wr1." + "a" * 64
        self.assert_error("invalid_grant", lambda: self.rotate(unknown))
        self.store.revoke(unknown)
        self.store.revoke("invalid")
        successor = self.rotate(first.refresh_token)
        self.store.revoke(first.refresh_token)
        self.store.revoke(first.refresh_token)
        self.assert_error("invalid_grant", lambda: self.rotate(successor.refresh_token))
        self.rotate(other.refresh_token)

    def test_signing_generation_change_and_subject_change(self):
        first = self.mint()
        changed = RefreshTokenStore(self.path, "generation-b", now=lambda: 1000)
        self.assert_error("invalid_grant", lambda: changed.rotate(first.refresh_token,
                          client_id="client", resource="https://resource",
                          allowed_scopes=("read",), expected_subject="owner"))
        self.assert_error("invalid_grant", lambda: self.rotate(first.refresh_token, expected_subject="new-owner"))

    def test_unsafe_existing_permissions_and_symlinks_rejected(self):
        self.path.parent.mkdir(mode=0o755)
        self.path.parent.chmod(0o755)
        with self.assertRaises(RefreshStorageError):
            self.mint()
        self.assertFalse(self.path.exists())
        self.path.parent.chmod(0o700)
        self.mint()
        self.path.chmod(0o644)
        with self.assertRaises(RefreshStorageError):
            self.mint()
        self.path.chmod(0o600)
        alias = self.path.parent / "alias.sqlite3"
        alias.symlink_to(self.path)
        with self.assertRaises(RefreshStorageError):
            RefreshTokenStore(alias, "generation-a").mint(subject="owner", client_id="client",
                                                       resource="https://resource", scopes=("read",))

    def test_crossprocess_rotation_single_use(self):
        first = self.mint()
        ctx = multiprocessing.get_context("spawn")
        gate, results = ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=_race_worker, args=(str(self.path), first.refresh_token, gate, results)) for _ in range(2)]
        for worker in workers:
            worker.start()
        gate.set()
        received = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join()
            self.assertEqual(worker.exitcode, 0)
        results.close()
        self.assertEqual(sorted(row[0] for row in received), ["invalid_grant", "ok"])
        winner = next(row[1] for row in received if row[0] == "ok")
        self.assert_error("invalid_grant", lambda: self.rotate(winner))


if __name__ == "__main__":
    unittest.main(verbosity=2)

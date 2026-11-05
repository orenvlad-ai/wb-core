"""Fixture-only regression checks for opt-in cloud data and OAuth boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from urllib import error as urllib_error

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.webcore_data_mcp_server import (
    AuthIdentity, BoundedToolExecutor, DEFAULT_SCOPES, WebCoreDataMcpServerConfig,
    _authorize, _handle_json_rpc, _make_access_token, _validated_scope,
    _pkce_s256_challenge, _store_authorization_code, build_server,
)
from apps.webcore_data_mcp_smoke import _get_json, _post_form, _post_json
from packages.application.finance_liquidity_cash import bootstrap_finance_cash_store
from packages.application.max_webhook_inbox import MaxWebhookConfig, parse_inbox_event, persist_inbox_event
from packages.application.webcore_cloud_reads import (
    SCOPE_TRANSIT_READ, TRANSIT_GROUP_ID, CLOUD_READ_SCOPES, CloudReadError, MaxInboxReadConfig, SCOPE_CASH_READ,
    SCOPE_MAX_INBOX_READ, WebCoreCloudReads,
)
from packages.application.webcore_data_mcp import WebCoreDataMcpError, WebCoreDataMcpGateway


class CloudReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.inbox = self.root / "inbox"
        self.inbox.mkdir(mode=0o700)
        self.webhook = MaxWebhookConfig("x" * 32, 99, frozenset({-100, -200}), self.inbox)
        self.cash_path = self.root / "cash.sqlite3"
        bootstrap_finance_cash_store(self.cash_path)
        self.access = self.root / "finance-access.json"
        self.access.write_text(json.dumps(dict(
            contract_version="finance_liquidity_bootstrap_access_v1", enabled=True,
            username="owner", capability="finance_admin", instance_label="TEST pilot",
            store_id="fixture-pilot", store_path=str(self.cash_path), mode="isolated_test")))
        self.env = dict(
            WEBCORE_DATA_MCP_MAX_INBOX_READ_ENABLED="1",
            WEBCORE_DATA_MCP_MAX_INBOX_DB_PATH=str(self.inbox / "max_inbox.sqlite3"),
            WEBCORE_DATA_MCP_MAX_INBOX_CHAT_IDS="-100",
            WEBCORE_DATA_MCP_CASH_READ_ENABLED="1",
            FINANCE_LIQUIDITY_ACCESS_CONFIG=str(self.access),
        )
        self.reads = WebCoreCloudReads.from_env(self.env)
        self.gateway = WebCoreDataMcpGateway(db_path=self.root / "absent-business.sqlite3", cloud_reads=self.reads)
        self.config = WebCoreDataMcpServerConfig(
            host="127.0.0.1", port=0, mcp_path="/mcp", health_path="/healthz", auth_mode="bearer_oauth",
            bearer_token="legacy-token", bearer_token_sha256="", runtime_dir=self.root, db_path=None,
            audit_log_path=None, resource_url="https://mcp.example.test", resource_documentation_url="",
            authorization_servers=("https://mcp.example.test",), scopes=(*DEFAULT_SCOPES, *sorted(CLOUD_READ_SCOPES)),
            oauth_signing_secret="x" * 48, oauth_owner_username="owner", oauth_owner_password_hash="fixture",
            oauth_session_secret="fixture", oauth_code_store_path=self.root / "codes.json",
            oauth_allowed_redirect_prefixes=("https://chatgpt.com/connector/oauth/",),
            oauth_allowed_client_id_prefixes=("https://chatgpt.com/oauth/",), oauth_code_ttl_seconds=300,
            oauth_access_token_ttl_seconds=3600, oauth_issuer="https://mcp.example.test",
        )
        self.executor = BoundedToolExecutor(2)

    def tearDown(self) -> None:
        self.executor.close()
        self.tmp.cleanup()

    def event(self, kind: str, mid: str, *, chat: int = -100, text: str = "hello") -> None:
        payload = dict(update_type=kind, timestamp=1000)
        if kind == "message_removed":
            payload.update(chat_id=chat, message_id=mid)
        else:
            payload["message"] = dict(recipient=dict(chat_id=chat, chat_type="chat"),
                                      body=dict(mid=mid, text=text))
        event = parse_inbox_event(json.dumps(payload).encode(), self.webhook.chat_ids)
        persist_inbox_event(self.webhook, event)

    def rpc(self, identity: AuthIdentity, method: str, **params):
        return _handle_json_rpc(dict(jsonrpc="2.0", id=1, method=method, params=params),
                                self.gateway, identity, self.executor, self.config)

    def test_default_off_and_independent_allowlist(self):
        disabled = WebCoreDataMcpGateway(cloud_reads=WebCoreCloudReads.from_env({}))
        self.assertFalse(set(CLOUD_READ_SCOPES) & set(DEFAULT_SCOPES))
        self.assertNotIn("cash_accounts", {tool["name"] for tool in disabled.list_tools()})
        with self.assertRaises(WebCoreDataMcpError) as error:
            disabled.call_tool("max_inbox_events", {"chat_id": -100})
        self.assertEqual(error.exception.code, "source_not_enabled")
        env = {**self.env, "WB_CORE_MAX_WEBHOOK_CHAT_IDS": "-100"}
        del env["WEBCORE_DATA_MCP_MAX_INBOX_CHAT_IDS"]
        with self.assertRaises(ValueError):
            WebCoreCloudReads.from_env(env)

    def test_config_rejects_alias_partial_real_and_invalid_groups(self):
        for key, value in (
            ("WEBCORE_DATA_MCP_MAX_INBOX_DB_PATH", "relative/max_inbox.sqlite3"),
            ("WEBCORE_DATA_MCP_MAX_INBOX_DB_PATH", str(self.root / "other.sqlite3")),
            ("WEBCORE_DATA_MCP_MAX_INBOX_DB_PATH", str(self.inbox / ".." / "max_inbox.sqlite3")),
            ("WEBCORE_DATA_MCP_MAX_INBOX_CHAT_IDS", "-100,2"),
            ("WEBCORE_DATA_MCP_MAX_INBOX_CHAT_IDS", "-9223372036854775809"),
            ("WEBCORE_DATA_MCP_CASH_READ_ENABLED", "true"),
            ("FINANCE_LIQUIDITY_ACCESS_CONFIG", ""),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                WebCoreCloudReads.from_env({**self.env, key: value})
        alias = self.root / "alias"
        alias.symlink_to(self.inbox, target_is_directory=True)
        with self.assertRaises(ValueError):
            MaxInboxReadConfig(alias / "max_inbox.sqlite3", frozenset({-100}))
        payload = json.loads(self.access.read_text())
        payload["mode"] = "live"
        self.access.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):
            WebCoreCloudReads.from_env(self.env)

    def test_scope_discovery_and_static_bearer_do_not_expand(self):
        old = AuthIdentity("old", DEFAULT_SCOPES, "oauth")
        names = {tool["name"] for tool in self.rpc(old, "tools/list")["result"]["tools"]}
        self.assertNotIn("max_inbox_events", names)
        self.assertNotIn("cash_accounts", names)
        for tool in ("max_inbox_events", "max_inbox_event", "cash_accounts"):
            response = self.rpc(old, "tools/call", name=tool)
            self.assertEqual(response["error"]["data"]["code"], "insufficient_scope")
        token = _make_access_token(self.config, subject="owner", client_id="fixture", scopes=(SCOPE_MAX_INBOX_READ,))
        identity = _authorize(SimpleNamespace(headers={"Authorization": "Bearer " + token}), self.config)
        tools = self.rpc(identity, "tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["max_inbox_events", "max_inbox_event"])
        self.assertEqual(tools[0]["inputSchema"]["properties"]["chat_id"]["enum"], [-100])
        self.assertEqual(tools[0]["securitySchemes"], [{"type": "oauth2", "scopes": [SCOPE_MAX_INBOX_READ]}])
        bearer = _authorize(SimpleNamespace(headers={"Authorization": "Bearer legacy-token"}), self.config)
        self.assertFalse(CLOUD_READ_SCOPES & set(bearer.scopes))
        forged = AuthIdentity("bearer", tuple(CLOUD_READ_SCOPES), "bearer")
        self.assertEqual(self.rpc(forged, "tools/list")["result"]["tools"], [])
        self.assertEqual(self.rpc(forged, "tools/call", name="cash_accounts")["error"]["data"]["code"], "insufficient_scope")
        self.assertEqual(_validated_scope("", self.config)["scopes"], DEFAULT_SCOPES)
        self.assertEqual(_validated_scope(SCOPE_CASH_READ, self.config)["scopes"], (SCOPE_CASH_READ,))

    def test_transit_scope_reads_only_its_group_and_attachment(self):
        from unittest.mock import patch

        identity = AuthIdentity("logistics", (SCOPE_TRANSIT_READ,), "oauth")
        self.assertEqual(self.rpc(identity, "tools/list")["result"]["tools"], [])
        self.gateway.cloud_reads = WebCoreCloudReads.from_env(dict(
            self.env, WEBCORE_DATA_MCP_TRANSIT_HISTORY_ENABLED="1",
            WEBCORE_DATA_MCP_MAX_TOKEN_PATH=str(self.root / "test-token"),
        ))
        tools = self.rpc(identity, "tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"transit_message", "transit_messages"})
        for tool in tools:
            self.assertNotIn("chat_id", tool["inputSchema"]["properties"])
        with self.assertRaises(CloudReadError):
            self.gateway.cloud_reads.call_tool("transit_messages", {"chat_id": -100})
        message = {"recipient": {"chat_id": TRANSIT_GROUP_ID, "chat_type": "chat"},
                   "body": {"mid": "test", "text": "Shipment", "attachments": [
                       {"type": "file", "filename": "invoice.pdf", "payload": {"url": "https://example.org/file"}}]}}
        with patch.object(self.gateway.cloud_reads.transit_api, "_get", return_value=[message]):
            result = self.gateway.cloud_reads.call_tool("transit_message", {"message_id": "test"})
            self.assertEqual(result["rows"][0]["attachments"][0]["filename"], "invoice.pdf")
            message["recipient"]["chat_id"] = -100
            with self.assertRaises(CloudReadError):
                self.gateway.cloud_reads.call_tool("transit_message", {"message_id": "test"})
        self.assertEqual(self.rpc(identity, "tools/call", name="max_posted_reply")["error"]["data"]["code"],
                         "insufficient_scope")

    def test_max_pagination_preserves_edits_deletes_and_snapshot(self):
        self.event("message_created", "one")
        self.event("message_edited", "one", text="edited")
        self.event("message_removed", "one")
        self.event("message_created", "foreign", chat=-200)
        first = self.gateway.call_tool("max_inbox_events", dict(chat_id=-100, limit=1))
        self.assertEqual(first["rows"][0]["update_type"], "message_removed")
        self.event("message_created", "new-arrival")
        second = self.gateway.call_tool("max_inbox_events", dict(chat_id=-100, limit=1, cursor=first["next_cursor"]))
        third = self.gateway.call_tool("max_inbox_events", dict(chat_id=-100, limit=1, cursor=second["next_cursor"]))
        self.assertEqual(second["rows"][0]["message_text"], "edited")
        self.assertEqual(third["rows"][0]["update_type"], "message_created")
        self.assertIsNone(third["next_cursor"])
        items = [page["rows"][0] for page in (first, second, third)]
        self.assertEqual(len({item["event_key"] for item in items}), 3)
        self.assertEqual({item["message_id"] for item in items}, {"one"})
        fresh = self.gateway.call_tool("max_inbox_events", dict(chat_id=-100))
        self.assertEqual(fresh["rows"][0]["message_id"], "new-arrival")

    def test_max_rejects_foreign_group_and_arbitrary_arguments(self):
        self.event("message_created", "one")
        for args, code in ((dict(chat_id=-200), "group_not_authorized"),
                           (dict(chat_id=True), "group_not_authorized"),
                           (dict(chat_id="-100"), "group_not_authorized"),
                           (dict(chat_id=-100, db_path="/etc/passwd"), "invalid_arguments"),
                           (dict(chat_id=-100, sql="SELECT 1"), "invalid_arguments"),
                           (dict(chat_id=-100, cursor="bad"), "invalid_arguments"),
                           (dict(chat_id=-100, limit=101), "invalid_arguments")):
            with self.subTest(args=args), self.assertRaises(WebCoreDataMcpError) as error:
                self.gateway.call_tool("max_inbox_events", args)
            self.assertEqual(error.exception.code, code)

    def test_cash_accounts_only_no_balances_and_paginated(self):
        before = hashlib.sha256(self.cash_path.read_bytes()).digest()
        first = self.gateway.call_tool("cash_accounts", {"limit": 2})
        second = self.gateway.call_tool("cash_accounts", {"limit": 2, "cursor": first["next_cursor"]})
        rows = first["rows"] + second["rows"]
        self.assertEqual(len(rows), 3)
        self.assertIn("cash_vladislav", {row["account_id"] for row in rows})
        self.assertEqual(first["store_class"], "TEST")
        self.assertEqual(first["store_mode"], "isolated_test")
        self.assertEqual(first["store_id"], "fixture-pilot")
        for row in rows:
            self.assertEqual(set(row), {"account_id", "name", "account_type", "currency", "currency_exponent", "is_active"})
        self.assertFalse(first["balances_returned"])
        self.assertFalse(first["transactions_returned"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(before, hashlib.sha256(self.cash_path.read_bytes()).digest())
        for args in ({"account_id": "cash_vladislav"}, {"store_path": "/other"}, {"cursor": "bad"}):
            with self.assertRaises(WebCoreDataMcpError):
                self.gateway.call_tool("cash_accounts", args)

    def test_max_event_detail_reconstructs_complete_long_text(self):
        text = "Оплата поставщику: 12500 рублей за товар, платёж подтверждён.\n" * 40
        self.event("message_created", "long-payment", text=text)
        preview = self.gateway.call_tool("max_inbox_events", {"chat_id": -100})["rows"][0]
        self.assertTrue(preview["message_text_truncated"])
        result = self.gateway.call_tool("max_inbox_event", dict(chat_id=-100, event_key=preview["event_key"]))
        event = result["event"]
        self.assertEqual(event["event_key"], preview["event_key"])
        self.assertEqual(event["message_id"], "long-payment")
        self.assertEqual(event["chat_id"], -100)
        self.assertTrue(event["message_text_complete"])
        self.assertFalse(event["message_text_too_long"])
        self.assertEqual("".join(event["message_text_chunks"]), text)
        self.assertTrue(all(len(chunk) <= 500 for chunk in event["message_text_chunks"]))
        self.assertFalse(result["raw_payloads_returned"])
        self.assertFalse(result["mutations"])

    def test_max_event_detail_requires_exact_identity_and_group(self):
        self.event("message_created", "local")
        self.event("message_created", "foreign", chat=-200)
        with self.reads._connect(self.inbox / "max_inbox.sqlite3", None, None) as conn:
            foreign_key = conn.execute("SELECT event_key FROM max_inbox_events WHERE chat_id=-200").fetchone()[0]
        for args, code in (
            (dict(chat_id=-200, event_key=foreign_key), "group_not_authorized"),
            (dict(chat_id=-100, event_key=foreign_key), "event_not_found"),
            (dict(chat_id=-100, event_key="0" * 64), "event_not_found"),
            (dict(chat_id=-100, event_key="../other"), "invalid_arguments"),
            (dict(chat_id=-100, event_key=foreign_key, db_path="/other"), "invalid_arguments"),
        ):
            with self.subTest(args=args), self.assertRaises(WebCoreDataMcpError) as error:
                self.gateway.call_tool("max_inbox_event", args)
            self.assertEqual(error.exception.code, code)

    def test_max_event_detail_bounds_and_cross_chunk_secret_redaction(self):
        # The credential marker crosses the 500-character boundary. Checking
        # individual chunks alone would miss it and disclose the credential.
        secret_text = "x" * 498 + "token=private-secret" + "x" * 700
        self.event("message_created", "secret-detail", text=secret_text)
        key = self.gateway.call_tool("max_inbox_events", {"chat_id": -100})["rows"][0]["event_key"]
        event = self.gateway.call_tool("max_inbox_event", dict(chat_id=-100, event_key=key))["event"]
        self.assertEqual(event["message_text_chunks"], [])
        self.assertTrue(event["message_text_redacted"])
        self.assertFalse(event["message_text_complete"])
        self.assertNotIn("private-secret", json.dumps(event))
        for size in (16000, 16001):
            self.event("message_created", f"size-{size}", text="x" * size)
            key = self.gateway.call_tool("max_inbox_events", {"chat_id": -100})["rows"][0]["event_key"]
            event = self.gateway.call_tool("max_inbox_event", dict(chat_id=-100, event_key=key))["event"]
            self.assertEqual(event["message_text_chars"], size)
            self.assertEqual(event["message_text_complete"], size <= 16000)
            self.assertEqual(event["message_text_too_long"], size > 16000)
            self.assertEqual(len("".join(event["message_text_chunks"])), size if size <= 16000 else 0)

    def test_output_schemas_match_structured_results(self):
        from apps.webcore_data_mcp_reliability_smoke import _assert_schema_type
        self.event("message_created", "schema-fixture")
        schemas = {tool["name"]: tool["outputSchema"] for tool in self.gateway.list_tools()}
        event_key = self.gateway.call_tool("max_inbox_events", {"chat_id": -100})["rows"][0]["event_key"]
        for name, args in (("max_inbox_events", {"chat_id": -100}),
                           ("max_inbox_event", {"chat_id": -100, "event_key": event_key}), ("cash_accounts", {})):
            result = self.gateway.call_tool(name, args)
            self.assertFalse(set(result) - set(schemas[name]["properties"]))
            for field, value in result.items():
                _assert_schema_type(name, field, value, schemas[name]["properties"][field])

    def test_absent_store_no_creation_and_changed_cash_binding_fails(self):
        for tool, args in (("max_inbox_events", {"chat_id": -100}), ("cash_accounts", {})):
            path = self.inbox / "max_inbox.sqlite3" if tool.startswith("max") else self.cash_path
            path.unlink(missing_ok=True)
            with self.assertRaises(WebCoreDataMcpError) as error:
                self.gateway.call_tool(tool, args)
            self.assertEqual(error.exception.code, "source_unavailable")
            self.assertFalse(path.exists())
        payload = json.loads(self.access.read_text())
        payload["store_id"] = "different-store"
        self.access.write_text(json.dumps(payload))
        with self.assertRaises(WebCoreDataMcpError):
            self.gateway.call_tool("cash_accounts", {})

    def test_connections_query_only_and_message_redaction(self):
        self.event("message_created", "secret", text="token=private-secret")
        self.event("message_edited", "long", text="x" * 900)
        path = self.inbox / "max_inbox.sqlite3"
        before = hashlib.sha256(path.read_bytes()).digest()
        with self.reads._connect(path, None, None) as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM max_inbox_events")
        result = self.gateway.call_tool("max_inbox_events", {"chat_id": -100})
        self.assertEqual(result["rows"][1]["message_text"], "[redacted]")
        self.assertTrue(result["rows"][0]["message_text_truncated"])
        self.assertEqual(len(result["rows"][0]["message_text"]), 550)
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).digest())

    def test_refresh_http_rotation_restart_scope_and_replay(self):
        self.config.oauth_refresh_store_path = self.root / "oauth-refresh" / "tokens.sqlite3"
        verifier = "A" * 64
        code = "fixture-code"
        client = "https://chatgpt.com/oauth/cloud-smoke/client.json"
        resource = self.config.resource_url
        _store_authorization_code(self.config, code, dict(
            client_id=client, redirect_uri="https://chatgpt.com/connector/oauth/callback",
            code_challenge=_pkce_s256_challenge(verifier), resource=resource,
            scope=SCOPE_CASH_READ, subject="owner", expires_at=int(time.time()) + 300, used=False))
        server = build_server(self.config, gateway=self.gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            metadata = _get_json(base + "/.well-known/oauth-authorization-server")
            self.assertIn("refresh_token", metadata["grant_types_supported"])
            token = _post_form(base + "/oauth/token", dict(
                grant_type="authorization_code", code=code,
                redirect_uri="https://chatgpt.com/connector/oauth/callback", client_id=client,
                code_verifier=verifier, resource=resource))
            self.assertIn("refresh_token", token)
            refresh = token["refresh_token"]
            self.assertEqual(token["expires_in"], 3600)
            for overrides, expected in (
                ({"scope": SCOPE_MAX_INBOX_READ}, "invalid_scope"),
                ({"client_id": "https://chatgpt.com/oauth/other.json"}, "invalid_grant"),
                ({"resource": "https://wrong.example"}, "invalid_grant"),
            ):
                with self.subTest(overrides=overrides), self.assertRaises(urllib_error.HTTPError) as error:
                    _post_form(base + "/oauth/token", {
                        **dict(grant_type="refresh_token", refresh_token=refresh,
                               client_id=client, resource=resource), **overrides})
                self.assertEqual(json.loads(error.exception.read())["error"], expected)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        # A new HTTP server/store instance must consume the persisted grant.
        server = build_server(self.config, gateway=self.gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            renewed = _post_form(base + "/oauth/token", dict(
                grant_type="refresh_token", refresh_token=refresh, client_id=client, resource=resource))
            self.assertNotEqual(renewed["refresh_token"], refresh)
            self.assertEqual(renewed["scope"], SCOPE_CASH_READ)
            tools = _post_json(base + "/mcp", dict(jsonrpc="2.0", id=1, method="tools/list"),
                               headers={"Authorization": "Bearer " + renewed["access_token"]})
            self.assertEqual([tool["name"] for tool in tools["result"]["tools"]], ["cash_accounts"])
            for old_token in (refresh, renewed["refresh_token"]):
                with self.assertRaises(urllib_error.HTTPError) as error:
                    _post_form(base + "/oauth/token", dict(grant_type="refresh_token", refresh_token=old_token,
                                                          client_id=client, resource=resource))
                self.assertEqual(json.loads(error.exception.read())["error"], "invalid_grant")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()

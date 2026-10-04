"""Anonymized synthetic shapes of observed provenance failures; zero live calls."""
import json
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from packages.domain.buyer_support_bot.contracts import Event
from packages.domain.buyer_support_bot.extraction import EXTRACTION_SCHEMA, SYSTEM_PROMPT
from packages.domain.buyer_support_bot.repair import REPAIR_PROTOCOL, repair_schema, validate_delta
from packages.domain.buyer_support_bot.replay import (BudgetStop, ReceiptLedger, ResponsesClient,
    fingerprint, run_dialogue, main)

RATES = {"input": .10, "cached": .01, "cache_write": .125, "output": .50}


def fact(key, value, event="b", quote="Стекло треснуло"):
    return {"issue_id": "a", "key": key, "value": value, "evidence": [{"event_id": event, "quote": quote}]}


def data(*facts):
    return {"facts": list(facts), "wording_variant": 0}


def settings(enabled=True):
    return SimpleNamespace(allow_heldout=False, repair_invalid_extraction=enabled,
        max_images_per_checkpoint=0, max_image_bytes=5000000, image_token_cap=20000)


def record(events=None):
    return {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "SYNTHETIC", "split": "dev",
        "events": events or [{"event_id": "b", "role": "buyer", "text": "Стекло треснуло"}]}


class Provider:
    """Mock HTTP transport with a deliberate, finite sequence of completions."""
    def __init__(self, responses, hook=None):
        self.responses, self.hook, self.requests = list(responses), hook, []
    def __call__(self, request, **kwargs):
        body = json.loads(request.data)
        self.requests.append(body)
        if self.hook:
            self.hook(body)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            status, output = response
        else:
            status, output = "completed", response
        raw = {"id": "mock-response", "status": status,
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(raw).encode()
        return Response()


class ClosingLedgerTestCase(unittest.TestCase):
    def ledger(self, *args):
        ledger = ReceiptLedger(*args)
        self.addCleanup(ledger.db.close)
        return ledger


class LedgerRepairLifecycle(ClosingLedgerTestCase):
    def origin(self, ledger):
        bad = data(fact("topic", "fracture", "mistyped"))
        ledger.reserve("origin", .01)
        ledger.finish("origin", {"data": bad, "cache_key": "origin", "accounting": {}, "cost_usd": .01}, .01)
        return {"protocol": REPAIR_PROTOCOL, "origin_request_key": "origin",
            "invalid_response_sha256": fingerprint(bad), "initial_input_sha256": "same-input"}

    def test_concurrent_different_keys_models_share_one_origin_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            first = self.ledger(path, 20, 1)
            binding = self.origin(first)
            connections = [self.ledger(path, 20, 1) for _ in range(8)]
            def reserve(i):
                try:
                    result = connections[i].reserve_repair("repair-" + str(i), .1,
                        {**binding, "repair_model": "changed-" + str(i), "repair_rates": i})
                    return result is None
                except BudgetStop:
                    return False
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(reserve, range(8))), 1)
            self.assertEqual(first.totals()["calls_reserved"], 2)
            self.assertEqual(first.totals()["repair_calls_reserved"], 1)

    def test_completed_repair_reused_despite_changed_protocol_model_rates(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self.ledger(Path(tmp) / "ledger.db", 2, .5)
            binding = self.origin(ledger)
            original = ledger.db.execute("SELECT * FROM calls WHERE key='origin'").fetchone()
            self.assertIsNone(ledger.reserve_repair("repair-1", .1, binding))
            repaired = {"data": data(fact("topic", "fracture")), "cache_key": "repair-1", "accounting": {}, "cost_usd": .02}
            ledger.finish("repair-1", repaired, .02)
            ledger.repair_validation("origin", True)
            cached = ledger.reserve_repair("repair-2", 999,
                {**binding, "protocol": "different", "repair_model": "different", "repair_rates": {"input": 999}})
            self.assertEqual(cached, repaired)
            self.assertEqual(ledger.db.execute("SELECT * FROM calls WHERE key='origin'").fetchone(), original)
            self.assertEqual(ledger.totals()["calls_reserved"], 2)
            self.assertAlmostEqual(ledger.totals()["charged_or_reserved_usd"], .03)
            self.assertAlmostEqual(ledger.totals()["repair_charged_or_reserved_usd"], .02)
            self.assertEqual(ledger.totals()["repair_validation_valid"], 1)

    def test_unknown_repair_never_reserves_again_and_keeps_full_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self.ledger(Path(tmp) / "ledger.db", 5, 1)
            binding = self.origin(ledger)
            ledger.reserve_repair("repair-1", .3, binding)
            ledger.fail("repair-1", "TimeoutError")
            with self.assertRaises(BudgetStop):
                ledger.reserve_repair("new-repair-key", .1, {**binding, "protocol": "new"})
            totals = ledger.totals()
            self.assertEqual(totals["repair_calls_unknown"], 1)
            self.assertAlmostEqual(totals["repair_charged_or_reserved_usd"], .3)
            self.assertEqual(totals["calls_reserved"], 2)

    def test_budget_stop_does_not_create_marker_or_call(self):
        for cap, estimate in ((1, .1), (3, 2)):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as tmp:
                ledger = self.ledger(Path(tmp) / "ledger.db", cap, 1)
                binding = self.origin(ledger)
                with self.assertRaises(BudgetStop):
                    ledger.reserve_repair("repair", estimate, binding)
                self.assertIsNone(ledger.repair_snapshot("origin"))
                self.assertEqual(ledger.totals()["calls_reserved"], 1)

    def test_origin_must_be_known_done_and_match_immutable_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self.ledger(Path(tmp) / "ledger.db", 5, 1)
            binding = self.origin(ledger)
            with self.assertRaises(ValueError):
                ledger.reserve_repair("bad-binding", .1, {**binding, "invalid_response_sha256": "not-matching"})
            ledger.fail("origin", "unknown")
            with self.assertRaises(BudgetStop):
                ledger.reserve_repair("repair", .1, binding)
            self.assertEqual(ledger.totals()["repair_calls_reserved"], 0)


class StrictRepairRegressions(unittest.TestCase):
    def check(self, output, delta, known=None):
        return validate_delta(output, delta, known or {e.event_id: e for e in delta})

    def test_id_and_whitespace_shape_requires_both_exact_not_fuzzy(self):
        event = Event("b", "buyer", "При установке язычок  потянул стекло")
        for identifier, quote in (("mistyped", "язычок  потянул"), ("b", "язычок потянул")):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, "exact observed quote"):
                self.check(data(fact("topic", "tab", identifier, quote)), [event])
        self.assertEqual(self.check(data(fact("topic", "tab", "b", "язычок  потянул")), [event])[2][0].value, "tab")
        schema = repair_schema([event, Event("system", "system", "metadata")])
        self.assertEqual(schema["properties"]["facts"]["items"]["properties"]["evidence"]["items"]["properties"]["event_id"]["enum"], ["b"])
        self.assertNotIn("enum", EXTRACTION_SCHEMA["properties"]["facts"]["items"]["properties"]["evidence"]["items"]["properties"]["event_id"])

    def test_paraphrase_splice_and_mixed_roles_still_fail(self):
        buyer = Event("b", "buyer", "В другом чате одобрели компенсацию. Пузыри не уходят")
        seller = Event("s", "seller", "Пузыри уходят обычно за сутки")
        for output in (data(fact("topic", "compensation", "b", "одобрили компенсацию")),
                       data(fact("topic", "bubbles", "b", "Пузыри ... не уходят")),
                       data({"issue_id": "a", "key": "advice_status", "value": "tried_failed", "evidence": [{"event_id": "s", "quote": seller.text}, {"event_id": "b", "quote": "Пузыри не уходят"}]})):
            with self.assertRaises(ValueError):
                self.check(output, [seller, buyer])

    def test_prior_only_and_future_fact_not_allowed_even_after_repair(self):
        previous, current = Event("old", "buyer", "Трещина"), Event("new", "buyer", "Другой вопрос")
        with self.assertRaisesRegex(ValueError, "delta-supported"):
            self.check(data(fact("topic", "fracture", "old", "Трещина")), [current], {"old": previous, "new": current})
        with self.assertRaisesRegex(ValueError, "exact observed quote"):
            self.check(data(fact("topic", "fracture", "future", "Трещина")), [current], {"old": previous, "new": current})


class ReplayRepairBoundary(ClosingLedgerTestCase):
    def setup_client(self, tmp, cap=10):
        ledger = self.ledger(Path(tmp) / "ledger.db", cap, 1)
        return ledger, ResponsesClient(ledger, "mock", "low", RATES, key="unit-test-key")

    def test_valid_initial_and_metadata_only_never_repair_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.setup_client(tmp)
            provider = Provider([data(fact("topic", "fracture"))])
            with patch("urllib.request.urlopen", provider):
                initial = run_dialogue(record(), client, ledger, {}, settings(False))
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
                resumed = run_dialogue(record(), client, ledger, {}, settings(True))
                media = run_dialogue(record([{"event_id": "m", "role": "buyer", "text": "", "attachments": [{"attachment_id": "photo", "kind": "image"}]}]), client, ledger, {}, settings(True))
            self.assertEqual(initial[0]["fact_extraction_cache_key"], resumed[0]["fact_extraction_cache_key"])
            self.assertTrue(resumed[0]["initial_extraction_valid"])
            self.assertIsNone(resumed[0]["extraction_repair"])
            self.assertEqual(media[0]["extraction_mode"], "deterministic_metadata_only")
            self.assertEqual(ledger.totals()["calls_reserved"], 1)
            self.assertEqual(ledger.totals()["repair_calls_reserved"], 0)
            self.assertEqual(provider.requests[0]["instructions"], SYSTEM_PROMPT)
            self.assertEqual(provider.requests[0]["text"]["format"]["schema"], EXTRACTION_SCHEMA)

    def test_successful_repair_preserves_origin_cache_input_and_separate_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.setup_client(tmp)
            source_before = []
            def hook(body):
                if body["text"]["format"]["name"] == "buyer_facts_repair":
                    origin = ledger.db.execute("SELECT origin_key FROM extraction_repairs").fetchone()[0]
                    source_before.append(ledger.db.execute("SELECT * FROM calls WHERE key=?", (origin,)).fetchone())
            provider = Provider([data(fact("topic", "fracture", "wrong-id")), data(fact("topic", "fracture"))], hook)
            with patch("urllib.request.urlopen", provider):
                rows = run_dialogue(record(), client, ledger, {}, settings())
            row = rows[0];origin = row["fact_extraction_cache_key"]
            self.assertEqual(ledger.db.execute("SELECT * FROM calls WHERE key=?", (origin,)).fetchone(), source_before[0])
            self.assertFalse(row["initial_extraction_valid"])
            self.assertTrue(row["extraction_repair"]["repair_valid"])
            self.assertFalse(row["extraction_repair"]["semantic_correctness_verified"])
            self.assertEqual(row["external_actions_executed"], 0)
            initial_payload = json.loads(provider.requests[0]["input"][0]["content"][0]["text"])
            repair_payload = json.loads(provider.requests[1]["input"][0]["content"][0]["text"])
            self.assertEqual(repair_payload["initial_request"], initial_payload)
            self.assertNotIn("extraction_repair", initial_payload["saved_state"])
            self.assertNotIn("extraction_repair", row["state"])
            with patch("urllib.request.urlopen", side_effect=AssertionError("no duplicate calls")), patch("packages.domain.buyer_support_bot.replay.REPAIR_PROMPT", "changed repair prompt"):
                cached = run_dialogue(record(), client, ledger, {}, settings())
            self.assertEqual(cached[0]["extraction_repair"]["repair_request_key"], row["extraction_repair"]["repair_request_key"])
            totals = ledger.totals()
            self.assertEqual((totals["calls_reserved"], totals["repair_calls_reserved"]), (2, 1))
            self.assertAlmostEqual(totals["charged_or_reserved_usd"], row["extraction_cost_usd"] + row["extraction_repair"]["repair_cost_usd"])

    def test_completed_invalid_repair_stops_without_third_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.setup_client(tmp)
            provider = Provider([data(fact("topic", "fracture", "wrong")), data(fact("topic", "fracture", "still-wrong"))])
            with patch("urllib.request.urlopen", provider):
                with self.assertRaisesRegex(ValueError, "one extraction repair remains invalid") as error:
                    run_dialogue(record(), client, ledger, {}, settings())
            self.assertFalse(error.exception.extraction_repair["repair_valid"])
            with patch("urllib.request.urlopen", side_effect=AssertionError("third call forbidden")):
                with self.assertRaises(ValueError):
                    run_dialogue(record(), client, ledger, {}, settings())
            self.assertEqual(ledger.totals()["repair_validation_invalid"], 1)
            self.assertEqual(ledger.totals()["calls_unknown"], 0)
            self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 0)

    def test_unknown_incomplete_initial_does_not_repair(self):
        for outcome in (TimeoutError(), ("incomplete", data())):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as tmp:
                ledger, client = self.setup_client(tmp)
                provider = Provider([outcome])
                with patch("urllib.request.urlopen", provider), self.assertRaises(RuntimeError):
                    run_dialogue(record(), client, ledger, {}, settings())
                self.assertEqual(ledger.totals()["repair_calls_reserved"], 0)
                self.assertEqual(ledger.totals()["calls_unknown"], 1)
                with patch("urllib.request.urlopen", side_effect=AssertionError("resend forbidden")), self.assertRaises(BudgetStop):
                    run_dialogue(record(), client, ledger, {}, settings())

    def test_unknown_repair_and_budget_stop_never_resend(self):
        for cap in (1, 10):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as tmp:
                ledger, client = self.setup_client(tmp, cap)
                provider = Provider([data(fact("topic", "fracture", "wrong")), TimeoutError()])
                with patch("urllib.request.urlopen", provider), self.assertRaises((BudgetStop, RuntimeError)) as error:
                    run_dialogue(record(), client, ledger, {}, settings())
                self.assertFalse(error.exception.extraction_repair["initial_answer_valid"])
                self.assertEqual(len(provider.requests), 1 if cap == 1 else 2)
                with patch("urllib.request.urlopen", side_effect=AssertionError("resend forbidden")), self.assertRaises(BudgetStop):
                    run_dialogue(record(), client, ledger, {}, settings())
                self.assertEqual(ledger.totals()["repair_calls_reserved"], 0 if cap == 1 else 1)

    def test_prior_delta_guard_happens_before_observe_and_audit_not_in_next_input(self):
        from packages.domain.buyer_support_bot.core import observe
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.setup_client(tmp)
            first = data(fact("topic", "general", "b1", "Первый вопрос"))
            old_only = data(fact("topic", "general", "b1", "Первый вопрос"))
            fixed = data(fact("topic", "general", "b2", "Второй вопрос"))
            final = data(fact("topic", "general", "b3", "Третий вопрос"))
            def hook(body):
                if body["text"]["format"]["name"] == "buyer_facts_repair":
                    self.assertEqual(spy.call_count, 1)
            provider = Provider([first, old_only, fixed, final], hook)
            source = record([{"event_id": "b1", "role": "buyer", "text": "Первый вопрос"}, {"event_id": "b2", "role": "buyer", "text": "Второй вопрос"}, {"event_id": "b3", "role": "buyer", "text": "Третий вопрос"}])
            with patch("urllib.request.urlopen", provider), patch("packages.domain.buyer_support_bot.replay.observe", wraps=observe) as spy:
                rows = run_dialogue(source, client, ledger, {}, settings())
            self.assertEqual(spy.call_count, 3)
            self.assertIsNotNone(rows[1]["extraction_repair"])
            last = json.loads(provider.requests[-1]["input"][0]["content"][0]["text"])
            self.assertNotIn("extraction_repair", last["saved_state"])
            repair_payload = json.loads(provider.requests[2]["input"][0]["content"][0]["text"])
            self.assertEqual([e["event_id"] for e in repair_payload["initial_request"]["actual_delta"]], ["b2"])
            self.assertNotIn("b3", repair_payload["initial_request"]["saved_state"]["processed_events"])
            self.assertNotIn("Третий вопрос", json.dumps(repair_payload, ensure_ascii=False))

    def test_missing_followup_remains_partial_after_successful_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.setup_client(tmp, 2)
            provider = Provider([data(fact("topic", "general", "wrong", "Первый вопрос")), data(fact("topic", "general", "b1", "Первый вопрос"))])
            source = record([{"event_id": "b1", "role": "buyer", "text": "Первый вопрос"}, {"event_id": "b2", "role": "buyer", "text": "Второй вопрос"}])
            with patch("urllib.request.urlopen", provider), self.assertRaises(BudgetStop):
                run_dialogue(source, client, ledger, {}, settings())
            self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
            self.assertEqual(len(provider.requests), 2)

    def test_repair_cannot_cite_old_event_even_mixed_with_a_real_delta_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.setup_client(tmp)
            first = data(fact("topic", "general", "b1", "Первый вопрос"))
            invalid = data(fact("topic", "general", "mistyped", "Второй вопрос"))
            mixed = fact("topic", "general", "b2", "Второй вопрос")
            mixed["evidence"].append({"event_id": "b1", "quote": "Первый вопрос"})
            provider = Provider([first, invalid, data(mixed)])
            source = record([{"event_id": "b1", "role": "buyer", "text": "Первый вопрос"}, {"event_id": "b2", "role": "buyer", "text": "Второй вопрос"}])
            with patch("urllib.request.urlopen", provider), self.assertRaisesRegex(ValueError, "repair remains invalid"):
                run_dialogue(source, client, ledger, {}, settings())
            self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
            self.assertEqual(ledger.totals()["repair_validation_invalid"], 1)

    def test_disabled_mode_never_counts_previous_repaired_checkpoint_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "dev.jsonl", Path(tmp) / "results"
            source.write_text(json.dumps(record()) + "\n")
            arguments = ["--dataset", str(source), "--output-dir", str(output), "--execute", "--model", "mock", "--max-calls", "10", "--max-cost-usd", "1"]
            provider = Provider([data(fact("topic", "fracture", "wrong")), data(fact("topic", "fracture"))])
            with patch.dict("os.environ", {"OPENAI_API_KEY": "unit-test-key"}), patch("urllib.request.urlopen", provider), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments + ["--repair-invalid-extraction"]), 0)
            first_summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(first_summary["repaired_extraction_checkpoints"], 1)
            self.assertEqual(first_summary["successful_initial_extraction_checkpoints"], 0)
            with patch.dict("os.environ", {"OPENAI_API_KEY": "unit-test-key"}), patch("urllib.request.urlopen", side_effect=AssertionError("no network")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 1)
            disabled = json.loads((output / "summary.json").read_text())
            self.assertEqual(disabled["completed_checkpoints"], 0)
            self.assertEqual(disabled["fully_completed_dialogues"], 0)
            self.assertEqual((output / "results.jsonl").read_text(), "")

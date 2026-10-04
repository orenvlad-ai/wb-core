import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from packages.domain.buyer_support_bot.contracts import Event
from packages.domain.buyer_support_bot.extraction import validate_extraction, EXTRACTION_SCHEMA
from packages.domain.buyer_support_bot.replay import (BudgetStop, ReceiptLedger, ResponsesClient,
    account_usage, context_from, image_input, load_media, main, run_dialogue)

RATES = {"input": .10, "cached": .01, "cache_write": .125, "output": .50}


def item(issue, key, value, event, quote):
    return {"issue_id": issue, "key": key, "value": value, "evidence": [{"event_id": event, "quote": quote}]}


class ExtractionBoundary(unittest.TestCase):
    def check(self, facts, events):
        return validate_extraction({"facts": facts, "wording_variant": 0}, events)

    def test_quote_and_event_must_exist(self):
        e = Event("e", "buyer", "трещина")
        for bad in (item("a", "topic", "fracture", "future", "трещина"), item("a", "topic", "fracture", "e", "вымышленное")):
            with self.assertRaises(ValueError):
                self.check([bad], [e])

    def test_seller_cannot_confirm_buyer_result_or_stage(self):
        e = Event("s", "seller", "Заявка одобрена и вопрос решён")
        for key, value in (("resolved", "true"), ("stage", "in_use"), ("topic", "fracture")):
            with self.assertRaises(ValueError):
                self.check([item("a", key, value, "s", "вопрос решён")], [e])

    def test_buyer_cannot_increment_seller_photo_counter(self):
        with self.assertRaises(ValueError):
            self.check([item("a", "photo_requested", "true", "b", "фото")], [Event("b", "buyer", "фото")])

    def test_unsupported_fact_value_and_external_status_rejected(self):
        for key, value in (("stage", "five_minutes"), ("approved", "true")):
            with self.assertRaises(ValueError):
                self.check([item("a", key, value, "b", "да")], [Event("b", "buyer", "да")])

    def test_seller_obsolete_promise_is_evidence_not_policy(self):
        facts, _ = self.check([item("legacy", "historical_obligation", "replacement_glass", "s", "новое стекло")], [Event("s", "seller", "Отправим новое стекло")])
        self.assertEqual(facts[0].value, "replacement_glass")

    def test_observed_product_title_does_not_verify_compatibility(self):
        c = context_from({"product": {"name": "iPhone 17 Pro", "nmID": 1}})
        self.assertEqual(c.compatibility, "unknown")
        self.assertFalse(c.product_verified)


class LedgerAndResponses(unittest.TestCase):
    def test_cache_cost_count_and_unknown_no_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 2, 1)
            self.assertIsNone(ledger.reserve("a", .3))
            ledger.finish("a", {"data": {"ok": True}}, .1)
            self.assertEqual(ledger.reserve("a", .3)["data"], {"ok": True})
            ledger.reserve("b", .3)
            ledger.fail("b", "TimeoutError")
            with self.assertRaises(BudgetStop):
                ledger.reserve("b", .3)
            with self.assertRaises(BudgetStop):
                ledger.reserve("c", .3)
            self.assertAlmostEqual(ledger.totals()["charged_or_reserved_usd"], .4)

    def test_atomic_shared_budget_under_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 3, .7)
            def attempt(i):
                try:
                    ledger.reserve(str(i), .2)
                    return 1
                except BudgetStop:
                    return 0
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(attempt, range(20))), 3)
            self.assertEqual(ledger.totals()["calls_reserved"], 3)

    def test_two_process_style_connections_share_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            first, second = ReceiptLedger(path, 1, 1), ReceiptLedger(path, 1, 1)
            first.reserve("first", .1)
            with self.assertRaises(BudgetStop):
                second.reserve("second", .1)

    def test_usage_cached_input_not_double_counted_reasoning_not_extra(self):
        accounting, cost = account_usage({"input_tokens": 1000, "output_tokens": 200, "input_tokens_details": {"cached_tokens": 300, "cache_write_tokens": 100}, "output_tokens_details": {"reasoning_tokens": 100}}, RATES)
        self.assertEqual(accounting["reasoning_tokens"], 100)
        self.assertAlmostEqual(cost, (600*.10 + 300*.01 + 100*.125 + 200*.50)/1e6)

    def test_no_missing_usage_assumed_zero(self):
        with self.assertRaises(ValueError):
            account_usage({}, RATES)

    def test_http_error_never_leaks_key_or_body_and_never_retries(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 3, 2)
            client = ResponsesClient(ledger, "mock", "low", RATES, key="DO-NOT-LOG-THIS")
            with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", 401, "key DO-NOT-LOG-THIS", {}, None)) as opener:
                with self.assertRaisesRegex(RuntimeError, "http_401") as error:
                    client.structured("test", "prompt", {"value": 1}, EXTRACTION_SCHEMA)
                self.assertNotIn("DO-NOT-LOG-THIS", str(error.exception))
                with self.assertRaises(BudgetStop):
                    client.structured("test", "prompt", {"value": 1}, EXTRACTION_SCHEMA)
                self.assertEqual(opener.call_count, 1)
            self.assertNotIn("DO-NOT-LOG-THIS", Path(tmp, "ledger.db").read_bytes().decode(errors="ignore"))

    def test_exact_response_cache_reused_without_second_paid_call(self):
        payload = {"id": "resp_mock", "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 30}, "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"facts":[],"wording_variant":0}'}]}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 3, 2)
            client = ResponsesClient(ledger, "mock", "low", RATES, key="ENV-ONLY")
            with patch("urllib.request.urlopen", return_value=Response()) as opener:
                one = client.structured("test", "prompt", {"value": 1}, EXTRACTION_SCHEMA)
                two = client.structured("test", "prompt", {"value": 1}, EXTRACTION_SCHEMA)
                self.assertEqual(one, two)
                self.assertEqual(opener.call_count, 1)
                request = opener.call_args.args[0]
                data = json.loads(request.data)
                self.assertTrue(data["text"]["format"]["strict"])
                self.assertFalse(data["store"])
                self.assertNotIn("tools", data)

    def test_no_silent_text_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 3, 2)
            client = ResponsesClient(ledger, "mock", "low", RATES, max_input_bytes=5, key="ENV-ONLY")
            with self.assertRaisesRegex(ValueError, "nothing truncated"):
                client.structured("test", "prompt", {"text": "Длинный текст"}, EXTRACTION_SCHEMA)
            self.assertEqual(ledger.totals()["calls_reserved"], 0)

    def test_hash_verified_media(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            data = b"\xff\xd8\xffsynthetic"
            sha = hashlib.sha256(data).hexdigest()
            path = Path(tmp, sha)
            path.write_bytes(data)
            manifest = Path(tmp, "manifest.json")
            manifest.write_text(json.dumps({"items": [{"attachment_id": "m", "sha256": sha, "filename": sha, "local_path": "/otherhost/unreachable"}]}))
            mapped = load_media(manifest, tmp)
            self.assertEqual(image_input(mapped["m"], 100, 20000)[0], data)
            path.write_bytes(b"altered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                image_input(mapped["m"], 100, 20000)


class HistoricalReplay(unittest.TestCase):
    def test_actual_prefix_no_future_no_candidate_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 10, 1)
            class Client:
                model, reasoning = "mock", "low"
                def __init__(self): self.inputs = []
                def structured(self, kind, prompt, payload, schema, image=None):
                    self.inputs.append(payload)
                    delta = payload["actual_delta"]
                    facts = [item("a", "topic", "bubbles", "b1", "воздушные пузыри"), item("a", "bubble_type", "air", "b1", "воздушные пузыри")] if len(self.inputs) == 1 else [item("a", "advice_given", "bubbles", "s1", "приподнимите край"), item("a", "advice_status", "tried_failed", "b2", "попробовал, не помогло")]
                    return {"data": {"facts": facts, "wording_variant": 0}, "cache_key": str(len(self.inputs)), "accounting": {"input_tokens": 1, "output_tokens": 1}, "cost_usd": 0}
            client = Client()
            record = {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "D1", "split": "dev", "metadata": {"future_hint": "must ignore"}, "events": [
                {"event_id": "b1", "role": "buyer", "text": "воздушные пузыри", "context": {"product": {"name": "unverified title"}}},
                {"event_id": "s1", "role": "seller", "text": "приподнимите край"},
                {"event_id": "b2", "role": "buyer", "text": "попробовал, не помогло"},
                {"event_id": "s2", "role": "seller", "text": "FUTURE-SECRET"},
            ]}
            settings = SimpleNamespace(allow_heldout=False, max_images_per_checkpoint=2, max_image_bytes=100, image_token_cap=20000)
            rows = run_dialogue(record, client, ledger, {}, settings)
            self.assertEqual(len(rows), 2)
            self.assertNotIn("FUTURE-SECRET", json.dumps(client.inputs))
            self.assertNotIn("must ignore", json.dumps(client.inputs))
            self.assertEqual(rows[0]["decision"]["action"], "advise")
            self.assertEqual(rows[1]["state_before"]["issues"]["a"]["advice_given"], [])
            self.assertEqual(rows[1]["decision"]["action"], "request_photo")
            self.assertNotIn(rows[0]["candidate_reply"], json.dumps(client.inputs[1]))

    def test_jsonl_unicode_separators_are_not_record_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "D1", "split": "dev", "events": [{"event_id": "e", "role": "buyer", "text": "первая\u2028вторая\u0085третья"}]}
            file = Path(tmp, "dev.jsonl")
            file.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--dataset", str(file), "--output-dir", str(Path(tmp, "out")), "--max-calls", "1", "--max-cost-usd", "1"]), 0)
            self.assertEqual(json.loads(output.getvalue())["dialogues"], 1)
            self.assertFalse(Path(tmp, "out").exists())

    def test_closed_holdout_requires_explicit_flag_even_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "SYNTHETIC", "split": "holdout", "events": []}
            file = Path(tmp, "synthetic.jsonl")
            file.write_text(json.dumps(data) + "\n")
            with self.assertRaisesRegex(ValueError, "allow-heldout"):
                main(["--dataset", str(file), "--output-dir", str(Path(tmp, "out")), "--max-calls", "1", "--max-cost-usd", "1"])


if __name__ == "__main__": unittest.main()

class PurchaseContextReplay(unittest.TestCase):
    def test_context_retained_and_purchases_partitioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 10, 1)
            class Client:
                model, reasoning = "mock", "low"
                def __init__(self): self.inputs = []
                def structured(self, kind, prompt, payload, schema, image=None):
                    self.inputs.append(payload)
                    buyer = next(source for source in reversed(payload["actual_delta"]) if source["role"] == "buyer")
                    fact = item("a", "topic", "privacy", buyer["event_id"], buyer["text"])
                    return {"data": {"facts": [fact], "wording_variant": 0}, "cache_key": str(len(self.inputs)), "accounting": {"input_tokens": 1, "output_tokens": 1}, "cost_usd": 0}
            client = Client()
            record = {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "D1", "split": "dev", "events": [
                {"event_id": "b1", "role": "buyer", "text": "антишпион", "context": {"product": {"purchase_key": "P1", "nmID": 1, "name": "Антишпион стекло", "price": 0, "priceCurrency": ""}}},
                {"event_id": "b2", "role": "buyer", "text": "уточняю"},
                {"event_id": "b3", "role": "buyer", "text": "другая покупка", "context": {"product": {"purchase_key": "P2", "nmID": 2, "name": "Матовое стекло"}}},
                {"event_id": "b4", "role": "buyer", "text": "без покупки"},
            ]}
            settings = SimpleNamespace(allow_heldout=False, max_images_per_checkpoint=2, max_image_bytes=100, image_token_cap=20000)
            rows = run_dialogue(record, client, ledger, {}, settings)
            self.assertEqual(client.inputs[1]["known_purchase_context"]["product"]["name"], "Антишпион стекло")
            self.assertNotIn("price", client.inputs[1]["known_purchase_context"]["product"])
            self.assertEqual(client.inputs[2]["saved_state"]["issues"], {})
            self.assertTrue(rows[3]["purchase_ambiguous"])
            self.assertEqual(client.inputs[3]["saved_state"]["issues"], {})
            self.assertNotEqual(rows[0]["purchase_scope"], rows[2]["purchase_scope"])

    def test_line_from_wb_title_not_phone_compatibility(self):
        for name, line in (("Антишпион на iPhone 17 Pro", "privacy"), ("Матовое стекло iPhone 17", "matte")):
            c = context_from({"product": {"nmID": 1, "name": name}})
            self.assertTrue(c.product_verified)
            self.assertEqual(c.product_line, line)
            self.assertEqual(c.compatibility, "unknown")

class ConservativeMissingCacheWrite(unittest.TestCase):
    def test_missing_cache_write_charged_at_conservative_rate(self):
        accounting, charged = account_usage({"input_tokens": 1000, "output_tokens": 200, "input_tokens_details": {"cached_tokens": 300}}, RATES)
        self.assertFalse(accounting["cache_write_reported"])
        self.assertAlmostEqual(charged, (700*.125 + 300*.01 + 200*.50)/1e6)
        self.assertAlmostEqual(accounting["standard_estimated_cost_usd"], (700*.10 + 300*.01 + 200*.50)/1e6)

class PrefixMediaBindingRegressions(unittest.TestCase):
    def scenario(self, prefix_sha, manifest_sha):
        class Client:
            model, reasoning = "mock", "low"
            def __init__(self): self.photo_calls = 0
            def structured(self, kind, prompt, payload, schema, image=None):
                if kind == "buyer_photo":
                    self.photo_calls += 1
                    data = {"result": "suitable"}
                else:
                    data = {"facts": [item("a", "topic", "tab", "b", "язычок оторвался")], "wording_variant": 0}
                return {"data": data, "cache_key": "mock-" + kind, "accounting": {"input_tokens": 1, "output_tokens": 1}, "cost_usd": 0}
        client = Client()
        attachment = {"attachment_id": "a", "kind": "image"}
        if prefix_sha is not None:
            attachment["sha256"] = prefix_sha
        record = {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "D", "split": "dev", "events": [{"event_id": "b", "role": "buyer", "text": "язычок оторвался", "attachments": [attachment]}]}
        media = {"a": {"sha256": manifest_sha, "resolved_path": "/never/read/unrelated.jpg"}}
        settings = SimpleNamespace(allow_heldout=False, max_images_per_checkpoint=2, max_image_bytes=100, image_token_cap=20000)
        return client, record, media, settings

    def test_wrong_or_missing_prefix_hash_stops_before_file_read_or_photo_call(self):
        for prefix_sha in ("a" * 64, None):
            with self.subTest(prefix_sha=prefix_sha), tempfile.TemporaryDirectory() as tmp:
                client, record, media, settings = self.scenario(prefix_sha, "b" * 64)
                ledger = ReceiptLedger(Path(tmp) / "ledger.db", 10, 1)
                with patch("packages.domain.buyer_support_bot.replay.image_input") as image_reader:
                    with self.assertRaisesRegex(ValueError, "prefix attachment hash"):
                        run_dialogue(record, client, ledger, media, settings)
                    image_reader.assert_not_called()
                self.assertEqual(client.photo_calls, 0)

    def test_matching_hash_allows_only_the_bound_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, record, media, settings = self.scenario("b" * 64, "b" * 64)
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 10, 1)
            with patch("packages.domain.buyer_support_bot.replay.image_input", return_value=(b"mock", "image/jpeg", 20000)) as image_reader:
                rows = run_dialogue(record, client, ledger, media, settings)
                image_reader.assert_called_once_with(media["a"], 100, 20000)
            self.assertEqual(client.photo_calls, 1)
            self.assertEqual(rows[0]["state"]["observations"][0]["attachment_id"], "a")

class EmptyMediaCitationRegressions(unittest.TestCase):
    def test_photo_only_invented_image_is_omitted_never_validated_as_quote(self):
        from packages.domain.buyer_support_bot.extraction import omit_empty_buyer_metadata
        events = [Event("photo", "buyer", "", attachments=({"attachment_id": "a", "kind": "image"},))]
        raw = {"facts": [item("general", "topic", "general", "photo", "image"), item("general", "substantive", "true", "photo", "image")], "wording_variant": 0}
        with self.assertRaisesRegex(ValueError, "exact observed quote"):
            validate_extraction(raw, events)
        accepted, audit = omit_empty_buyer_metadata(raw, events)
        self.assertEqual(accepted["facts"], [])
        self.assertEqual(len(audit), 2)
        self.assertEqual(validate_extraction(accepted, events)[0], [])
        self.assertEqual(raw["facts"][0]["evidence"][0]["quote"], "image")

    def test_mixed_actual_seller_quote_retained_empty_buyer_substantive_omitted(self):
        from packages.domain.buyer_support_bot.extraction import omit_empty_buyer_metadata
        events = [Event("seller", "seller", "Пришлите фото края"), Event("photo", "buyer", "", attachments=({"attachment_id": "a", "kind": "image", "availability": "available", "sha256": "a"*64},))]
        raw = {"facts": [item("edge", "photo_requested", "true", "seller", "Пришлите фото края"), item("edge", "substantive", "false", "photo", "")], "wording_variant": 0}
        accepted, audit = omit_empty_buyer_metadata(raw, events)
        facts, _ = validate_extraction(accepted, events)
        self.assertEqual([fact.key for fact in facts], ["photo_requested"])
        self.assertEqual(len(audit), 1)
        from packages.domain.buyer_support_bot import CaseState, observe
        s = observe(CaseState("x"), events[0], facts)
        s = observe(s, events[1], [])
        self.assertEqual(s.issues["edge"].counters["photo_requests"], 1)
        self.assertTrue(s.last_substantive)
        self.assertEqual(s.received_materials["photo:0"]["attachment_id"], "a")
        self.assertEqual(s.received_materials["photo:0"]["sha256"], "a"*64)
        self.assertFalse(s.observations)  # actual receipt is not a visual conclusion
        self.assertNotIn("substantive", s.issues["edge"].facts)

    def test_semantic_or_nonempty_quote_errors_remain_fatal(self):
        from packages.domain.buyer_support_bot.extraction import omit_empty_buyer_metadata
        cases = [
            ([Event("b", "buyer", "", attachments=({"kind": "image"},))], item("a", "stage", "in_use", "b", "image")),
            ([Event("b", "buyer", "", attachments=({"kind": "image"},))], item("a", "resolved", "true", "b", "")),
            ([Event("b", "buyer", "", attachments=({"kind": "image"},))], item("a", "return_requested", "true", "b", "image")),
            ([Event("b", "buyer", "", attachments=({"kind": "image"},))], item("a", "topic", "fracture", "b", "image")),
            ([Event("b", "buyer", "Настоящий текст", attachments=({"kind": "image"},))], item("a", "substantive", "true", "b", "image")),
            ([Event("b", "buyer", "")], item("a", "topic", "general", "b", "image")),
        ]
        for events, fact in cases:
            with self.subTest(key=fact["key"]):
                accepted, audit = omit_empty_buyer_metadata({"facts": [fact], "wording_variant": 0}, events)
                self.assertFalse(audit)
                with self.assertRaises(ValueError):
                    validate_extraction(accepted, events)

    def test_nontext_delta_never_calls_model_and_records_received_photo(self):
        class NoModel:
            model, reasoning = "mock", "low"
            def structured(self, *args, **kwargs):
                raise AssertionError("there is no text to extract")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 1, 1)
            record = {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "D", "split": "dev", "events": [{"event_id": "b", "role": "buyer", "text": "", "attachments": [{"attachment_id": "a", "kind": "image", "availability": "unavailable"}]}]}
            settings = SimpleNamespace(allow_heldout=False, max_images_per_checkpoint=2, max_image_bytes=100, image_token_cap=20000)
            rows = run_dialogue(record, NoModel(), ledger, {}, settings)
            self.assertEqual(rows[0]["extraction_mode"], "deterministic_metadata_only")
            self.assertIsNone(rows[0]["fact_extraction_cache_key"])
            self.assertEqual(rows[0]["state"]["received_materials"]["b:0"]["attachment_id"], "a")
            self.assertEqual(rows[0]["decision"]["template"], "problem_detail")
            self.assertEqual(ledger.totals()["calls_reserved"], 0)

    def test_mixed_replay_reports_omission_and_keeps_request_cache_contract(self):
        class Client:
            model, reasoning = "mock", "low"
            def __init__(self): self.calls = []
            def structured(self, kind, prompt, payload, schema, image=None):
                self.calls.append(payload)
                if len(self.calls) == 1:
                    facts = [item("edge", "topic", "edge", "b1", "край не приклеился")]
                else:
                    facts = [item("edge", "photo_requested", "true", "s", "Пришлите фото"), item("edge", "substantive", "false", "b2", "")]
                return {"data": {"facts": facts, "wording_variant": 0}, "cache_key": "completed-receipt", "accounting": {"input_tokens": 1, "output_tokens": 1}, "cost_usd": 0}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "ledger.db", 10, 1)
            client = Client()
            record = {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "D", "split": "dev", "events": [{"event_id": "b1", "role": "buyer", "text": "край не приклеился"}, {"event_id": "s", "role": "seller", "text": "Пришлите фото"}, {"event_id": "b2", "role": "buyer", "text": "", "attachments": [{"attachment_id": "a", "kind": "image"}]}]}
            settings = SimpleNamespace(allow_heldout=False, max_images_per_checkpoint=2, max_image_bytes=100, image_token_cap=20000)
            rows = run_dialogue(record, client, ledger, {}, settings)
            self.assertEqual(rows[1]["metadata_fact_omission_count"], 1)
            self.assertEqual(rows[1]["extracted_facts"][0]["key"], "photo_requested")
            self.assertEqual(rows[1]["state"]["issues"]["edge"]["counters"]["photo_requests"], 1)
            self.assertEqual(rows[1]["state"]["received_materials"]["b2:0"]["event_id"], "b2")
            self.assertNotIn("received_materials", client.calls[0]["saved_state"])
            self.assertNotIn("received_materials", client.calls[1]["saved_state"])

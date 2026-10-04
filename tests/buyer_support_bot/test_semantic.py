"""Anonymized synthetic regressions for the reviewed semantic/media package."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packages.domain.buyer_support_bot import CaseState, ClaimSnapshot, Context, Event, Evidence, Fact, PhotoObservation, decide, observe, render
from packages.domain.buyer_support_bot.contracts import IssueState, OperationIntent, ReviewSnapshot
from packages.domain.buyer_support_bot.extraction import validate_extraction
from packages.domain.buyer_support_bot.replay import PHOTO_SCHEMA, ReceiptLedger, photo_payload, run_dialogue, validate_photo


def facts(event_id, text, issue_id="new", **values):
    return [Fact(issue_id, key, value, (Evidence(event_id, text),)) for key, value in values.items()]


def turn(state=None, issue_id="new", text="Синтетический вопрос", **values):
    state = state or CaseState("synthetic")
    return observe(state, Event("current", "buyer", text), facts("current", text, issue_id, **values))


def subject(**values):
    return CaseState("synthetic", issues={"a": IssueState(facts=values)})


class CurrentFocusRegressions(unittest.TestCase):
    def old_issue(self):
        return subject(topic="edge", advice_status="tried_failed", historical_obligation="replacement_glass")

    def test_product_question_without_actual_question_is_available(self):
        s = turn(topic="product", buyer_intent="question_pending")
        d = decide(s, Context())
        self.assertEqual((d.action, d.template), ("clarify", "product_question"))
        self.assertTrue(render(s, d, Context()).startswith("Здравствуйте."))
        self.assertNotIn("жаль", render(s, d, Context()))
        self.assertEqual(d.method, "unknown")

    def test_neutral_property_query_not_glass_scratch_or_supplies(self):
        for topic in ("scratch", "supplies"):
            s = turn(topic=topic, buyer_intent="product_question")
            d = decide(s, Context())
            self.assertEqual((d.action, d.rule), ("data_unavailable", "product_answer_grounding"))
            reply = render(s, d, Context())
            self.assertIn("подтверждённые сведения", reply)
            self.assertNotIn("жаль", reply)
            self.assertNotIn("установ", reply)

    def test_current_status_or_logistics_beats_old_defect_and_promise(self):
        for intent in ("return_status", "return_logistics"):
            s = turn(self.old_issue(), buyer_intent=intent, topic="supplies")
            before = copy.deepcopy(s.issues["a"])
            d = decide(s, Context())
            self.assertEqual((d.action, d.rule, d.method), ("restore_read", "current_return_status", "unknown"))
            self.assertIn("legacy_obligation_resolution", d.secondary_unavailable)
            self.assertEqual(s.issues["a"], before)
            self.assertNotIn("салфет", render(s, d, Context()))
            self.assertNotIn("видео", render(s, d, Context()))

    def test_current_status_uses_only_fresh_authoritative_method(self):
        s = turn(buyer_intent="return_logistics", topic="general")
        for source in ("unknown", "simulation"):
            c = Context(claim=ClaimSnapshot(availability="present", status="approved", linked=True, fresh=True, source=source, return_method="keep_goods"))
            self.assertEqual(decide(s, c).action, "restore_read")
        c = Context(claim=ClaimSnapshot(availability="present", status="approved", linked=True, fresh=True, source="authoritative_api", return_method="return_goods"))
        self.assertEqual(decide(s, c).action, "confirmed_return")

    def test_new_installation_question_keeps_old_obligation(self):
        s = turn(self.old_issue(), buyer_intent="installation_help", topic="instruction")
        d = decide(s, Context())
        self.assertEqual(d.template, "instruction")
        self.assertIn("legacy_obligation_resolution", d.secondary_unavailable)
        self.assertEqual(s.issues["a"].facts["historical_obligation"], "replacement_glass")

    def test_legacy_followup_acknowledges_without_claiming_fulfillment(self):
        s = turn(self.old_issue(), buyer_intent="legacy_followup", topic="general")
        d = decide(s, Context())
        self.assertEqual((d.action, d.rule), ("technical_pause", "legacy_promise_followup"))
        self.assertIn("сейчас подтвердить исполнение не можем", render(s, d, Context()))
        self.assertFalse(d.operation)

    def test_fresh_neutral_topic_precedes_separate_old_return_ground(self):
        old = subject(topic="missing_glass", glass_status="missing_on_receipt")
        s = turn(old, topic="general", problem_context="installation")
        d = decide(s, Context())
        self.assertEqual((d.issue_id, d.template), ("new", "installation_detail"))

    def test_thanks_is_quiet_and_not_proof_of_fulfillment(self):
        s = turn(self.old_issue(), buyer_intent="acknowledgement", substantive="false")
        d = decide(s, Context())
        self.assertEqual(d.action, "silent")
        self.assertEqual(render(s, d, Context()), "")
        self.assertNotEqual(s.issues["a"].facts.get("resolved"), "true")
        self.assertIn("legacy_obligation_resolution", d.secondary_unavailable)
        self.assertEqual(s.observations, [])

    def test_explicit_resolution_closes_exchange_preserving_old_obligation(self):
        s = turn(self.old_issue(), issue_id="a", resolved="true")
        d = decide(s, Context())
        self.assertEqual(d.action, "complete")
        self.assertEqual(s.issues["a"].facts["historical_obligation"], "replacement_glass")
        self.assertFalse(d.operation)

    def test_new_intent_is_not_replayed_on_later_turn(self):
        s = turn(topic="review", buyer_intent="review_edit")
        self.assertEqual(decide(s, Context()).action, "technical_pause")
        s = observe(s, Event("later", "buyer", "Новая проблема при установке"), facts("later", "Новая проблема при установке", topic="general", problem_context="installation"))
        self.assertEqual(decide(s, Context()).template, "installation_detail")

    def test_review_mention_without_edit_request_is_not_edit(self):
        s = turn(topic="review")
        d = decide(s, Context())
        self.assertEqual(d.action, "clarify")
        self.assertNotIn("изменении отзыва", render(s, d, Context()))
        s = turn(topic="review", buyer_intent="review_find")
        self.assertIn("где найти отзыв", render(s, decide(s, Context()), Context()))

    def test_explicit_resolution_with_thanks_preserves_strict_review_gate(self):
        s = turn(topic="edge", buyer_intent="acknowledgement", resolved="true")
        self.assertEqual(decide(s, Context()).action, "complete")
        review = ReviewSnapshot(linked=True, fresh=True, source="authoritative_api", negative=True)
        self.assertEqual(decide(s, Context(review=review)).action, "request_review")
        s.review_requested = True
        self.assertEqual(decide(s, Context(review=review)).action, "complete")
        without_thanks = turn(topic="edge", resolved="true")
        self.assertEqual(decide(without_thanks, Context(review=review)).action, "request_review")

    def test_fresh_multiple_issues_use_one_existing_sufficient_return_ground(self):
        text = "Синтетическая жалоба с двумя недостатками"
        observed = facts("current", text, "touch", topic="touch", stage="installation", advice_status="tried_failed") + facts("current", text, "tab", topic="tab")
        s = observe(CaseState("synthetic"), Event("current", "buyer", text), observed)
        d = decide(s, Context(claim=ClaimSnapshot(availability="absent")))
        self.assertEqual((d.issue_id, d.action, d.method), ("touch", "request_claim", "keep_goods"))

    def test_uncertain_operation_cannot_be_bypassed_by_focus_or_closure(self):
        for op_state in ("dispatching", "unknown"):
            for values in ({"buyer_intent": "review_edit"}, {"buyer_intent": "installation_help"}, {"buyer_intent": "return_status"}, {"buyer_intent": "acknowledgement"}, {"resolved": "true"}):
                with self.subTest(op_state=op_state, values=values):
                    s = turn(topic="general", **values)
                    s.operations["same-operation"] = OperationIntent("same-operation", "same-claim", "approve1", state=op_state, simulated=False)
                    before = copy.deepcopy(s)
                    d = decide(s, Context())
                    self.assertEqual((d.action, d.rule), ("verify_operation", "unknown_operation_no_resend"))
                    self.assertFalse(d.operation)
                    self.assertEqual(s, before)


class GroundedFactsRegressions(unittest.TestCase):
    def test_compensation_requires_actual_phone_damage_scope(self):
        for scope in (None, "glass_refund", "unknown"):
            values = {"topic": "compensation", "buyer_intent": "replacement"}
            if scope: values["compensation_kind"] = scope
            s = subject(**values)
            d = decide(s, Context())
            self.assertNotEqual(d.rule, "phone_damage_separate")
            reply = render(s, d, Context())
            self.assertNotIn("экран вашего телефона повреждён", reply)
            self.assertNotIn("ремонт", reply)
            self.assertFalse(d.operation)
        s = subject(topic="compensation", compensation_kind="phone_damage")
        self.assertEqual(decide(s, Context()).rule, "phone_damage_separate")

    def test_wb_reward_is_platform_payment_not_phone_repair(self):
        s = subject(topic="compensation", compensation_kind="wb_reward")
        d = decide(s, Context())
        self.assertEqual(d.template, "payment")
        self.assertNotIn("ремонт", render(s, d, Context()))
        self.assertNotIn("экран", render(s, d, Context()))

    def test_unknown_payment_scope_never_creates_generic_glass_return(self):
        s = subject(topic="compensation", compensation_kind="unknown")
        s.issues["a"].counters["detail_requests"] = 2
        d = decide(s, Context(chat_available=False, claim=ClaimSnapshot(availability="absent")))
        self.assertEqual(d.method, "unknown")
        self.assertFalse(d.operation)

    def test_missing_glass_is_receipt_absence_not_disposal_or_consumables(self):
        for status in ("discarded", "received", "unknown"):
            s = subject(topic="missing_glass", glass_status=status)
            d = decide(s, Context())
            self.assertNotEqual(d.rule, "missing_glass_return")
            self.assertEqual(d.method, "unknown")
            self.assertFalse(d.operation)
            self.assertNotIn("не оказалось стекла", render(s, d, Context()))
        s = subject(topic="missing_glass", glass_status="missing_on_receipt")
        d = decide(s, Context(claim=ClaimSnapshot(availability="absent")))
        self.assertEqual((d.rule, d.method), ("missing_glass_return", "keep_goods"))
        self.assertNotEqual(d.action, "request_photo")

    def test_elapsed_time_or_looking_does_not_establish_damage_stage(self):
        for stage in ("in_use", "installation", "initial_inspection"):
            for basis in (None, "elapsed_discovery", "unknown"):
                values = {"topic": "fracture", "stage": stage, "return_requested": "true"}
                if basis: values["stage_basis"] = basis
                s = subject(**values)
                d = decide(s, Context())
                self.assertEqual(d.template, "fracture_stage")
                self.assertFalse(d.operation)
        s = subject(topic="fracture", stage="in_use", stage_basis="explicit_stage", return_requested="true")
        self.assertEqual(decide(s, Context()).rule, "post_use_fracture")

    def test_discovery_after_days_is_not_late_wear_and_cleaning_not_repeated(self):
        s = subject(topic="marks", marks_kind="late_wear", stage_basis="elapsed_discovery", advice_status="tried_failed")
        d = decide(s, Context())
        self.assertEqual(d.template, "marks_manifestation")
        self.assertNotEqual(d.rule, "marks_late_wear")
        self.assertNotIn("микрофибр", render(s, d, Context()))
        s = subject(topic="marks", marks_kind="unknown")
        self.assertEqual(decide(s, Context()).template, "wipe")

    def test_weaker_discovery_statement_does_not_erase_explicit_known_stage(self):
        s = turn(issue_id="a", stage="in_use", stage_basis="explicit_stage", topic="fracture")
        s = observe(s, Event("later", "buyer", "Заметил ещё вчера"), facts("later", "Заметил ещё вчера", "a", stage_basis="elapsed_discovery"))
        self.assertEqual(s.issues["a"].facts["stage_basis"], "explicit_stage")
        self.assertEqual(decide(s, Context()).rule, "post_use_fracture")

    def test_possible_variant_unknown_compatibility_no_photo(self):
        for model in (None, "iPhone synthetic"):
            values = {"topic": "wrong_item", "problem_context": "fit"}
            if model: values["phone_model"] = model
            s = subject(**values)
            d = decide(s, Context())
            self.assertNotEqual(d.action, "request_photo")
            self.assertEqual(d.method, "unknown")
            self.assertFalse(d.operation)

    def test_known_installation_mechanism_or_geometry_gets_targeted_question(self):
        for context, template in (("installation", "installation_detail"), ("mechanism", "mechanism_detail"), ("geometry", "geometry_detail")):
            for topic in ("general", "product"):
                s = subject(topic=topic, problem_context=context)
                d = decide(s, Context())
                self.assertEqual(d.template, template)
                self.assertNotIn("что произошло со стеклом", render(s, d, Context()))
                self.assertEqual(d.method, "unknown")
        s = subject(topic="general", problem_context="geometry")
        self.assertNotIn("установилось неровно", render(s, decide(s, Context()), Context()))

    def test_specific_mechanism_or_alignment_requires_established_kind(self):
        for topic, template in (("film", "mechanism_detail"), ("tab", "mechanism_detail"), ("alignment", "geometry_detail"), ("supplies", "mechanism_detail")):
            s = subject(topic=topic, **({"problem_context": "mechanism"} if topic == "supplies" else {}))
            d = decide(s, Context())
            self.assertEqual(d.template, template)
            self.assertNotEqual(d.action, "request_photo")
            self.assertEqual(d.method, "unknown")
            if topic in ("tab", "film"):
                self.assertNotIn("плёнка застряла", render(s, d, Context()))
                self.assertNotIn("язычок оторвался", render(s, d, Context()))
        s = subject(topic="film", mechanism_kind="stuck_film")
        self.assertEqual(decide(s, Context()).template, "stuck_film")
        s = subject(topic="alignment", installation_result="crooked_via_box")
        self.assertEqual(decide(s, Context()).template, "installation_alignment")

    def test_scoped_facts_keep_exact_buyer_quotes_and_role_validation(self):
        text = "Комплект получил, после установки стекло утилизировал."
        data = {"wording_variant": 0, "facts": [{"issue_id": "a", "key": "glass_status", "value": "discarded", "evidence": [{"event_id": "b", "quote": text}]}]}
        self.assertEqual(validate_extraction(data, [Event("b", "buyer", text)])[0][0].value, "discarded")
        with self.assertRaisesRegex(ValueError, "seller/system"):
            validate_extraction(data, [Event("b", "seller", text)])
        data["facts"][0]["evidence"][0]["quote"] = "Придуманное отсутствие товара"
        with self.assertRaisesRegex(ValueError, "exact observed quote"):
            validate_extraction(data, [Event("b", "buyer", text)])


class MediaMeaningRegressions(unittest.TestCase):
    def test_unavailable_first_reply_honest_without_new_photo_or_approval(self):
        s = subject(topic="fracture", stage="installation", stage_basis="explicit_stage")
        s.observations.append(PhotoObservation("p", "a", "visible_glass_damage", "unavailable", "b", True))
        d = decide(s, Context())
        self.assertEqual((d.action, d.method), ("media_unavailable", "unknown"))
        text = render(s, d, Context())
        self.assertTrue(text.startswith("Здравствуйте. Очень жаль"))
        self.assertIn("содержание сейчас нельзя достоверно оценить", text)
        self.assertNotIn("Пришлите", text)
        self.assertNotIn("одобрена", text)
        self.assertEqual(s.issues["a"].counters, {})

    def test_relevant_unknown_is_not_suitable_and_respects_actual_photo_cap(self):
        s = subject(topic="film", mechanism_kind="stuck_film")
        s.observations.append(PhotoObservation("p", "a", "stuck_film", "unassessable", "b", True))
        d = decide(s, Context())
        self.assertEqual((d.action, d.template), ("request_photo", "photo_detail"))
        self.assertIn("бокса", render(s, d, Context()))
        self.assertNotIn("посторон", render(s, d, Context()))
        self.assertEqual(d.method, "unknown")
        s.issues["a"].counters["photo_requests"] = 2
        c = Context(claim=ClaimSnapshot(availability="absent"))
        d = decide(s, c)
        self.assertEqual(d.method, "return_goods")
        self.assertNotEqual(d.action, "request_photo")

    def test_historical_film_failure_cannot_be_disproved_by_later_damage(self):
        s = turn(issue_id="a", text="Во время установки плёнка порвалась.", topic="film", photo_assertion="past_event")
        request = photo_payload(s, "a", "stuck_film", Context())
        self.assertIsNone(request["buyer_assertion"])
        self.assertEqual(request["buyer_evidence"][0]["quote"], "Во время установки плёнка порвалась.")
        result, guard = validate_photo({"result": "contradiction", "visible_glass_damage": "supported"}, request)
        self.assertEqual(result, "unassessable")
        self.assertIsNotNone(guard)
        current = turn(issue_id="a", text="На снимке язычок остаётся целым.", topic="tab", photo_assertion="current_visible")
        request = photo_payload(current, "a", "torn_tab", Context())
        self.assertEqual(request["buyer_assertion"]["quote"], "На снимке язычок остаётся целым.")
        self.assertEqual(validate_photo({"result": "contradiction", "visible_glass_damage": "unknown"}, request)[0], "contradiction")

    def test_independent_visible_damage_requires_known_pre_use_stage(self):
        for stage in ("unknown", "installation"):
            s = subject(topic="film", stage=stage, stage_basis="explicit_stage")
            s.observations.append(PhotoObservation("p", "a", "stuck_film", "unassessable", "b", True))
            s.observations.append(PhotoObservation("p", "a", "visible_glass_damage", "suitable", "b", True))
            c = Context(claim=ClaimSnapshot(availability="absent"))
            d = decide(s, c)
            if stage == "unknown": self.assertEqual(d.template, "fracture_stage")
            else: self.assertEqual((d.rule, d.method), ("independent_glass_damage.evidence", "keep_goods"))
            self.assertFalse(d.operation)

    def test_closure_does_not_start_old_media_call(self):
        class Client:
            model, reasoning = "mock", "low"
            def structured(self, kind, prompt, payload, schema, image=None):
                if kind == "buyer_photo": raise AssertionError("No new vision task on closure")
                e = payload["actual_delta"][-1]
                values = {"topic": "edge", "advice_status": "tried_failed"} if e["event_id"] == "first" else {"buyer_intent": "acknowledgement", "substantive": "false"}
                data = {"facts": [{"issue_id": "a", "key": k, "value": v, "evidence": [{"event_id": e["event_id"], "quote": e["text"]}]} for k, v in values.items()], "wording_variant": 0}
                return {"data": data, "cache_key": e["event_id"], "accounting": {"input_tokens": 1, "output_tokens": 1}, "cost_usd": 0}
        record = {"schema_version": "wbc0115.dialogue.v1", "dialogue_id": "Synthetic", "split": "dev", "events": [{"event_id": "first", "role": "buyer", "text": "Край отходит, разглаживание не помогло", "attachments": []}, {"event_id": "last", "role": "buyer", "text": "Спасибо за информацию", "attachments": [{"kind": "image", "attachment_id": "p", "sha256": "a" * 64}]}]}
        settings = SimpleNamespace(allow_heldout=False, max_images_per_checkpoint=2, max_image_bytes=100, image_token_cap=20000)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReceiptLedger(Path(tmp) / "receipts.db", 10, 1)
            self.addCleanup(ledger.db.close)
            with patch("packages.domain.buyer_support_bot.replay.image_input", side_effect=AssertionError("No image read on closure")):
                rows = run_dialogue(record, Client(), ledger, {"p": {"sha256": "a" * 64}}, settings)
            self.assertEqual(rows[-1]["decision"]["action"], "silent")
            self.assertEqual(rows[-1]["media_checks"], [])
            self.assertEqual(rows[-1]["external_actions_executed"], 0)
            self.assertEqual(rows[-1]["prefix_end"], 2)
            self.assertNotIn("last", rows[0]["state"]["processed_events"])


class GroundedEmpathyRegressions(unittest.TestCase):
    def test_first_contact_concrete_empathy_only_for_a_known_problem(self):
        for values in ({"topic": "general", "problem_context": "installation"}, {"topic": "general", "problem_context": "complaint"}, {"topic": "scratch"}, {"topic": "delivery"}):
            s = subject(**values)
            d = decide(s, Context())
            reply = render(s, d, Context())
            self.assertTrue(reply.startswith("Здравствуйте. Очень жаль"))
            s.greeted = True
            self.assertNotIn("Здравствуйте", render(s, d, Context()))
        neutral = subject(topic="product", buyer_intent="question_pending")
        self.assertNotIn("жаль", render(neutral, decide(neutral, Context()), Context()))

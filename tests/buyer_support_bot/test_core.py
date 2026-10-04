import unittest
from packages.domain.buyer_support_bot import *
from packages.domain.buyer_support_bot.contracts import IssueState, OperationIntent
from packages.domain.buyer_support_bot.core import PHOTO_TASKS


def state(topic, **facts):
    return CaseState("synthetic", issues={"a": IssueState(facts={"topic": topic, **facts})})


def ctx(**kwargs):
    return Context(claim=ClaimSnapshot(availability="absent"), **kwargs)


def pending(actions=("approve1", "approve2", "rejectcustom"), **kwargs):
    return Context(claim=ClaimSnapshot(availability="present", claim_id="test-claim", status="pending", linked=True, fresh=True, source="authoritative_api", actions=actions), **kwargs)


def photo(s, result="suitable", task=None):
    s.observations.append(PhotoObservation("synthetic-photo", "a", task or PHOTO_TASKS[s.issues["a"].facts["topic"]], result, "synthetic-event", True))
    return s


class CoreScenarios(unittest.TestCase):
    def test_air_bubbles_help_first(self):
        s = state("bubbles")
        d = decide(s, ctx())
        self.assertEqual(d.action, "advise")
        self.assertNotIn("возврат", render(s, d, ctx()))
        self.assertIn("суток после установки", render(s, d, ctx()))

    def test_elapsed_installation_day_is_not_restarted(self):
        s = state("bubbles", installation_age="over_day")
        self.assertNotIn("суток", render(s, decide(s, ctx()), ctx()))

    def test_known_failed_tip_not_repeated(self):
        for topic in ("bubbles", "edge", "dust"):
            with self.subTest(topic=topic):
                s = state(topic, advice_status="tried_failed")
                self.assertEqual(decide(s, ctx()).action, "request_photo")

    def test_refusal_to_try_not_barrier(self):
        s = state("touch", stage="installation", advice_status="refused")
        d = decide(s, ctx())
        self.assertEqual((d.action, d.method), ("request_claim", "keep_goods"))
        self.assertNotIn("видео", render(s, d, ctx()))

    def test_dust_without_sticker_never_buys_replacement(self):
        s = state("dust", missing_sticker="true")
        self.assertEqual(decide(s, ctx()).action, "request_photo")
        self.assertNotIn("куп", render(s, decide(s, ctx()), ctx()).lower())

    def test_supplies_help_from_remaining_items(self):
        for option, word in (("microfibre", "микрофиброй"), ("wet_dry_wipes", "влажной"), ("own_soft_cloth", "имеющейся сухой")):
            s = state("supplies", cleaning_option=option)
            d = decide(s, ctx())
            self.assertEqual(d.action, "advise")
            self.assertIn(word, render(s, d, ctx()))
            self.assertNotIn("возврат", render(s, d, ctx()))

    def test_unknown_remaining_supplies_asks_not_guesses(self):
        d = decide(state("supplies"), ctx())
        self.assertEqual(d.template, "remaining_supplies")

    def test_tab_film_no_repair(self):
        for topic, phrase in (("tab", "язычка"), ("film", "застрявшей плёнкой")):
            s = state(topic)
            d = decide(s, ctx())
            self.assertEqual(d.action, "request_photo")
            self.assertIn(phrase, render(s, d, ctx()))
            self.assertNotIn("попробуйте", render(s, d, ctx()).lower())

    def test_pre_use_fracture_different_from_exploitation(self):
        for stage in ("before_use", "installation", "initial_inspection"):
            with self.subTest(stage=stage):
                self.assertEqual(decide(state("fracture", stage=stage), ctx()).action, "request_photo")
        d = decide(state("fracture", stage="in_use", return_requested="true"), ctx())
        self.assertEqual(d.rule, "post_use_fracture")
        self.assertEqual(d.action, "explain")

    def test_fracture_unknown_stage_asks_no_hour_threshold(self):
        s = state("fracture")
        d = decide(s, ctx())
        self.assertEqual(d.template, "fracture_stage")
        self.assertNotIn("час", render(s, d, ctx()))

    def test_post_use_unknown_cause_not_refund_uncertainty(self):
        s = state("fracture", stage="in_use", return_requested="true")
        s.issues["a"].counters["detail_requests"] = 1
        self.assertEqual(decide(s, pending()).operation, "rejectcustom")

    def test_no_unsolicited_refusal(self):
        d = decide(state("fracture", stage="in_use"), ctx())
        self.assertEqual(d.template, "protection")

    def test_photo_limit_shared_with_claim(self):
        s = state("tab")
        one = simulate(s, decide(s, ctx()), ctx(now="2026-10-04T00:00:00Z"))
        photo(one, "irrelevant")
        two = simulate(one, decide(one, pending()), pending())
        d = decide(two, pending())
        self.assertEqual((d.action, d.operation, d.method), ("prepare_operation", "approve2", "return_goods"))
        self.assertEqual(two.issues["a"].counters["photo_requests"], 2)

    def test_photo_unavailable_is_not_irrelevant_attempt(self):
        s = photo(state("tab"), "unavailable")
        d = decide(s, ctx())
        self.assertEqual(d.action, "media_unavailable")
        self.assertEqual(s.issues["a"].counters.get("photo_requests", 0), 0)

    def test_no_photo_possible_physical_not_refusal(self):
        d = decide(state("tab", cannot_photo="true"), pending())
        self.assertEqual(d.operation, "approve2")

    def test_suitable_chat_photo_survives_irrelevant_claim_photo(self):
        s = photo(state("tab"))
        s.observations.append(PhotoObservation("other", "a", "torn_tab", "irrelevant", "claim", True))
        d = decide(s, pending())
        self.assertEqual(d.operation, "approve1")

    def test_photo_presence_alone_not_evidence(self):
        s = state("tab")
        s.observations.append(PhotoObservation("meta", "a", "torn_tab", "suitable", "e", False))
        self.assertEqual(decide(s, ctx()).action, "request_photo")

    def test_claim_absent_unknown_error_distinct(self):
        s = photo(state("tab"))
        self.assertEqual(decide(s, ctx()).action, "request_claim")
        for availability in ("unknown", "error"):
            d = decide(s, Context(claim=ClaimSnapshot(availability=availability)))
            self.assertEqual(d.action, "restore_read")
            self.assertNotEqual(d.operation, "rejectcustom")

    def test_concrete_action_unavailable_never_substitutes(self):
        s = photo(state("tab"))
        d = decide(s, pending(actions=("approve2",)))
        self.assertEqual(d.action, "action_unavailable")
        self.assertEqual(d.method, "keep_goods")
        self.assertFalse(d.operation)

    def test_approved_status_authoritative_and_method(self):
        for method in ("keep_goods", "return_goods"):
            context = Context(claim=ClaimSnapshot(availability="present", claim_id="x", status="approved", linked=True, fresh=True, source="authoritative_api", return_method=method), return_discussed=True)
            d = decide(state("tab"), context)
            self.assertEqual(d.action, "confirmed_return")
            self.assertIn("Заявка одобрена", render(state("tab"), d, context))
        simulated = Context(claim=ClaimSnapshot(availability="present", status="approved", linked=True, fresh=True, source="simulation", return_method="keep_goods"), return_discussed=True)
        self.assertNotEqual(decide(state("tab"), simulated).action, "confirmed_return")

    def test_approved_no_unsolicited_notice(self):
        context = Context(claim=ClaimSnapshot(availability="present", status="approved", linked=True, fresh=True, source="authoritative_api", return_method="keep_goods"))
        self.assertEqual(decide(state("tab"), context).action, "silent")

    def test_unlinked_status_not_authoritative(self):
        context = Context(claim=ClaimSnapshot(availability="present", status="approved", linked=False, fresh=True, source="authoritative_api", return_method="keep_goods"), return_discussed=True)
        self.assertNotEqual(decide(state("tab"), context).action, "confirmed_return")

    def test_rejected_no_reconsideration(self):
        context = Context(claim=ClaimSnapshot(availability="present", status="rejected", linked=True, fresh=True, source="authoritative_api"), return_discussed=True)
        d = decide(photo(state("tab")), context)
        self.assertEqual(d.rule, "rejected_no_reconsideration")
        self.assertFalse(d.operation)
        self.assertEqual(d.template, "rejected_unknown")

    def test_return_before_claim_never_guarantees_keep_goods(self):
        s = photo(state("tab"))
        text = render(s, decide(s, ctx()), ctx())
        self.assertNotIn("сдавать", text)
        self.assertNotIn("одобрена", text)
        self.assertIn("описание и фотографии", text)

    def test_one_ground_not_every_issue_photo(self):
        s = state("touch", stage="installation", advice_status="tried_failed")
        s.issues["b"] = IssueState(facts={"topic": "tab"})
        d = decide(s, ctx())
        self.assertEqual((d.issue_id, d.action), ("a", "request_claim"))

    def test_compensation_kept_separate_and_open(self):
        s = state("touch", stage="installation", advice_status="tried_failed")
        s.issues["damage"] = IssueState(facts={"topic": "compensation", "compensation_materials": "true"})
        d = decide(s, ctx())
        self.assertEqual(d.action, "request_claim")
        self.assertIn("damage_final_process", d.secondary_unavailable)
        standalone = decide(state("compensation", compensation_materials="true"), ctx())
        self.assertEqual(standalone.action, "policy_unavailable")
        self.assertFalse(standalone.template)

    def test_legacy_promises_not_silently_cancelled(self):
        d = decide(state("general", historical_obligation="replacement_glass"), ctx())
        self.assertEqual(d.action, "policy_unavailable")

    def test_general_clarifies_once_then_physical(self):
        s = state("general")
        d = decide(s, ctx())
        self.assertEqual(d.template, "problem_detail")
        s = simulate(s, d, ctx())
        self.assertEqual(decide(s, pending()).operation, "approve2")

    def test_claim_without_chat_does_not_wait_unsent_question(self):
        d = decide(state("general"), pending(chat_available=False))
        self.assertEqual(d.operation, "approve2")

    def test_unknown_size_model_then_unknown_compatibility(self):
        self.assertEqual(decide(state("size"), ctx()).template, "phone_model")
        d = decide(state("size", phone_model="iPhone 17"), ctx())
        self.assertEqual(d.action, "data_unavailable")
        self.assertIn("verified_product_compatibility", d.unavailable)

    def test_buyer_selection_unused_physical_used_no_auto_approval(self):
        for stage, action in (("before_use", "request_claim"), ("installation", "explain"), ("in_use", "explain")):
            s = state("size", phone_model="iPhone 17", stage=stage, pristine="true")
            d = decide(s, ctx(compatibility="verified_mismatch", received_matches_order=True))
            self.assertEqual(d.action, action)
            if action == "request_claim":
                self.assertEqual(d.method, "return_goods")

    def test_privacy_property_not_guessed(self):
        self.assertEqual(decide(state("privacy", privacy_effect="absent_from_start"), ctx()).action, "data_unavailable")
        self.assertEqual(decide(state("privacy", privacy_effect="absent_from_start"), ctx(product_verified=True, product_line="privacy")).action, "request_claim")
        self.assertEqual(decide(state("privacy", privacy_effect="partial"), ctx(product_verified=True, product_line="privacy")).template, "privacy_partial")

    def test_late_device_issue_not_extended_early_rule(self):
        d = decide(state("touch", stage="in_use", advice_status="tried_failed"), ctx())
        self.assertEqual(d.action, "policy_unavailable")
        for topic in ("camera", "faceid"):
            d = decide(state(topic, stage="in_use", advice_status="tried_failed"), ctx())
            self.assertEqual(d.action, "request_claim")

    def test_review_requires_explicit_resolution_and_fresh_link(self):
        review = ReviewSnapshot(linked=True, fresh=True, negative=True, source="authoritative_api")
        self.assertNotEqual(decide(state("touch"), ctx(review=review)).action, "request_review")
        s = state("touch", resolved="true")
        self.assertEqual(decide(s, ctx(review=review)).action, "request_review")
        after = simulate(s, decide(s, ctx(review=review)), ctx(review=review))
        self.assertEqual(decide(after, ctx(review=review)).action, "complete")
        s.issues["b"] = IssueState(facts={"topic": "compensation"})
        self.assertNotEqual(decide(s, ctx(review=review)).action, "request_review")

    def test_no_response_without_claim_24h_no_message(self):
        s = state("general")
        s.issues["a"].waiting_since = "2026-10-03T00:00:00Z"
        d = decide(s, ctx(now="2026-10-04T00:00:00Z", timer_event=True))
        self.assertEqual(d.action, "no_response")
        self.assertFalse(render(s, d, ctx()))

    def test_claim_wait_deadline_unknown_is_open(self):
        s = state("general")
        s.issues["a"].waiting_since = "2026-10-03T00:00:00Z"
        d = decide(s, pending(now="2026-10-04T00:00:00Z", timer_event=True))
        self.assertEqual(d.rule, "claim_deadline_unknown")

    def test_unknown_operation_never_repeat(self):
        s = photo(state("tab"))
        s.operations["op"] = OperationIntent("op", "test-claim", "approve1", state="unknown")
        self.assertEqual(decide(s, pending()).action, "verify_operation")

    def test_simulated_intent_cannot_dispatch(self):
        s = photo(state("tab"))
        context = pending()
        s = simulate(s, decide(s, context), context)
        op = next(iter(s.operations.values()))
        self.assertEqual(op.state, "prepared")
        self.assertTrue(op.simulated)
        self.assertEqual(validate_prepared_intent(op, s, context), (False, "simulation_cannot_dispatch"))
        self.assertNotIn("confirmed", [o.state for o in s.operations.values()])

    def test_greeting_only_first_contact_and_neutral_no_sympathy(self):
        s = state("tab")
        text = render(s, decide(s, ctx()), ctx())
        self.assertTrue(text.startswith("Здравствуйте. Очень жаль"))
        s.greeted = True
        self.assertFalse(render(s, decide(s, ctx()), ctx()).startswith("Здравствуйте"))
        s = state("product")
        self.assertNotIn("Очень жаль", render(s, decide(s, ctx()), ctx()))

    def test_tone_does_not_change_return(self):
        s = photo(state("tab"))
        baseline = decide(s, pending())
        s.insult_count = 3
        s.last_substantive = False
        self.assertEqual(decide(s, pending()).operation, baseline.operation)
        self.assertFalse(render(s, baseline, pending()))

    def test_idempotent_event_and_changed_payload_error(self):
        event = Event("e", "buyer", "Стекло треснуло при установке")
        facts = [Fact("a", "topic", "fracture", (Evidence("e", "Стекло треснуло"),))]
        s = observe(CaseState("x"), event, facts)
        self.assertEqual(observe(s, event, facts).to_dict(), s.to_dict())
        with self.assertRaises(ValueError):
            observe(s, Event("e", "buyer", "изменено"), facts)

    def test_state_roundtrip(self):
        s = simulate(state("bubbles"), decide(state("bubbles"), ctx()), ctx())
        self.assertEqual(CaseState.from_dict(s.to_dict()).to_dict(), s.to_dict())

    def test_historical_counter_dedup(self):
        e = Event("s", "seller", "Пришлите фото")
        f = Fact("a", "photo_requested", "true", (Evidence("s", "Пришлите фото"),))
        s = observe(CaseState("x"), e, [f])
        s = observe(s, Event("b", "buyer", "кот"), [f])
        self.assertEqual(s.issues["a"].counters["photo_requests"], 1)

    def test_actual_historical_advice_not_repeated(self):
        s = state("bubbles")
        e = Event("s", "seller", "Приподнимите край")
        s = observe(s, e, [Fact("a", "advice_given", "bubbles", (Evidence("s", "Приподнимите край"),))])
        self.assertEqual(decide(s, ctx()).action, "wait_buyer")


if __name__ == "__main__":
    unittest.main()

class InheritedAndOpenScenarios(unittest.TestCase):
    def test_missing_glass_text_no_impossible_photo(self):
        d = decide(state("missing_glass"), ctx())
        self.assertEqual((d.action, d.method), ("request_claim", "keep_goods"))
        self.assertFalse(d.missing)

    def test_wrong_variant_opened_before_use_and_dangerous_edges(self):
        for topic in ("wrong_item", "opened_used", "dangerous_edge"):
            s = photo(state(topic))
            self.assertEqual(decide(s, pending()).operation, "approve1")

    def test_scratch_before_and_after_use(self):
        self.assertEqual(decide(state("scratch", stage="before_use"), ctx()).action, "request_photo")
        self.assertEqual(decide(state("scratch", stage="in_use", return_requested="true"), ctx()).template, "scratch_wear")

    def test_injury_returns_without_medical_claims(self):
        s = state("injury")
        d = decide(s, ctx())
        self.assertEqual(d.action, "request_claim")
        self.assertIn("не касайтесь осколков руками", render(s, d, ctx()))
        self.assertIn("injury_medical_compensation_not_bot", d.secondary_unavailable)

    def test_substantive_clarification_cap_is_explicit_open(self):
        s = state("fracture")
        d = decide(s, ctx())
        s = simulate(s, d, ctx())
        again = decide(s, ctx())
        self.assertEqual(again.action, "policy_unavailable")
        self.assertIn("substantive_clarification_limit", again.unavailable)
        self.assertNotEqual(again.operation, "approve1")

    def test_different_missing_fact_not_exhausted_by_model_question(self):
        s = state("size", phone_model="iPhone 17")
        s.issues["a"].counters["asked_phone_model"] = 1
        d = decide(s, ctx(compatibility="verified_mismatch", received_matches_order=True))
        self.assertEqual(d.template, "selection_condition")

    def test_explicit_correction_resolves_conflict_and_keeps_sources(self):
        s = state("fracture", stage="installation")
        s = observe(s, Event("b2", "buyer", "При использовании"), [Fact("a", "stage", "in_use", (Evidence("b2", "При использовании"),))])
        self.assertIn("stage", s.issues["a"].conflicts)
        s = observe(s, Event("b3", "buyer", "Уточняю, ошибся: это было при установке"), [Fact("a", "stage", "installation", (Evidence("b3", "при установке"),)), Fact("a", "correction", "stage", (Evidence("b3", "Уточняю, ошибся"),))])
        self.assertNotIn("stage", s.issues["a"].conflicts)
        self.assertEqual(s.issues["a"].facts["stage"], "installation")
        self.assertEqual(len(s.issues["a"].provenance["stage"]), 2)

    def test_approved_glass_does_not_resolve_separate_compensation(self):
        s = state("compensation", compensation_materials="true")
        c = Context(claim=ClaimSnapshot(availability="present", status="approved", linked=True, fresh=True, source="authoritative_api", return_method="keep_goods"))
        self.assertEqual(decide(s, c).action, "policy_unavailable")
        c = Context(claim=c.claim, return_discussed=True)
        self.assertIn("damage_final_process", decide(s, c).secondary_unavailable)

    def test_new_instruction_after_approval_is_helped(self):
        c = Context(claim=ClaimSnapshot(availability="present", status="approved", linked=True, fresh=True, source="authoritative_api", return_method="keep_goods"))
        self.assertEqual(decide(state("instruction"), c).template, "instruction")

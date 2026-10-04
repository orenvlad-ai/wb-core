"""Pure deterministic reducer and policy. Deliberately contains no action transport."""
from __future__ import annotations
import copy
import hashlib
import json
from .contracts import (CaseState, Context, COUNTER_KEYS, Decision, Event, Fact, IssueState,
                        OperationIntent, PhotoObservation, parsed_time)

# A known failure/refusal skips help only where this particular scenario permits return.
TIPS = {
    "bubbles": "bubbles", "dust": "dust", "edge": "edge", "supplies": "supplies",
    "privacy_dark": "brightness", "marks": "wipe", "touch": "touch_clean",
    "camera": "camera_clean", "faceid": "faceid_clean", "case": "without_case", "display": "display_clean",
}
PHOTO_TASKS = {
    "fracture": "visible_glass_damage", "tab": "torn_tab", "film": "stuck_film",
    "size": "label_or_fit", "alignment": "installation_alignment", "frame": "screen_overlap",
    "bubbles": "remaining_bubbles", "dust": "dust_under_glass", "edge": "unglued_edge",
    "opened_used": "opened_contents", "wrong_item": "wrong_variant", "scratch": "surface_scratch", "dangerous_edge": "dangerous_edge", "earpiece": "earpiece_overlap", "display": "display_artifact", "matte": "uneven_coating", "marks": "persistent_marks",
}
IMMUTABLE_CONFLICT_KEYS = {"stage", "phone_model", "privacy_effect", "fit_kind"}


def observe(state: CaseState, event: Event, facts: list[Fact], photos: tuple[PhotoObservation, ...] = ()) -> CaseState:
    """Ingest factual observations only, atomically in a caller's isolated store.

    Duplicate event -> no change. Same id/different payload -> error. Facts with old
    evidence may reappear from extraction but counters count evidence event once.
    """
    if event.role not in ("buyer", "seller", "system"):
        raise ValueError("unknown event role")
    digest = hashlib.sha256(json.dumps({"role": event.role, "text": event.text, "at": event.at, "attachments": event.attachments}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if event.event_id in state.processed_events:
        if state.processed_events[event.event_id] != digest:
            raise ValueError("event id reused with different content")
        return copy.deepcopy(state)
    if not event.event_id:
        raise ValueError("event id required")
    result = copy.deepcopy(state)
    result.processed_events[event.event_id] = digest
    result.revision += 1
    result.last_substantive = bool(event.text.strip())
    if event.role == "seller":
        result.greeted = True
    for fact in facts:
        issue = result.issues.setdefault(fact.issue_id, IssueState())
        old_sources = issue.provenance.setdefault(fact.key, [])
        known = {(item["event_id"], item["quote"], item["value"]) for item in old_sources}
        new_sources = [source for source in fact.evidence if (source.event_id, source.quote, fact.value) not in known]
        if not new_sources:
            continue
        old_value = issue.facts.get(fact.key)
        if fact.key in issue.conflicts and fact.value != "unknown":
            if fact.value not in issue.conflicts[fact.key]:
                issue.conflicts[fact.key].append(fact.value)
            issue.facts[fact.key] = "unknown"
        elif fact.key in IMMUTABLE_CONFLICT_KEYS and old_value not in (None, "unknown", fact.value) and fact.value != "unknown":
            values = issue.conflicts.setdefault(fact.key, [old_value])
            if fact.value not in values:
                values.append(fact.value)
            issue.facts[fact.key] = "unknown"
        elif fact.value != "unknown" or old_value is None:
            issue.facts[fact.key] = fact.value
        old_sources.extend({"event_id": source.event_id, "quote": source.quote, "value": fact.value} for source in new_sources)
        if fact.key == "detail_request_kind":
            counter = "asked_" + fact.value
            seen = {item["event_id"] for item in old_sources[:-len(new_sources)]}
            issue.counters[counter] = issue.counters.get(counter, 0) + len({source.event_id for source in new_sources} - seen)
        if fact.key == "advice_given":
            issue.advice_given.add(fact.value)
        if fact.key == "correction":
            issue.conflicts.pop(fact.value, None)
            sources = issue.provenance.get(fact.value, [])
            if sources:
                issue.facts[fact.value] = sources[-1]["value"]
        if fact.key in COUNTER_KEYS:
            before = {item["event_id"] for item in old_sources[:-len(new_sources)]}
            count = len({source.event_id for source in new_sources} - before)
            counter = COUNTER_KEYS[fact.key]
            issue.counters[counter] = issue.counters.get(counter, 0) + count
        for fact_key, attr in (("greeted", "greeted"), ("review_requested", "review_requested"), ("claim_number_requested", "claim_number_requested"), ("claim_location_explained", "claim_location_explained")):
            if fact.key == fact_key and fact.value == "true":
                setattr(result, attr, True)
        if fact.key == "direct_insult" and fact.value == "true":
            already_counted = {item["event_id"] for item in old_sources[:-len(new_sources)]}
            result.insult_count += len({source.event_id for source in new_sources} - already_counted)
        if fact.key == "substantive":
            result.last_substantive = fact.value == "true"
    for photo in photos:
        if photo.result not in ("suitable", "irrelevant", "contradiction", "unassessable", "unavailable"):
            raise ValueError("invalid photo observation")
        if photo.evidence_event_id not in result.processed_events:
            raise ValueError("photo evidence event not observed")
        if photo not in result.observations:
            result.observations.append(photo)
    if event.role == "buyer" and result.phase == "no_response":
        result.phase = "received"
    return result


def _photo(state: CaseState, issue_id: str, task: str) -> str:
    photos = [item for item in state.observations if item.issue_id == issue_id and item.task == task and item.checked]
    if any(item.result == "contradiction" for item in photos):
        return "contradiction"
    if any(item.result == "suitable" for item in photos):
        return "suitable"
    if any(item.result == "irrelevant" for item in photos):
        return "irrelevant"
    if any(item.result in ("unavailable", "unassessable") for item in photos):
        return "technical_unknown"
    return "none"


def _decision(action: str, rule: str, issue_id: str = "", **kwargs) -> Decision:
    return Decision(action, rule, issue_id, **kwargs)


def _return(state: CaseState, context: Context, issue_id: str, method: str, rule: str) -> Decision:
    claim = context.claim
    reported = any(item.facts.get("claim_reported") == "true" for item in state.issues.values())
    supplied_number = any(item.facts.get("claim_number") for item in state.issues.values())
    if claim.availability in ("absent", "unknown") and reported and not claim.linked:
        if not state.claim_number_requested and not supplied_number:
            return _decision("request_claim_number", "reported_claim_unlinked", issue_id, method=method, template="claim_number")
        if claim.availability == "absent" and not state.claim_location_explained:
            return _decision("explain_claim_location", "reported_claim_not_found", issue_id, method=method, template="claim_location")
        return _decision("restore_read", "reported_claim_link_check", issue_id, method=method, unavailable=("purchase_link",))
    if claim.availability == "absent":
        return _decision("request_claim", rule, issue_id, method=method, template="request_claim")
    if claim.availability in ("unknown", "error"):
        return _decision("restore_read", rule, issue_id, method=method, template="status_unknown", unavailable=("claim_status",))
    if not claim.linked:
        if not state.claim_number_requested:
            return _decision("request_claim_number", "claim_link_unverified", issue_id, method=method, template="claim_number")
        if not state.claim_location_explained:
            return _decision("explain_claim_location", "claim_link_unverified", issue_id, method=method, template="claim_location")
        return _decision("restore_read", "claim_link_unverified", issue_id, method=method, unavailable=("purchase_link",))
    if not claim.fresh or claim.source not in ("authoritative_api", "simulation") or claim.status != "pending":
        return _decision("restore_read", "claim_freshness_required", issue_id, method=method, unavailable=("fresh_pending_claim",))
    action = "approve2" if method == "return_goods" else "approve1"
    if action not in claim.actions:
        return _decision("action_unavailable", rule, issue_id, method=method, unavailable=(action,), template="status_unknown")
    return _decision("prepare_operation", rule, issue_id, method=method, operation=action, template="prepared")


def _ask_or_physical(state: CaseState, context: Context, issue_id: str, key: str, template: str, rule: str) -> Decision:
    issue = state.issues[issue_id]
    count = issue.counters.get(key, 0) if key == "contradiction_requests" else issue.counters.get("asked_" + template, 0)
    if template == "problem_detail":
        count = max(count, issue.counters.get("detail_requests", 0))
    if not context.chat_available or (count >= 1 and (key == "contradiction_requests" or template == "problem_detail")):
        return _return(state, context, issue_id, "return_goods", rule + ".residual_uncertainty")
    if count >= 1:
        return _decision("policy_unavailable", rule + ".clarification_limit_open", issue_id, unavailable=("substantive_clarification_limit",))
    return _decision("clarify", rule, issue_id, missing=("asked_" + template if key != "contradiction_requests" else key,), template=template)


def _with_photo(state: CaseState, context: Context, issue_id: str, topic: str, rule: str) -> Decision:
    issue = state.issues[issue_id]
    task = PHOTO_TASKS[topic]
    photo = _photo(state, issue_id, task)
    if photo == "suitable":
        return _return(state, context, issue_id, "keep_goods", rule + ".evidence")
    if photo == "contradiction" or issue.conflicts:
        return _ask_or_physical(state, context, issue_id, "contradiction_requests", "contradiction", rule)
    if issue.facts.get("cannot_photo") == "true" or issue.counters.get("photo_requests", 0) >= 2 or not context.chat_available:
        return _return(state, context, issue_id, "return_goods", rule + ".photo_missing")
    if photo == "technical_unknown":
        return _decision("media_unavailable", rule, issue_id, missing=(task,), unavailable=("media_analysis",))
    return _decision("request_photo", rule, issue_id, missing=(task,), template=task)


def _refuse(state: CaseState, context: Context, issue_id: str, rule: str, template: str) -> Decision:
    issue = state.issues[issue_id]
    count = issue.counters.get("refusal_replies", 0)
    if count >= 2 and not state.last_substantive:
        return _decision("silent", rule + ".repeat_limit", issue_id)
    claim = context.claim
    # A substantive new topic can be helped without reconsidering an existing refusal.
    if claim.availability == "present" and claim.linked and claim.status == "pending":
        if not claim.fresh or claim.source not in ("authoritative_api", "simulation"):
            return _decision("restore_read", rule, issue_id, template=template, unavailable=("fresh_claim",))
        if "rejectcustom" not in claim.actions:
            return _decision("action_unavailable", rule, issue_id, template=template, unavailable=("rejectcustom",))
        return _decision("prepare_operation", rule, issue_id, template=template, operation="rejectcustom")
    return _decision("explain", rule, issue_id, template="fracture_objection" if count else template)


def _subject(state: CaseState, context: Context, issue_id: str) -> Decision:
    issue = state.issues[issue_id]
    f = issue.facts
    topic = f.get("topic", "general")
    tried = f.get("advice_status") in ("tried_failed", "refused")
    if f.get("historical_obligation") in ("replacement_glass", "compensation"):
        return _decision("policy_unavailable", "legacy_obligation", issue_id, unavailable=("legacy_obligation_resolution",))
    if topic in ("delivery", "payment"):
        return _decision("explain", "wb_platform_boundary", issue_id, template=topic)
    if topic == "compensation":
        return _decision("policy_unavailable", "phone_damage_separate", issue_id, template="compensation" if f.get("compensation_materials") != "true" else "", unavailable=("damage_final_process",))
    if topic == "giveaway":
        if issue.last_action == "giveaway" and not state.last_substantive:
            return _decision("silent", "giveaway_repeat", issue_id)
        return _decision("explain", "giveaway_temporary_help", issue_id, template="giveaway")
    if issue.conflicts:
        return _ask_or_physical(state, context, issue_id, "contradiction_requests", "contradiction", "fact_conflict")
    if topic in ("general", "other"):
        return _ask_or_physical(state, context, issue_id, "detail_requests", "problem_detail", "general_complaint")
    if topic == "fracture":
        stage = f.get("stage", "unknown")
        if stage == "unknown":
            return _ask_or_physical(state, context, issue_id, "detail_requests", "fracture_stage", "fracture_stage_required")
        if stage == "in_use":
            return _refuse(state, context, issue_id, "post_use_fracture", "fracture_use") if f.get("return_requested") == "true" else _decision("explain", "post_use_fracture", issue_id, template="protection")
        return _with_photo(state, context, issue_id, topic, "pre_use_fracture")
    if topic == "missing_glass":
        return _return(state, context, issue_id, "keep_goods", "missing_glass_return")
    if topic == "instruction":
        return _decision("explain", "installation_instruction", issue_id, template="instruction")
    if topic == "injury":
        decision = _return(state, context, issue_id, "keep_goods", "injury_return")
        return Decision(**{**decision.__dict__, "secondary_unavailable": ("injury_medical_compensation_not_bot",)})
    if topic in ("opened_used", "wrong_item"):
        return _with_photo(state, context, issue_id, topic, topic + "_return")
    if topic == "scratch":
        if f.get("stage", "unknown") == "unknown":
            return _ask_or_physical(state, context, issue_id, "detail_requests", "onset", "scratch_stage")
        if f.get("stage") == "in_use":
            return _refuse(state, context, issue_id, "scratch_in_use", "scratch_wear") if f.get("return_requested") == "true" else _decision("explain", "scratch_in_use", issue_id, template="scratch_wear")
        return _with_photo(state, context, issue_id, topic, "scratch_before_use")
    if topic == "dangerous_edge":
        if f.get("edge_kind") == "subjective":
            return _decision("explain", "edge_discomfort", issue_id, template="edge_discomfort")
        return _with_photo(state, context, issue_id, topic, "dangerous_edge_return")
    if topic in ("tab", "film", "alignment"):
        return _with_photo(state, context, issue_id, topic, topic + "_return")
    if topic == "earpiece":
        if context.compatibility == "unknown":
            return _decision("data_unavailable", "earpiece_model_unknown", issue_id, unavailable=("verified_product_compatibility",))
        if context.compatibility == "verified_match" and f.get("advice_status") in ("tried_failed", "refused"):
            return _with_photo(state, context, issue_id, topic, "earpiece_overlap")
        if "earpiece_check" in issue.advice_given:
            return _decision("wait_buyer", "earpiece_advice_pending", issue_id)
        return _decision("advise", "earpiece_check", issue_id, template="earpiece_check")
    if topic == "size" or (topic == "frame" and f.get("fit_kind") == "content_overlap"):
        if not f.get("phone_model"):
            return _ask_or_physical(state, context, issue_id, "detail_requests", "phone_model", "size_phone_required")
        if context.compatibility == "unknown":
            return _decision("data_unavailable", "compatibility_unknown", issue_id, unavailable=("verified_product_compatibility",))
        if context.compatibility == "verified_mismatch" and context.received_matches_order is True:
            if f.get("stage") == "before_use" and f.get("pristine") == "true":
                return _return(state, context, issue_id, "return_goods", "buyer_selection_unused")
            if f.get("stage") in ("installation", "initial_inspection", "in_use"):
                return _refuse(state, context, issue_id, "buyer_selection_used", "selection_used")
            return _ask_or_physical(state, context, issue_id, "detail_requests", "selection_condition", "buyer_selection_condition")
        if context.compatibility == "verified_mismatch" and context.received_matches_order is None:
            return _decision("data_unavailable", "received_variant_unknown", issue_id, unavailable=("received_matches_order",))
        if f.get("fit_kind") == "normal_gap":
            return _decision("explain", "normal_case_gap", issue_id, template="normal_gap")
        return _with_photo(state, context, issue_id, topic, "fit_return")
    if topic == "frame" and f.get("fit_kind") == "subjective_frame":
        return _decision("explain", "frame_preference", issue_id, template="frame")
    if topic in ("privacy", "privacy_dark", "matte"):
        wanted = "matte" if topic == "matte" else "privacy"
        if not context.product_verified or context.product_line != wanted:
            return _decision("data_unavailable", "property_line_unknown", issue_id, unavailable=("verified_product_line",))
        if topic == "privacy":
            if f.get("privacy_effect") == "partial":
                return _decision("explain", "privacy_normal_partial", issue_id, template="privacy_partial")
            if f.get("privacy_effect") == "absent_from_start":
                return _return(state, context, issue_id, "keep_goods", "privacy_absent")
            return _ask_or_physical(state, context, issue_id, "detail_requests", "privacy_manifestation", "privacy_manifestation")
        if topic == "matte":
            if f.get("coating_kind") == "light_grain":
                return _decision("explain", "matte_normal_grain", issue_id, template="matte_grain")
            if f.get("coating_kind") == "absent":
                return _return(state, context, issue_id, "keep_goods", "matte_absent")
            if f.get("coating_kind") == "uneven":
                return _with_photo(state, context, issue_id, topic, "matte_uneven")
            return _ask_or_physical(state, context, issue_id, "detail_requests", "coating_manifestation", "matte_manifestation")
    if topic == "dust" and f.get("missing_sticker") == "true":
        return _with_photo(state, context, issue_id, topic, "dust_no_sticker")
    if topic in ("touch", "camera", "faceid", "marks") and f.get("stage", "unknown") == "unknown":
        return _ask_or_physical(state, context, issue_id, "detail_requests", "onset", topic + "_onset")
    if topic == "touch" and f.get("stage") == "in_use":
        return _decision("policy_unavailable", topic + "_late", issue_id, unavailable=("late_device_issue_rule",))
    if topic == "marks":
        if f.get("marks_kind") == "wipeable":
            return _decision("explain", "fingerprints_normal", issue_id, template="fingerprints")
        if f.get("marks_kind") == "late_wear":
            return _decision("policy_unavailable", "marks_late_wear", issue_id, unavailable=("late_marks_rule",))
    if topic == "display" and f.get("display_kind") == "subjective_discomfort":
        return _decision("data_unavailable", "display_subjective_property", issue_id, unavailable=("verified_product_line",))
    if topic in TIPS:
        tip = TIPS[topic]
        if topic == "supplies" and not tried and f.get("cleaning_option", "unknown") == "unknown":
            return _ask_or_physical(state, context, issue_id, "detail_requests", "remaining_supplies", "supplies_available")
        if not tried and tip not in issue.advice_given:
            return _decision("advise", topic + "_help_first", issue_id, template=tip)
        if not tried:
            return _decision("wait_buyer", topic + "_advice_pending", issue_id)
        if topic in ("privacy_dark", "case"):
            return _decision("explain", topic + "_normal_feature", issue_id, template="privacy_dark" if topic == "privacy_dark" else "case_conflict")
        if topic in ("touch", "camera", "faceid", "supplies"):
            return _return(state, context, issue_id, "keep_goods", topic + "_persistent")
        if topic == "display" and f.get("display_kind") != "persistent_artifact":
            return _ask_or_physical(state, context, issue_id, "detail_requests", "coating_manifestation", "display_manifestation")
        if topic == "marks" and f.get("marks_kind") != "persistent_from_start":
            return _ask_or_physical(state, context, issue_id, "detail_requests", "marks_manifestation", "marks_manifestation")
        return _with_photo(state, context, issue_id, topic, topic + "_persistent")
    if topic == "product":
        return _decision("data_unavailable", "product_answer_grounding", issue_id, unavailable=("product_answer_contract",))
    return _decision("policy_unavailable", "unmapped_subject", issue_id, unavailable=("subject_rule",))


def decide(state: CaseState, context: Context) -> Decision:
    """Select next intent, without mutating state or asserting an external result."""
    claim = context.claim
    uncertain = [op for op in state.operations.values() if op.state in ("dispatching", "unknown")]
    if uncertain:
        return _decision("verify_operation", "unknown_operation_no_resend", template="status_unknown", unavailable=("operation_result",))
    if claim.availability == "present" and claim.linked and claim.fresh and claim.source == "authoritative_api":
        if claim.status == "approved":
            if not context.return_discussed:
                independent = [(key, issue) for key, issue in state.issues.items() if issue.facts.get("topic") in ("product", "instruction", "delivery", "payment", "giveaway", "compensation")]
                if independent:
                    return _subject(state, context, independent[0][0])
                return _decision("silent", "approved_no_unsolicited_notice")
            if claim.return_method == "unknown":
                return _decision("restore_read", "approved_method_unknown", unavailable=("confirmed_return_method",))
            separate = tuple("damage_final_process" for item in state.issues.values() if item.facts.get("topic") == "compensation")
            return _decision("confirmed_return", "authoritative_approved", method=claim.return_method, template="confirmed_" + claim.return_method, secondary_unavailable=separate)
        if claim.status == "rejected" and context.return_discussed:
            return _decision("explain", "rejected_no_reconsideration", template="rejected_known" if claim.rejection_reason else "rejected_unknown")
    active = {key: value for key, value in state.issues.items() if value.facts.get("resolved") != "true"}
    for issue_id, issue in state.issues.items():
        if issue.facts.get("resolved") == "true" and not active:
            review = context.review
            if review.linked and review.fresh and review.source == "authoritative_api" and review.negative is True and not state.review_requested:
                return _decision("request_review", "resolved_negative_review", issue_id, template="review")
            return _decision("complete", "explicit_resolution", issue_id, template="resolved")
    if context.timer_event:
        waiting = [item for item in state.issues.values() if item.waiting_since]
        if not waiting or not context.now:
            return _decision("wait_buyer", "no_wait_clock")
        earliest = min(parsed_time(item.waiting_since) for item in waiting)
        seconds = (parsed_time(context.now) - earliest).total_seconds()
        if claim.availability == "absent":
            return _decision("no_response", "chat_timeout_no_reminder") if seconds >= 86400 else _decision("wait_buyer", "chat_wait_24h")
        if not claim.deadline_verified or not claim.deadline_at or claim.safety_margin_seconds is None:
            return _decision("data_unavailable", "claim_deadline_unknown", unavailable=("verified_deadline_and_margin",))
        deadline_due = parsed_time(context.now).timestamp() >= parsed_time(claim.deadline_at).timestamp() - claim.safety_margin_seconds
        if seconds < 86400 and not deadline_due:
            return _decision("wait_buyer", "claim_wait_window")
        # Decide from available facts after the bounded wait, without another question.
        context = Context(**{**context.__dict__, "chat_available": False, "timer_event": False})
    if not state.issues:
        return _decision("clarify", "unknown_problem", "general", missing=("detail_requests",), template="problem_detail")
    candidates = [_subject(state, context, issue_id) for issue_id in active]
    returns = [item for item in candidates if item.method in ("keep_goods", "return_goods")]
    if returns:
        # One sufficient ground: prefer established keep-goods evidence; do not ask
        # other issues solely to reach the same glass refund.
        result = next((item for item in returns if item.method == "keep_goods"), returns[0])
    else:
        result = next((item for item in candidates if item.action not in ("silent", "policy_unavailable", "data_unavailable")), candidates[0])
    secondary = tuple(sorted(set(result.secondary_unavailable) | {gap for item in candidates if item.issue_id != result.issue_id for gap in item.unavailable}))
    used = tuple(sorted(state.issues.get(result.issue_id, IssueState()).facts))
    return Decision(**{**result.__dict__, "facts_used": used, "secondary_unavailable": secondary})


def simulate(state: CaseState, decision: Decision, context: Context) -> CaseState:
    """Apply a *simulated* candidate response. Never use for historical replay.

    No intent is dispatched and no simulation becomes real approval.
    """
    result = copy.deepcopy(state)
    issue = result.issues.setdefault(decision.issue_id or "general", IssueState())
    if decision.action == "silent":
        return result
    result.greeted = True
    issue.last_action = decision.template
    result.revision += 1
    if decision.action == "advise":
        issue.advice_given.add(decision.template)
        result.phase = "advised"
    if decision.action in ("clarify", "request_photo", "advise", "wait_buyer"):
        result.phase = "waiting_buyer"
        if not issue.waiting_since:
            issue.waiting_since = context.now
    if decision.action == "request_photo":
        issue.counters["photo_requests"] = issue.counters.get("photo_requests", 0) + 1
    if decision.action == "clarify":
        counter = decision.missing[0] if decision.missing else "detail_requests"
        issue.counters[counter] = issue.counters.get(counter, 0) + 1
    if decision.action == "request_claim_number":
        result.claim_number_requested = True
    if decision.action == "explain_claim_location":
        result.claim_location_explained = True
    if decision.action == "request_review":
        result.review_requested = True
    if decision.rule.startswith(("post_use_fracture", "buyer_selection_used", "scratch_in_use")) and decision.template:
        issue.counters["refusal_replies"] = issue.counters.get("refusal_replies", 0) + 1
    if decision.action == "prepare_operation":
        seed = f"{result.case_id}:{context.claim.claim_id}:{decision.operation}"
        operation_id = hashlib.sha256(seed.encode()).hexdigest()
        if not any(op.claim_id == context.claim.claim_id for op in result.operations.values()):
            result.operations[operation_id] = OperationIntent(operation_id, context.claim.claim_id, decision.operation, case_revision=result.revision)
        result.phase = "decision_prepared"
    if decision.action == "no_response":
        result.phase = "no_response"
    if decision.action == "complete":
        result.phase = "completed"
    return result


def validate_prepared_intent(intent: OperationIntent, state: CaseState, context: Context) -> tuple[bool, str]:
    """Reusable production preflight contract, still no transport capability."""
    if intent.state != "prepared":
        return False, "read_result_only"
    if intent.case_revision != state.revision:
        return False, "stale_case_revision"
    claim = context.claim
    if not claim.linked or not claim.fresh or claim.source != "authoritative_api" or claim.claim_id != intent.claim_id or claim.status != intent.expected_claim_status:
        return False, "fresh_linked_target_required"
    if intent.action not in claim.actions:
        return False, "action_unavailable"
    if intent.simulated:
        return False, "simulation_cannot_dispatch"
    return True, "ready_for_external_owner"

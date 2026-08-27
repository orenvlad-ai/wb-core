"""Versioned server owner-policy above the immutable WB bundle v1.4.2."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping, Sequence


OWNER_POLICY_VERSION = "owner-policy-2026-08-08-v5"
OWNER_POLICY_CONTRACT = "wb_autoanswers_owner_policy_v1"
OWNER_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "wb_autoanswers_owner_policy_v1.json"
)
OWNER_POLICY_UNSAFE_PUBLIC_REPLY_CODE = "owner_policy_unsafe_public_reply"


class OwnerPolicyUnsafePublicReplyError(RuntimeError):
    """Typed semantic refusal for one composed public reply.

    Configuration, identity and template failures intentionally remain ordinary
    ``RuntimeError`` instances so callers cannot mistake an invariant failure
    for a safely terminalizable per-review outcome.
    """

    code = OWNER_POLICY_UNSAFE_PUBLIC_REPLY_CODE

    def __init__(self, message: str, *, evidence: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.evidence = dict(evidence)


@lru_cache(maxsize=1)
def _policy() -> dict[str, Any]:
    value = json.loads(OWNER_POLICY_PATH.read_text(encoding="utf-8"))
    if (
        value.get("contract") != OWNER_POLICY_CONTRACT
        or value.get("policy_version") != OWNER_POLICY_VERSION
        or value.get("source_bundle_version") != "1.4.2"
        or value.get("route") != "public_only"
        or value.get("openai_calls") != 0
    ):
        raise RuntimeError("WB Autoanswers owner-policy identity mismatch")
    return value


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _tokens(value: Any) -> list[str]:
    return _normalize(value).split()


def _has_phrase(text: str, phrases: Sequence[Any]) -> bool:
    padded = f" {_normalize(text)} "
    return any(f" {_normalize(phrase)} " in padded for phrase in phrases if _normalize(phrase))


def _has_stem(tokens: Sequence[str], stems: Sequence[Any]) -> bool:
    normalized = tuple(_normalize(stem) for stem in stems if _normalize(stem))
    return any(token.startswith(normalized) for token in tokens) if normalized else False


def _content_text(content_json: Any) -> str:
    if isinstance(content_json, Mapping):
        content = dict(content_json)
    else:
        try:
            content = json.loads(str(content_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            content = {}
    if not isinstance(content, Mapping):
        return ""
    surfaces: list[str] = [str(content.get(key) or "") for key in ("text", "pros", "cons")]
    tags = content.get("tags")
    if isinstance(tags, list):
        surfaces.extend(str(item or "") for item in tags)
    elif tags:
        surfaces.append(str(tags))
    return ". ".join(part.strip() for part in surfaces if part.strip())


def _clauses(text: str) -> list[str]:
    return [part for part in re.split(r"[.!?;:\n]+", str(text or "")) if _normalize(part)]


def _same_clause_stems(text: str, left: Sequence[Any], right: Sequence[Any]) -> bool:
    return any(
        _has_stem(_tokens(clause), left) and _has_stem(_tokens(clause), right)
        for clause in _clauses(text)
    )


def _same_clause_stem_phrase(text: str, stems: Sequence[Any], phrases: Sequence[Any]) -> bool:
    return any(
        _has_stem(_tokens(clause), stems) and _has_phrase(clause, phrases)
        for clause in _clauses(text)
    )


def _has_negated_stem(words: Sequence[str], stems: Sequence[Any]) -> bool:
    for index, word in enumerate(words):
        if _has_stem([word], stems) and "не" in words[max(0, index - 3) : index]:
            return True
    return False


def _phrase_ranges(words: Sequence[str], phrases: Sequence[Any]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for phrase in phrases:
        phrase_words = _tokens(phrase)
        if not phrase_words:
            continue
        for index in range(0, len(words) - len(phrase_words) + 1):
            if list(words[index : index + len(phrase_words)]) == phrase_words:
                ranges.append((index, index + len(phrase_words) - 1))
    return ranges


def _persistent_visual(text: str, signals: Mapping[str, Any]) -> bool:
    for clause in _clauses(text):
        words = _tokens(clause)
        if not _has_stem(words, signals.get("persistent_visual_stems") or []):
            continue
        if _has_stem(words, ["салфет", "тряпк"]) and not _has_stem(
            words, ["экран", "покрыт"]
        ):
            continue
        if (
            _has_stem(words, signals.get("persistence_stems") or [])
            or _has_negated_stem(words, signals.get("persistence_negative_stems") or [])
            or _has_phrase(clause, signals.get("persistence_phrases") or [])
            or _has_stem(words, ["полос", "пятн", "полосил"])
        ):
            return True
    return False


def _sensor_failure(text: str, signals: Mapping[str, Any]) -> bool:
    if _has_phrase(text, signals.get("sensor_phrases") or []):
        return True
    for clause in _clauses(text):
        words = _tokens(clause)
        if not _has_stem(words, signals.get("sensor_subject_stems") or []):
            continue
        if _has_stem(words, signals.get("sensor_degradation_stems") or []) or _has_negated_stem(
            words, signals.get("sensor_negative_stems") or []
        ):
            return True
    return False


def _arrival_damage(text: str, signals: Mapping[str, Any]) -> bool:
    for clause in _clauses(text):
        words = _tokens(clause)
        arrival_indexes = [
            index
            for index, word in enumerate(words)
            if _has_stem([word], signals.get("arrival_stems") or [])
        ]
        damage_indexes = [
            index
            for index, word in enumerate(words)
            if _has_stem([word], signals.get("arrival_damage_stems") or [])
        ]
        if _has_phrase(clause, signals.get("arrival_phrases") or []) and _has_stem(
            words, signals.get("arrival_damage_stems") or []
        ):
            return True
        product_indexes = [
            index
            for index, word in enumerate(words)
            if _has_stem([word], signals.get("arrival_product_stems") or [])
        ]
        for arrival_index in arrival_indexes:
            arrival_word = words[arrival_index]
            for damage_index in damage_indexes:
                if abs(arrival_index - damage_index) > 3:
                    continue
                if arrival_word.startswith("получ"):
                    previous_is_device = arrival_index > 0 and _has_stem(
                        [words[arrival_index - 1]], ["телефон", "экран", "стекл"]
                    )
                    product_nearby = any(
                        abs(arrival_index - product_index) <= 4
                        for product_index in product_indexes
                    )
                    if previous_is_device or not product_nearby:
                        continue
                return True
    return False


def _opened_or_incomplete(text: str, signals: Mapping[str, Any]) -> bool:
    if _has_phrase(text, signals.get("missing_phrases") or []):
        return True
    for clause in _clauses(text):
        words = _tokens(clause)
        if _has_stem(words, signals.get("packaging_stems") or []) and _has_stem(
            words, signals.get("opened_stems") or []
        ):
            return True
        component_indexes = [
            index
            for index, word in enumerate(words)
            if _has_stem([word], signals.get("kit_component_stems") or [])
        ]
        for _start, end in _phrase_ranges(words, signals.get("missing_leads") or []):
            if any(end < index <= end + 2 for index in component_indexes):
                return True
    return False


def _wrong_item_or_variant(text: str, signals: Mapping[str, Any]) -> bool:
    if _has_phrase(text, signals.get("wrong_variant_phrases") or []):
        return True
    words = _tokens(text)
    if all(_has_stem(words, [stem]) for stem in signals.get("wrong_item_stems") or []):
        return True
    if _has_stem(words, signals.get("matte_stems") or []) and _has_stem(
        words, signals.get("gloss_stems") or []
    ) and (
        _has_stem(words, signals.get("model_order_stems") or [])
        or _has_stem(words, signals.get("model_arrival_stems") or [])
        or "вместо" in words
    ):
        return True
    if _has_stem(words, signals.get("wrong_insert_stems") or []) and _has_stem(
        words, signals.get("wrong_insert_failure_stems") or []
    ):
        return True
    numbers = {
        match.group(1)
        for match in re.finditer(r"\b(1[0-9]|2[0-9])(?:про|pro)?\b", _normalize(text))
    }
    return bool(
        len(numbers) >= 2
        and _has_stem(words, signals.get("model_order_stems") or [])
        and _has_stem(words, signals.get("model_arrival_stems") or [])
    )


def _fit_mismatch(text: str, signals: Mapping[str, Any]) -> bool:
    if _has_phrase(text, signals.get("fit_phrases") or []):
        return True
    for clause in _clauses(text):
        words = _tokens(clause)
        subject = _has_stem(words, signals.get("fit_subject_stems") or [])
        direct_failure = _has_negated_stem(
            words, signals.get("fit_failure_stems") or []
        ) or _has_stem(words, ["перекрыв"])
        size_indexes = [
            index
            for index, word in enumerate(words)
            if _has_stem([word], signals.get("fit_size_stems") or [])
        ]
        reference_indexes = [
            index
            for index, word in enumerate(words)
            if _has_stem([word], signals.get("fit_reference_stems") or [])
        ]
        temporal_indexes = [
            index
            for index, word in enumerate(words)
            if _has_stem([word], ["дн", "сут", "час", "недел", "месяц"])
        ]
        size_mismatch = any(
            abs(size_index - reference_index) <= 4
            and not any(abs(size_index - temporal_index) <= 2 for temporal_index in temporal_indexes)
            for size_index in size_indexes
            for reference_index in reference_indexes
        )
        missing_cutout = _has_stem(words, ["вырез"]) and _has_phrase(
            clause, ["нет", "нету", "не было"]
        )
        measured_gap = _has_stem(words, ["расстояни", "зазор"]) and "мм" in words
        if subject and (direct_failure or size_mismatch or missing_cutout or measured_gap):
            return True
    return False


def _explicit_positive_impact(text: str, signals: Mapping[str, Any]) -> bool:
    impact_stems = signals.get("impact_stems") or []
    negations = {_normalize(item) for item in signals.get("impact_negations") or []}
    for clause in _clauses(text):
        words = _tokens(clause)
        for index, word in enumerate(words):
            if not _has_stem([word], impact_stems):
                continue
            if any(token in negations for token in words[max(0, index - 3) : index]):
                continue
            return True
    return False


def classify_return_guard(content_json: Any) -> dict[str, Any]:
    """Return deterministic semantic evidence, not a single keyword match."""

    policy = _policy()
    signals = dict(policy.get("semantic_signals") or {})
    text = _content_text(content_json)
    normalized = _normalize(text)
    words = _tokens(text)
    reasons: list[str] = []

    breakage = _has_stem(words, signals.get("breakage_stems") or [])
    if _arrival_damage(text, signals):
        reasons.append("received_or_pre_use_damage")

    if _same_clause_stems(
        text,
        signals.get("pre_use_component_stems") or [],
        signals.get("pre_use_damage_stems") or [],
    ):
        reasons.append("pre_use_component_damage")
    if _opened_or_incomplete(text, signals):
        reasons.append("opened_or_incomplete")
    if _wrong_item_or_variant(text, signals):
        reasons.append("wrong_item_or_variant")
    if _fit_mismatch(text, signals):
        reasons.append("physical_fit_mismatch")

    if _persistent_visual(text, signals) or _has_phrase(
        normalized, signals.get("uneven_coating_phrases") or []
    ):
        reasons.append("persistent_visual_or_coating_defect")
    if _sensor_failure(text, signals):
        reasons.append("persistent_sensor_or_camera_failure")

    partial_privacy = _has_phrase(normalized, signals.get("partial_privacy_phrases") or [])
    if not partial_privacy and _has_phrase(
        normalized, signals.get("privacy_absence_phrases") or []
    ):
        reasons.append("privacy_effect_fully_absent")
    if _has_phrase(normalized, signals.get("device_or_injury_phrases") or []):
        reasons.append("device_damage_or_injury")

    dangerous_edge = breakage and (
        _same_clause_stems(
            text,
            signals.get("breakage_stems") or [],
            signals.get("large_stems") or [],
        )
        or _same_clause_stems(
            text,
            signals.get("breakage_stems") or [],
            signals.get("danger_stems") or [],
        )
    )
    if dangerous_edge:
        reasons.append("large_or_dangerous_chip")

    hard_reasons = sorted(set(reasons))
    return {
        "hard_return": bool(hard_reasons),
        "hard_return_reasons": hard_reasons,
        "post_use_breakage": bool(
            breakage
            and "received_or_pre_use_damage" not in hard_reasons
            and not hard_reasons
        ),
        "explicit_impact": _explicit_positive_impact(text, signals),
        "partial_privacy_only": bool(partial_privacy and not hard_reasons),
        "semantic_text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def _stable_choice(items: Sequence[Any], *identity: Any) -> tuple[str, int]:
    if not items:
        raise RuntimeError("WB Autoanswers owner-policy template list is empty")
    digest = hashlib.sha256("|".join(str(item) for item in identity).encode("utf-8")).hexdigest()
    index = int(digest, 16) % len(items)
    return str(items[index]).strip(), index


def _remove_unfortunately(text: str) -> str:
    value = re.sub(r"\s*,?\s*к\s+сожалению\s*,?\s*", " ", text, flags=re.IGNORECASE)
    value = re.sub(r"\s+([,.!?;:])", r"\1", value)
    value = re.sub(r"([,.!?;:])(?=[А-ЯA-Z])", r"\1 ", value)
    return " ".join(value.split())


def normalize_unfortunately(reply: str) -> tuple[str, str]:
    """Enforce one natural phrase and never combine it with regret empathy."""

    policy = _policy()
    rules = dict(policy.get("unfortunately") or {})
    text = str(reply or "").strip()
    folded = _normalize(text)
    exact = _normalize(rules.get("exact_phrase") or "к сожалению")
    empathy = rules.get("empathy_phrases") or []
    occurrences = len(re.findall(r"\bк\s+сожалению\b", text, flags=re.IGNORECASE))
    if _has_phrase(folded, empathy) and occurrences:
        return _remove_unfortunately(text), "removed_double_empathy"
    if occurrences > 1:
        kept = False

        def replace(match: re.Match[str]) -> str:
            nonlocal kept
            if not kept:
                kept = True
                return match.group(0)
            return ""

        limited = re.sub(r"\bк\s+сожалению\b", replace, text, flags=re.IGNORECASE)
        limited = re.sub(r",\s*,", ",", limited)
        return " ".join(limited.split()), "limited_to_one"
    if occurrences:
        return text, "preserved_existing"
    if _has_phrase(folded, empathy):
        return text, "skipped_empathy_present"

    contexts = rules.get("limitation_contexts") or []
    markers = rules.get("limitation_markers") or []
    for context in contexts:
        normalized_context = _normalize(context)
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            normalized_sentence = _normalize(sentence)
            if not (
                _has_phrase(normalized_sentence, [normalized_context])
                and _has_phrase(normalized_sentence, markers)
            ):
                continue
            pattern = re.compile(rf"\b{re.escape(str(context))}\b", flags=re.IGNORECASE)
            enriched, count = pattern.subn(
                lambda match: f"{match.group(0)}, {exact},",
                text,
                count=1,
            )
            if count:
                return enriched, "inserted_limitation"
    return text, "unchanged"


def apply_owner_policy(
    *,
    feedback_id: str,
    rating: int,
    content_json: Any,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply v5 to one immutable bundle result and return auditable evidence."""

    policy = _policy()
    stored = dict(result)
    source_route = str(stored.get("final_route") or "").strip()
    source_reply = str(stored.get("final_reply") or "").strip()
    if not source_route or not source_reply:
        raise ValueError("owner-policy requires final_route and final_reply")
    decision = classify_return_guard(content_json)
    route = source_route
    reply = source_reply
    template_id: str | None = None
    reason = "route_not_wb_return"
    if source_route == "wb_return":
        if decision["hard_return"]:
            reason = "independent_hard_return_preserved"
        else:
            route = "public_only"
            if decision["post_use_breakage"]:
                group = "explicit_impact" if decision["explicit_impact"] else "ordinary"
                choices = ((policy.get("post_use_breakage_templates") or {}).get(group) or [])
                reply, index = _stable_choice(choices, feedback_id, rating, group)
                template_id = f"post_use_breakage_{group}_v{index + 1}"
                reason = "ordinary_post_use_breakage"
            else:
                choices = policy.get("soft_public_templates") or []
                reply, index = _stable_choice(choices, feedback_id, rating, "soft_public")
                template_id = f"soft_public_v{index + 1}"
                reason = "no_independent_hard_return_signal"

    reply, unfortunately_action = normalize_unfortunately(reply)
    forbidden = [str(item) for item in policy.get("forbidden_reply_patterns") or []]
    matched_forbidden = [
        pattern
        for pattern in forbidden
        if route == "public_only" and re.search(pattern, reply, flags=re.IGNORECASE)
    ]
    if matched_forbidden:
        raise OwnerPolicyUnsafePublicReplyError(
            "WB Autoanswers owner-policy composed an unsafe public reply",
            evidence={
                "contract": OWNER_POLICY_CONTRACT,
                "policy_version": OWNER_POLICY_VERSION,
                "source_route": source_route,
                "publication_route": route,
                "owner_policy_reason": reason,
                "reply_sha256": hashlib.sha256(reply.encode("utf-8")).hexdigest(),
                "semantic_text_sha256": decision["semantic_text_sha256"],
                "matched_forbidden_patterns": matched_forbidden,
            },
        )
    if len(re.findall(r"\bк\s+сожалению\b", reply, flags=re.IGNORECASE)) > 1:
        raise RuntimeError("WB Autoanswers owner-policy duplicated 'к сожалению'")

    stored["final_route"] = route
    stored["final_reply"] = reply
    if route != source_route:
        stored["case_code"] = None
        stored["fallback_used"] = False
        pipeline = dict(stored.get("pipeline_result") or {})
        pipeline.update(
            {
                "route": route,
                "source_route": source_route,
                "owner_policy_reason": reason,
            }
        )
        stored["pipeline_result"] = pipeline
    stored["server_owner_policy"] = {
        "contract": OWNER_POLICY_CONTRACT,
        "policy_version": OWNER_POLICY_VERSION,
        "source_bundle_version": "1.4.2",
        "source_route": source_route,
        "source_reply_sha256": hashlib.sha256(source_reply.encode("utf-8")).hexdigest(),
        "publication_route": route,
        "reason": reason,
        "hard_return_reasons": decision["hard_return_reasons"],
        "post_use_breakage": decision["post_use_breakage"],
        "explicit_impact": decision["explicit_impact"],
        "template_id": template_id,
        "unfortunately_action": unfortunately_action,
        "operator_handoff": False,
        "model_calls": 0,
    }
    return stored

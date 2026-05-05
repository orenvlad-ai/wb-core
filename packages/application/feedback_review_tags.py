"""Helpers for normalized WB review tags/chips."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


MAX_REVIEW_TAGS = 16
MAX_REVIEW_TAG_LENGTH = 80
REVIEW_TAG_TEXT_KEYS = (
    "text",
    "name",
    "title",
    "label",
    "value",
    "caption",
    "displayName",
)
OFFICIAL_REVIEW_TAG_KEYS = (
    "bables",
    "bubbles",
    "badges",
    "tags",
    "reviewTags",
    "review_tags",
    "chips",
)
KNOWN_REVIEW_TAGS = {
    "внешний вид": "Внешний вид",
    "качество": "Качество",
    "плохое качество": "Плохое качество",
    "размер": "Размер",
    "удобство": "Удобство",
    "не соответствует описанию": "Не соответствует описанию",
}
MISSING_TEXT_REASON_PHRASES = (
    "без текста",
    "текст отсутствует",
    "нет текста",
    "без описания",
    "описание отсутствует",
    "нет описания",
    "без свободного текста",
    "текст, достоинства и недостатки не заполнены",
)


def normalize_review_tags(value: Any, *, max_tags: int = MAX_REVIEW_TAGS) -> list[str]:
    """Normalize a loosely shaped WB tag/chip value into short unique strings."""

    tags: list[str] = []
    for item in _iter_tag_values(value):
        tag = _clean_tag_text(item)
        if not tag:
            continue
        if _tag_key(tag) in {_tag_key(existing) for existing in tags}:
            continue
        tags.append(tag)
        if len(tags) >= max_tags:
            break
    return tags


def extract_official_wb_review_tags(item: Mapping[str, Any]) -> dict[str, Any]:
    """Extract review tags from official WB feedback payload fields."""

    review_tags: list[str] = []
    field_names: list[str] = []
    for key in OFFICIAL_REVIEW_TAG_KEYS:
        value = item.get(key)
        tags = normalize_review_tags(value)
        if tags:
            review_tags.extend(tags)
            field_names.append(key)
    review_tags = normalize_review_tags(review_tags)
    return {
        "review_tags": review_tags,
        "pros_tags": [],
        "cons_tags": [],
        "tag_source": "official_wb_api" if review_tags else "none",
        "tag_source_fields": field_names,
    }


def known_review_tags_from_text(value: Any) -> list[str]:
    """Return known WB chip labels found in short Seller Portal text blocks."""

    text = _clean_tag_text(value)
    if not text:
        return []
    normalized = _tag_key(text)
    if normalized in KNOWN_REVIEW_TAGS:
        return [KNOWN_REVIEW_TAGS[normalized]]
    found: list[str] = []
    for needle, label in KNOWN_REVIEW_TAGS.items():
        if re.search(r"(^|\b)" + re.escape(needle) + r"(\b|$)", normalized, flags=re.IGNORECASE):
            found.append(label)
    found_keys = {_tag_key(item) for item in found}
    found = [
        item
        for item in found
        if not any(_tag_key(item) != other and _tag_key(item) in other for other in found_keys)
    ]
    return normalize_review_tags(found)


def review_tags_display(tags: Iterable[Any]) -> str:
    return "; ".join(normalize_review_tags(list(tags)))


def reason_mentions_missing_text(reason: Any) -> bool:
    text = str(reason or "").replace("ё", "е").lower()
    return any(phrase in text for phrase in MISSING_TEXT_REASON_PHRASES)


def reason_contradicts_review_tags(reason: Any, tags: Iterable[Any]) -> bool:
    return bool(normalize_review_tags(list(tags))) and reason_mentions_missing_text(reason)


def _iter_tag_values(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        values: list[Any] = []
        for key in REVIEW_TAG_TEXT_KEYS:
            if key in value:
                values.append(value.get(key))
        for nested_key in ("items", "values", "tags", "children"):
            if nested_key in value:
                values.extend(_iter_tag_values(value.get(nested_key)))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_iter_tag_values(item))
        return values
    return [value]


def _clean_tag_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    text = text.strip(" \t\r\n:;,.")
    if len(text) > MAX_REVIEW_TAG_LENGTH:
        return ""
    return text[:1].upper() + text[1:] if text[:1].islower() else text


def _tag_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("ё", "е").strip().lower())

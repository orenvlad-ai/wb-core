"""Git-tracked, normalized update registry and deterministic NEW semantics."""

from __future__ import annotations

from datetime import date
import re

from packages.application.operator_instruction_models import (
    InstructionNewState,
    InstructionUpdate,
    OperatorInstruction,
)
from packages.application.operator_instruction_registry import PUBLISHED_OPERATOR_INSTRUCTIONS


INSTRUCTION_NEW_WINDOW_DAYS = 30
INSTRUCTION_NEW_BADGE_LABEL = "NEW"
_UPDATE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SOURCE_TYPE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

# Append new normalized entries at the end. The public list is rendered newest first.
PUBLISHED_OPERATOR_INSTRUCTION_UPDATES = (
    InstructionUpdate(
        update_id="supply-management-r2-wb-warehouse-selection",
        published_on=date(2026, 7, 20),
        instruction_id="supply-management",
        instruction_revision=2,
        summary="Добавлен порядок динамического подбора актуального склада WB отдельно для каждого направления.",
        section_anchors=("wb-warehouse-selection",),
        block_ids=(
            "wb-warehouse-selection-steps",
            "wb-warehouse-selection-central-directions",
            "wb-warehouse-selection-dynamic-rule",
            "wb-warehouse-selection-quantity-boundary",
        ),
        source_type="owner_audio_instruction",
        target_id="wb-warehouse-selection",
        new_section_anchors=("wb-warehouse-selection",),
        revisit_condition=(
            "Пересмотреть границу по количеству, когда руководитель официально передаст менеджеру "
            "расчёт количества и формирование Excel-распределений в WebCore."
        ),
    ),
)


def list_operator_instruction_updates() -> tuple[InstructionUpdate, ...]:
    """Return visible update history from newest to oldest."""

    return tuple(
        sorted(
            PUBLISHED_OPERATOR_INSTRUCTION_UPDATES,
            key=lambda item: (item.published_on, item.update_id),
            reverse=True,
        )
    )


def is_instruction_update_new(update: InstructionUpdate, business_date: date) -> bool:
    """Return true from publication day through day 29, using an injected business date."""

    if not isinstance(business_date, date):
        raise TypeError("business_date must be datetime.date")
    age_days = (business_date - update.published_on).days
    return 0 <= age_days < INSTRUCTION_NEW_WINDOW_DAYS


def active_operator_instruction_updates(
    business_date: date,
    updates: tuple[InstructionUpdate, ...] = PUBLISHED_OPERATOR_INSTRUCTION_UPDATES,
) -> tuple[InstructionUpdate, ...]:
    return tuple(
        update
        for update in sorted(
            updates,
            key=lambda item: (item.published_on, item.update_id),
            reverse=True,
        )
        if is_instruction_update_new(update, business_date)
    )


def build_instruction_new_state(
    instruction: OperatorInstruction,
    business_date: date,
    updates: tuple[InstructionUpdate, ...] = PUBLISHED_OPERATOR_INSTRUCTION_UPDATES,
) -> InstructionNewState:
    """Build article/topic/section/block badges with section-level inheritance."""

    active_updates = tuple(
        update
        for update in active_operator_instruction_updates(business_date, updates)
        if update.instruction_id == instruction.instruction_id
    )
    new_sections = {
        anchor for update in active_updates for anchor in update.new_section_anchors
    }
    block_to_section = {
        block.block_id: section.anchor
        for section in instruction.sections
        for block in section.blocks
    }
    new_blocks = {
        block_id
        for update in active_updates
        for block_id in update.new_block_ids
        if block_to_section.get(block_id) not in new_sections
    }
    topic_sections = set(new_sections)
    topic_sections.update(
        block_to_section[block_id]
        for block_id in new_blocks
        if block_id in block_to_section
    )
    return InstructionNewState(
        instruction_is_new=bool(new_sections or new_blocks),
        new_section_anchors=frozenset(new_sections),
        new_block_ids=frozenset(new_blocks),
        topic_section_anchors=frozenset(topic_sections),
    )


def validate_operator_instruction_updates(
    instructions: tuple[OperatorInstruction, ...] = PUBLISHED_OPERATOR_INSTRUCTIONS,
    updates: tuple[InstructionUpdate, ...] = PUBLISHED_OPERATOR_INSTRUCTION_UPDATES,
) -> None:
    """Validate dates, revisions and all registry references against published content."""

    instruction_by_id = {item.instruction_id: item for item in instructions}
    update_ids: set[str] = set()
    previous_date: date | None = None
    latest_revision_by_instruction: dict[str, int] = {}
    for update in updates:
        if not _UPDATE_ID_RE.fullmatch(str(update.update_id or "")):
            raise ValueError(f"invalid update id: {update.update_id!r}")
        if update.update_id in update_ids:
            raise ValueError(f"duplicate update id: {update.update_id}")
        update_ids.add(update.update_id)
        if type(update.published_on) is not date:
            raise ValueError(f"invalid update date: {update.update_id}")
        if previous_date is not None and update.published_on < previous_date:
            raise ValueError("instruction updates must be appended in nondecreasing publication order")
        previous_date = update.published_on
        instruction = instruction_by_id.get(update.instruction_id)
        if instruction is None:
            raise ValueError(f"update references unknown instruction: {update.update_id}")
        if not isinstance(update.instruction_revision, int) or not (
            1 <= update.instruction_revision <= instruction.revision
        ):
            raise ValueError(f"update revision is inconsistent: {update.update_id}")
        latest_revision_by_instruction[update.instruction_id] = max(
            latest_revision_by_instruction.get(update.instruction_id, 0),
            update.instruction_revision,
        )
        if not update.summary.strip():
            raise ValueError(f"update summary is required: {update.update_id}")
        if not _SOURCE_TYPE_RE.fullmatch(str(update.source_type or "")):
            raise ValueError(f"invalid update source type: {update.update_id}")
        for label, values in (
            ("section anchors", update.section_anchors),
            ("block ids", update.block_ids),
            ("new section anchors", update.new_section_anchors),
            ("new block ids", update.new_block_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} in update: {update.update_id}")

        section_anchors = {section.anchor for section in instruction.sections}
        block_to_section = {
            block.block_id: section.anchor
            for section in instruction.sections
            for block in section.blocks
        }
        if not set(update.section_anchors).issubset(section_anchors):
            raise ValueError(f"update references unknown section: {update.update_id}")
        if not set(update.block_ids).issubset(block_to_section):
            raise ValueError(f"update references unknown block: {update.update_id}")
        if update.target_id not in set(update.section_anchors) | set(update.block_ids):
            raise ValueError(f"update target is not among changed ids: {update.update_id}")
        if not set(update.new_section_anchors).issubset(update.section_anchors):
            raise ValueError(f"new section is not among changed sections: {update.update_id}")
        if not set(update.new_block_ids).issubset(update.block_ids):
            raise ValueError(f"new block is not among changed blocks: {update.update_id}")
        if any(
            block_to_section[block_id] not in update.section_anchors
            for block_id in update.block_ids
        ):
            raise ValueError(f"changed block section is missing from update: {update.update_id}")
        if any(
            block_to_section[block_id] in update.new_section_anchors
            for block_id in update.new_block_ids
        ):
            raise ValueError(f"new blocks must inherit a new section badge: {update.update_id}")

    for instruction in instructions:
        latest_revision = latest_revision_by_instruction.get(instruction.instruction_id)
        if instruction.revision > 1 and latest_revision != instruction.revision:
            raise ValueError(f"latest update revision is stale: {instruction.instruction_id}")

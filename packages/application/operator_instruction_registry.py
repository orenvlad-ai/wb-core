"""Published operator-instruction registry and structural validation."""

from __future__ import annotations

import re

from packages.application.operator_instruction_content.supply_management import (
    SUPPLY_MANAGEMENT_INSTRUCTION,
)
from packages.application.operator_instruction_models import OperatorInstruction


_STABLE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SUPPORTED_BLOCK_KINDS = frozenset(
    {"subheading", "numbered", "checklist", "important", "not_responsibility", "escalation", "table"}
)

PUBLISHED_OPERATOR_INSTRUCTIONS = (SUPPLY_MANAGEMENT_INSTRUCTION,)


def list_operator_instructions() -> tuple[OperatorInstruction, ...]:
    """Return published records in deterministic navigation order."""

    return PUBLISHED_OPERATOR_INSTRUCTIONS


def get_operator_instruction(instruction_id: str | None = None) -> OperatorInstruction | None:
    """Resolve a published id without filesystem lookup or dynamic loading."""

    normalized = str(instruction_id or "").strip()
    if not normalized:
        return PUBLISHED_OPERATOR_INSTRUCTIONS[0] if PUBLISHED_OPERATOR_INSTRUCTIONS else None
    for instruction in PUBLISHED_OPERATOR_INSTRUCTIONS:
        if instruction.instruction_id == normalized:
            return instruction
    return None


def validate_operator_instruction_registry(
    instructions: tuple[OperatorInstruction, ...] = PUBLISHED_OPERATOR_INSTRUCTIONS,
) -> None:
    """Fail closed on unstable ids, duplicates and malformed structured content."""

    instruction_ids: set[str] = set()
    global_block_ids: set[str] = set()
    for instruction in instructions:
        _require_stable_id(instruction.instruction_id, "instruction id")
        if instruction.instruction_id in instruction_ids:
            raise ValueError(f"duplicate instruction id: {instruction.instruction_id}")
        instruction_ids.add(instruction.instruction_id)
        if not isinstance(instruction.revision, int) or instruction.revision < 1:
            raise ValueError(f"invalid instruction revision: {instruction.instruction_id}")
        if not instruction.title.strip() or not instruction.summary.strip():
            raise ValueError(f"instruction title and summary are required: {instruction.instruction_id}")

        section_anchors: set[str] = set()
        instruction_block_ids: set[str] = set()
        for section in instruction.sections:
            _require_stable_id(section.anchor, "section anchor")
            if section.anchor in section_anchors:
                raise ValueError(f"duplicate section anchor in {instruction.instruction_id}: {section.anchor}")
            section_anchors.add(section.anchor)
            if not section.title.strip():
                raise ValueError(f"section title is required: {instruction.instruction_id}/{section.anchor}")
            for block in section.blocks:
                _require_stable_id(block.block_id, "block id")
                if block.block_id in instruction_block_ids or block.block_id in global_block_ids:
                    raise ValueError(f"duplicate block id: {block.block_id}")
                instruction_block_ids.add(block.block_id)
                global_block_ids.add(block.block_id)
                if block.kind not in _SUPPORTED_BLOCK_KINDS:
                    raise ValueError(f"unsupported block kind: {block.kind}")
                if block.kind == "table":
                    if not block.headers or any(len(row) != len(block.headers) for row in block.rows):
                        raise ValueError(f"malformed table block: {block.block_id}")


def _require_stable_id(value: str, label: str) -> None:
    if not _STABLE_ID_RE.fullmatch(str(value or "")):
        raise ValueError(f"invalid stable {label}: {value!r}")

"""Compatibility facade for the Git-tracked operator instructions knowledge base."""

from __future__ import annotations

from packages.application.operator_instruction_models import (
    InstructionBlock,
    InstructionNewState,
    InstructionSection,
    InstructionUpdate,
    OperatorInstruction,
)
from packages.application.operator_instruction_registry import (
    PUBLISHED_OPERATOR_INSTRUCTIONS,
    get_operator_instruction,
    list_operator_instructions,
    validate_operator_instruction_registry,
)
from packages.application.operator_instruction_updates import (
    INSTRUCTION_NEW_BADGE_LABEL,
    INSTRUCTION_NEW_WINDOW_DAYS,
    PUBLISHED_OPERATOR_INSTRUCTION_UPDATES,
    active_operator_instruction_updates,
    build_instruction_new_state,
    is_instruction_update_new,
    list_operator_instruction_updates,
    validate_operator_instruction_updates,
)


validate_operator_instruction_registry()
validate_operator_instruction_updates()


__all__ = (
    "INSTRUCTION_NEW_BADGE_LABEL",
    "INSTRUCTION_NEW_WINDOW_DAYS",
    "InstructionBlock",
    "InstructionNewState",
    "InstructionSection",
    "InstructionUpdate",
    "OperatorInstruction",
    "PUBLISHED_OPERATOR_INSTRUCTIONS",
    "PUBLISHED_OPERATOR_INSTRUCTION_UPDATES",
    "active_operator_instruction_updates",
    "build_instruction_new_state",
    "get_operator_instruction",
    "is_instruction_update_new",
    "list_operator_instruction_updates",
    "list_operator_instructions",
    "validate_operator_instruction_registry",
    "validate_operator_instruction_updates",
)

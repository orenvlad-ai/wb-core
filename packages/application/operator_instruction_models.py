"""Typed, Git-tracked contracts for the operator instructions knowledge base."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class InstructionBlock:
    """One stable, safe, renderer-owned semantic block."""

    block_id: str
    kind: str
    title: str = ""
    text: str = ""
    items: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstructionSection:
    """One anchorable section whose anchor is its stable DOM id."""

    anchor: str
    title: str
    lead: str = ""
    blocks: tuple[InstructionBlock, ...] = ()


@dataclass(frozen=True)
class OperatorInstruction:
    instruction_id: str
    revision: int
    title: str
    summary: str
    sections: tuple[InstructionSection, ...]


@dataclass(frozen=True)
class InstructionUpdate:
    """Normalized append-only-in-practice publication record."""

    update_id: str
    published_on: date
    instruction_id: str
    instruction_revision: int
    summary: str
    section_anchors: tuple[str, ...]
    block_ids: tuple[str, ...]
    source_type: str
    target_id: str
    new_section_anchors: tuple[str, ...] = ()
    new_block_ids: tuple[str, ...] = ()
    revisit_condition: str = ""


@dataclass(frozen=True)
class InstructionNewState:
    """Deterministic server-side NEW state for one rendered instruction."""

    instruction_is_new: bool = False
    new_section_anchors: frozenset[str] = frozenset()
    new_block_ids: frozenset[str] = frozenset()
    topic_section_anchors: frozenset[str] = frozenset()

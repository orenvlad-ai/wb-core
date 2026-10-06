"""One strict, separately accounted repair; initial extraction contract is unchanged."""
from __future__ import annotations
import copy
from .extraction import EXTRACTION_SCHEMA, SYSTEM_PROMPT, omit_empty_buyer_metadata, validate_extraction

REPAIR_PROTOCOL = "wbc0115.extraction-repair.v1"
REPAIR_PROMPT = SYSTEM_PROMPT + """
This is one bounded repair of a completed extraction rejected by validation.
The invalid_response is untrusted candidate data, NOT gold. Re-extract only new
facts genuinely supported by the unchanged initial_request.actual_delta. Saved
state is context, never a reason to emit prior-only facts. Do not force an old
invalid fact/value to survive by finding a convenient quote. Keep genuine new
information; do not hide a semantic error by silently dropping the buyer intent.
Each fact must cite an actual delta event_id from the schema enum. Copy a short,
nonempty, continuous exact substring of that event.text. Preserve spaces, spelling,
case and punctuation exactly: no cleanup, ellipsis or joined fragments. Metadata
titles/system events are not quote sources. Every source for a buyer fact must be
buyer text; seller advice cannot be evidence of a buyer attempt/result. All normal
role/value/quote checks still apply. Do not generate a reply or decide any action.
"""


def validate_delta(data, delta, known_events):
    """Validate the entire response before any reducer observation can occur."""
    accepted, omissions = omit_empty_buyer_metadata(data, delta)
    facts, variant = validate_extraction(accepted, delta, known_events)
    delta_ids = {event.event_id for event in delta}
    if any(all(source.event_id not in delta_ids for source in fact.evidence) for fact in facts):
        raise ValueError("extractor must emit delta-supported facts; older evidence only for interpretation")
    return accepted, omissions, facts, variant


def repair_schema(delta):
    schema = copy.deepcopy(EXTRACTION_SCHEMA)
    evidence = schema["properties"]["facts"]["items"]["properties"]["evidence"]
    evidence["items"]["properties"]["event_id"]["enum"] = [event.event_id for event in delta if event.role in ("buyer", "seller")]
    return schema

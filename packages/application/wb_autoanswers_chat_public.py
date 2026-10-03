"""Pure, fail-closed projection of a complete seller-chat invitation to a public reply."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from packages.application.wb_autoanswers_owner_policy import forbidden_public_reply_patterns


CHAT_PUBLIC_CONTRACT = "wb_autoanswers_chat_invitation_public_v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_invitation(reply: str, case_code: str) -> bool:
    return bool(
        case_code
        and len(case_code) <= 64
        and reply.count(case_code) == 1
        and reply.startswith("Здравствуйте.")
        and re.search(r"\bчат\s+(?:с\s+продавцом|продавца)\b|\bчате\s+продавца\b", reply, flags=re.IGNORECASE)
        and not re.search(r"(?:фото|видео|скриншот|этикет|доказатель|материал)", reply, flags=re.IGNORECASE)
        and not re.search(r"(?:ответьте|напишите|уточните).{0,35}(?:под отзывом|в ответе на отзыв|здесь в комментариях)", reply, flags=re.IGNORECASE)
        and not re.search(r"(?:обязательн.{0,20}замен|гарантируем|компенсир|верн[её]м.{0,20}деньги|заявк[ау].{0,20}одобр)", reply, flags=re.IGNORECASE)
        and not re.search(r"(?:оформите|подайте).{0,40}заявк.{0,20}возврат|обратитесь\s+в\s+поддержку", reply, flags=re.IGNORECASE)
        and not forbidden_public_reply_patterns(reply)
    )


def promote_chat_invitation(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the authored answer and code; never invent a replacement template."""

    source = dict(result)
    reply = str(source.get("final_reply") or "").strip()
    code = str(source.get("case_code") or "").strip()
    if (
        source.get("final_route") != "seller_chat"
        or not source.get("node_contract_valid")
        or not source.get("hard_gates_passed")
        or source.get("fallback_used")
        or source.get("media_uncertain")
        or not _valid_invitation(reply, code)
    ):
        raise ValueError("seller_chat invitation is not safe for automatic public publication")
    source["final_route"] = "public_only"
    source["final_reply"] = reply
    source["case_code"] = code
    source["server_policy_transform"] = {
        "contract": CHAT_PUBLIC_CONTRACT,
        "source_route": "seller_chat",
        "source_reply_sha256": _sha(reply),
        "publication_route": "public_only",
        "publication_reply_sha256": _sha(reply),
        "case_code_sha256": _sha(code),
        "operator_handoff": False,
        "model_calls": 0,
    }
    return source


def validate_promoted_chat_invitation(
    *, result: Mapping[str, Any], route: str, reply: str, case_code: str,
) -> bool:
    """Validate persisted metadata and exact publication text at every durable gate."""

    metadata = result.get("server_policy_transform")
    if not isinstance(metadata, Mapping):
        return False
    return bool(
        route == "public_only"
        and metadata.get("contract") == CHAT_PUBLIC_CONTRACT
        and metadata.get("source_route") == "seller_chat"
        and metadata.get("publication_route") == "public_only"
        and metadata.get("source_reply_sha256") == _sha(reply)
        and metadata.get("publication_reply_sha256") == _sha(reply)
        and metadata.get("case_code_sha256") == _sha(case_code)
        and metadata.get("operator_handoff") is False
        and metadata.get("model_calls") == 0
        and result.get("final_route") == route
        and result.get("final_reply") == reply
        and result.get("case_code") == case_code
        and _valid_invitation(reply, case_code)
    )


def has_chat_public_evidence(result: Mapping[str, Any], case_code: str, reply: str = "") -> bool:
    return bool(
        case_code or result.get("server_policy_transform")
        or re.search(r"\bчат\s+(?:с\s+продавцом|продавца)\b|\bчате\s+продавца\b", reply, flags=re.IGNORECASE)
    )

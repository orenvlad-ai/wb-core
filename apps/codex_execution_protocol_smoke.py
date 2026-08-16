#!/usr/bin/env python3
"""Fail closed on regressions in the active Codex routing contract."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PROTOCOLS = {
    "root": ROOT / "AGENTS.md",
    "authority": ROOT / "docs/architecture/07_codex_execution_protocol.md",
    "curator_role": ROOT / "workspaces/WB Core · Кураторы/AGENTS.override.md",
}


def require_all(name: str, text: str, markers: tuple[str, ...]) -> list[str]:
    normalized = " ".join(text.split())
    return [
        f"{name}: missing {marker!r}"
        for marker in markers
        if " ".join(marker.split()) not in normalized
    ]


def main() -> int:
    sources = {
        name: path.read_text(encoding="utf-8")
        for name, path in ACTIVE_PROTOCOLS.items()
    }
    failures: list[str] = []

    shared_markers = (
        "CAPABILITY_ROUTING_CANARY",
        "CANARY_QUALIFIED",
        "CANARY_RESTRICTED",
        "platform_approval_count=0",
        "approval_policy=never",
        "sandbox=danger-full-access",
        "routing defect",
        "supported task/thread creation surface",
        "collaboration `spawn_agent`",
        "zero curator `spawn_agent` calls",
        "exact production-mutation gate",
        "login/2FA/captcha",
        "security change",
        "new external destination",
        "material scope/risk change",
    )
    for name in ("root", "authority"):
        failures.extend(require_all(name, sources[name], shared_markers))

    failures.extend(
        require_all(
            "root",
            sources["root"],
            (
                "Re-canary обязателен",
                "Первый curator `spawn_agent` — dispatch defect",
                "видимый executor task/thread ID",
                "zero platform approval prompts",
            ),
        )
    )
    failures.extend(
        require_all(
            "authority",
            sources["authority"],
            (
                "Routing canary повторяется",
                "Первый curator collaboration `spawn_agent` — dispatch defect",
                "visible executor task/thread ID",
                "zero platform approval prompts",
            ),
        )
    )
    failures.extend(
        require_all(
            "curator_role",
            sources["curator_role"],
            (
                "supported task/thread creation surface",
                "thread ID",
                "Collaboration",
                "`spawn_agent`/subagent",
                "CANARY_QUALIFIED",
                "CANARY_RESTRICTED",
                "platform_approval_count=0",
                "zero curator `spawn_agent` calls",
            ),
        )
    )

    combined = "\n".join(sources.values())
    forbidden = {
        "front-loaded command approvals": re.compile(r"front[ -]?load", re.I),
        "legacy approval human gate": re.compile(
            r"waitingOnApproval`, missing (?:permission|credential)", re.I
        ),
        "legacy hidden executor wording": re.compile(
            r"создаёт ровно одного прямого исполнителя без nested curator или subagent",
            re.I,
        ),
        "legacy ambiguous curator launch": re.compile(
            r"куратор запускает одного отдельного исполнителя", re.I
        ),
    }
    for label, pattern in forbidden.items():
        match = pattern.search(combined)
        if match:
            failures.append(f"forbidden {label}: {match.group(0)!r}")

    if failures:
        raise SystemExit(
            "Codex execution protocol regression:\n- " + "\n- ".join(failures)
        )

    print(
        "codex execution protocol smoke: ok "
        "(visible direct executor, qualified canary, zero platform approvals)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

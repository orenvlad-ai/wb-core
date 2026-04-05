"""Минимальный локальный smoke-check для web-source snapshot block."""

from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.web_source_snapshot_block import transform_legacy_payload


ARTIFACTS = ROOT / "artifacts" / "web_source_snapshot_block"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_case(name: str, legacy_path: Path, target_path: Path) -> None:
    legacy_payload = _load_json(legacy_path)
    expected_target = _load_json(target_path)
    actual_target = asdict(transform_legacy_payload(legacy_payload))
    if actual_target != expected_target:
        raise SystemExit(
            f"{name}: smoke-check failed\n"
            f"expected={json.dumps(expected_target, ensure_ascii=False, sort_keys=True)}\n"
            f"actual={json.dumps(actual_target, ensure_ascii=False, sort_keys=True)}"
        )
    print(f"{name}: ok")


def main() -> None:
    _check_case(
        "normal",
        ARTIFACTS / "legacy" / "normal__template__legacy__fixture.json",
        ARTIFACTS / "target" / "normal__template__target__fixture.json",
    )
    _check_case(
        "not-found",
        ARTIFACTS / "legacy" / "not-found__template__legacy__fixture.json",
        ARTIFACTS / "target" / "not-found__template__target__fixture.json",
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()

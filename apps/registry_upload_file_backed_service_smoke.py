"""Smoke-check для file-backed service registry upload."""

from dataclasses import asdict
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_file_backed_service import (
    INPUT_BUNDLE_FIXTURE,
    RegistryUploadFileBackedService,
)

ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_file_backed_service"
TARGET_DIR = ARTIFACTS_DIR / "target"
ACTIVATED_AT = "2026-04-13T12:00:02Z"


def main() -> None:
    with TemporaryDirectory(prefix="registry-upload-file-backed-service-") as tmp:
        service = RegistryUploadFileBackedService(storage_dir=Path(tmp) / "store")

        accepted_result = service.upload_bundle_from_path(INPUT_BUNDLE_FIXTURE, activated_at=ACTIVATED_AT)
        accepted_result_expected = _load_json(TARGET_DIR / "upload_result__accepted__fixture.json")
        if asdict(accepted_result) != accepted_result_expected:
            raise AssertionError("accepted upload result differs from target fixture")

        accepted_bundle_expected = _load_json(TARGET_DIR / "accepted_bundle__fixture.json")
        if _load_json(service.accepted_bundle_path(accepted_result.bundle_version)) != accepted_bundle_expected:
            raise AssertionError("accepted bundle differs from target fixture")

        current_marker_expected = _load_json(TARGET_DIR / "current_marker__fixture.json")
        if _load_json(service.current_marker_path) != current_marker_expected:
            raise AssertionError("current marker differs from target fixture")

        if _load_json(service.upload_result_path(accepted_result.bundle_version)) != accepted_result_expected:
            raise AssertionError("stored accepted result differs from target fixture")

        duplicate_result = service.upload_bundle_from_path(INPUT_BUNDLE_FIXTURE, activated_at=ACTIVATED_AT)
        duplicate_result_expected = _load_json(TARGET_DIR / "upload_result__duplicate_bundle_version__fixture.json")
        if asdict(duplicate_result) != duplicate_result_expected:
            raise AssertionError("duplicate upload result differs from target fixture")

        if _load_json(service.current_marker_path) != current_marker_expected:
            raise AssertionError("current marker changed after duplicate rejection")
        if _load_json(service.upload_result_path(accepted_result.bundle_version)) != accepted_result_expected:
            raise AssertionError("stored accepted result changed after duplicate rejection")

        print(f"accepted status: ok -> {accepted_result.status}")
        print(f"bundle_version: ok -> {accepted_result.bundle_version}")
        print(f"current marker: ok -> {service.current_marker_path.name}")
        print(f"duplicate status: ok -> {duplicate_result.status}")
        print("smoke-check passed")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()

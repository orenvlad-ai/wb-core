"""Application-слой file-backed service для registry upload."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

from packages.application.registry_upload_bundle_v1 import (
    RegistryUploadBundleV1Block,
    load_registry_upload_bundle_v1_from_path,
    parse_registry_upload_bundle_v1_payload,
)
from packages.contracts.registry_upload_bundle_v1 import RegistryUploadBundleV1
from packages.contracts.registry_upload_file_backed_service import (
    RegistryUploadAcceptedCounts,
    RegistryUploadCurrentMarker,
    RegistryUploadResult,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_file_backed_service"
INPUT_BUNDLE_FIXTURE = ARTIFACTS_DIR / "input" / "registry_upload_bundle__fixture.json"


class RegistryUploadFileBackedService:
    def __init__(
        self,
        storage_dir: Path,
        bundle_block: RegistryUploadBundleV1Block | None = None,
    ) -> None:
        self.storage_dir = storage_dir
        self.bundle_block = bundle_block or RegistryUploadBundleV1Block()
        self.accepted_dir = self.storage_dir / "accepted"
        self.results_dir = self.storage_dir / "results"
        self.current_dir = self.storage_dir / "current"
        self.current_marker_path = self.current_dir / "registry_upload_current.json"

    def upload_bundle_from_path(self, bundle_path: Path, activated_at: str) -> RegistryUploadResult:
        bundle = load_registry_upload_bundle_v1_from_path(bundle_path)
        return self.upload_bundle(bundle, activated_at=activated_at)

    def upload_bundle(
        self,
        bundle_input: RegistryUploadBundleV1 | Mapping[str, Any],
        activated_at: str,
    ) -> RegistryUploadResult:
        bundle = _coerce_bundle(bundle_input)
        result_path = self.upload_result_path(bundle.bundle_version)
        errors = self._collect_validation_errors(bundle, activated_at)

        if errors:
            result = RegistryUploadResult(
                status="rejected",
                bundle_version=bundle.bundle_version,
                accepted_counts=_zero_counts(),
                validation_errors=errors,
                activated_at=None,
            )
            if not result_path.exists():
                self._ensure_layout()
                _write_json(result_path, asdict(result))
            return result

        self._ensure_layout()
        accepted_path = self.accepted_bundle_path(bundle.bundle_version)

        _write_json(accepted_path, asdict(bundle))

        result = RegistryUploadResult(
            status="accepted",
            bundle_version=bundle.bundle_version,
            accepted_counts=_accepted_counts(bundle),
            validation_errors=[],
            activated_at=activated_at,
        )
        _write_json(result_path, asdict(result))

        current_marker = RegistryUploadCurrentMarker(
            bundle_version=bundle.bundle_version,
            activated_at=activated_at,
            accepted_bundle_path=str(accepted_path.relative_to(self.storage_dir)),
            upload_result_path=str(result_path.relative_to(self.storage_dir)),
        )
        _write_json(self.current_marker_path, asdict(current_marker))
        return result

    def accepted_bundle_path(self, bundle_version: str) -> Path:
        return self.accepted_dir / f"{_bundle_version_filename(bundle_version)}.json"

    def upload_result_path(self, bundle_version: str) -> Path:
        return self.results_dir / f"{_bundle_version_filename(bundle_version)}.json"

    def _ensure_layout(self) -> None:
        self.accepted_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.current_dir.mkdir(parents=True, exist_ok=True)

    def _collect_validation_errors(self, bundle: RegistryUploadBundleV1, activated_at: str) -> list[str]:
        errors: list[str] = []

        try:
            self.bundle_block.validate_bundle(bundle, enforce_fixture_uniqueness=False)
        except ValueError as exc:
            errors.append(str(exc))

        if self.accepted_bundle_path(bundle.bundle_version).exists():
            errors.append(f"bundle_version already accepted: {bundle.bundle_version}")

        try:
            _validate_timestamp(activated_at, field_name="activated_at")
        except ValueError as exc:
            errors.append(str(exc))

        return errors


def _coerce_bundle(bundle_input: RegistryUploadBundleV1 | Mapping[str, Any]) -> RegistryUploadBundleV1:
    if isinstance(bundle_input, RegistryUploadBundleV1):
        return bundle_input
    return parse_registry_upload_bundle_v1_payload(bundle_input)


def _accepted_counts(bundle: RegistryUploadBundleV1) -> RegistryUploadAcceptedCounts:
    return RegistryUploadAcceptedCounts(
        config_v2=len(bundle.config_v2),
        metrics_v2=len(bundle.metrics_v2),
        formulas_v2=len(bundle.formulas_v2),
    )


def _zero_counts() -> RegistryUploadAcceptedCounts:
    return RegistryUploadAcceptedCounts(config_v2=0, metrics_v2=0, formulas_v2=0)


def _validate_timestamp(value: str, field_name: str) -> None:
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp ending with Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 timestamp") from exc


def _bundle_version_filename(bundle_version: str) -> str:
    return bundle_version.replace(":", "-")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

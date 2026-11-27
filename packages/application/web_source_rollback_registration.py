"""Exact operation-bound rollback images; status reads metadata, never image bodies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping

OWNER = "web_source_publication_rollback"
CONTRACT = "wb_core_web_source_rollback_registration_v1"
DIRECTORY = ".web-source-publication-rollback"
OPERATION = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_REGISTRATION_BYTES = 64 * 1024


class RollbackRegistrationError(ValueError):
    pass


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _identity(path: Path) -> dict[str, int]:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise RollbackRegistrationError("rollback image must be a regular file")
    return {key: int(getattr(value, "st_" + key)) for key in
            ("dev", "ino", "size", "mtime_ns", "ctime_ns")}


def _paths(image: Path, runtime: Path) -> tuple[str, Path]:
    runtime = runtime.resolve()
    image = image.parent.resolve() / image.name
    evidence = runtime / "evidence"
    if image.parent != evidence or evidence.is_symlink() or image.is_symlink():
        raise RollbackRegistrationError("rollback image is outside the exact evidence directory")
    operation = image.name.removesuffix(".before.json")
    if image.name != operation + ".before.json" or OPERATION.fullmatch(operation) is None:
        raise RollbackRegistrationError("rollback image operation filename is invalid")
    directory = evidence / DIRECTORY
    if directory.is_symlink():
        raise RollbackRegistrationError("rollback registration directory is a symlink")
    return operation, directory / (operation + ".json")


def prepare_registration(*, image: Path, runtime: Path, preview: Mapping[str, Any],
                         expected_image_sha256: str | None = None) -> dict[str, Any]:
    """One-time full image validation against an independently captured preview."""
    image = Path(image).parent.resolve() / Path(image).name
    runtime = Path(runtime).resolve()
    operation, _ = _paths(image, runtime)
    before = _identity(image)
    raw = image.read_bytes()
    retained = json.loads(raw)
    candidate = retained["candidate"]
    request = retained["request"]
    if (retained.get("operation_id") != operation or candidate.get("operation_id") != operation
        or preview.get("operation_id") != operation or candidate.get("phase") not in {"source", "publication"}
        or candidate["phase"] != request.get("phase")
        or Path(request.get("runtime_dir", "")).resolve() != runtime
        or preview.get("recovery", {}).get("kind") != "exact-source-and-ready-before-images"
        or Path(preview.get("recovery", {}).get("path", "")).resolve() != image
        or candidate.get("source_sha256") != request.get("source_sha256")
        or _digest(request.get("observations")) != request.get("source_sha256")
        or _digest(candidate.get("before_images")) != candidate.get("prestate_sha256")
        or candidate.get("prestate_sha256") != preview.get("prestate_sha256")
        or _digest(candidate) != preview.get("candidate_sha256")):
        raise RollbackRegistrationError("rollback image operation/preview binding mismatch")
    image_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if expected_image_sha256 is not None and image_digest != "sha256:" + expected_image_sha256.removeprefix("sha256:"):
        raise RollbackRegistrationError("rollback image SHA256 mismatch")
    after = _identity(image)
    if before != after:
        raise RollbackRegistrationError("rollback image changed during registration")
    return {"contract_version": CONTRACT, "owner": OWNER, "operation_id": operation,
            "phase": candidate["phase"], "image_path": str(image), "image_sha256": image_digest,
            "candidate_sha256": preview["candidate_sha256"], "prestate_sha256": preview["prestate_sha256"],
            "request_sha256": _digest(request), "source_sha256": request["source_sha256"],
            "identity": after}


def write_registration(*, image: Path, runtime: Path, registration: Mapping[str, Any]) -> Path:
    image = Path(image).parent.resolve() / Path(image).name
    operation, path = _paths(image, Path(runtime))
    if registration.get("operation_id") != operation or registration.get("identity") != _identity(image):
        raise RollbackRegistrationError("rollback image changed before registration")
    raw = (json.dumps(dict(registration), sort_keys=True) + "\n").encode()
    if len(raw) > MAX_REGISTRATION_BYTES:
        raise RollbackRegistrationError("rollback registration is oversized")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    directory_stat = path.parent.stat()
    if directory_stat.st_uid != image.stat().st_uid or stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise RollbackRegistrationError("rollback registration directory must be owner-only")
    # Publish only a complete fsynced claim. link() is atomic and never replaces
    # an earlier claim; a crash can leave only an ignored small temporary file.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".claim-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read_claim(path, image=image) != dict(registration):
                raise RollbackRegistrationError("rollback registration already exists with different binding") from None
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _read_claim(path: Path, *, image: Path) -> dict[str, Any]:
    directory = path.parent.stat()
    if stat.S_IMODE(directory.st_mode) != 0o700 or directory.st_uid != image.stat().st_uid:
        raise RollbackRegistrationError("rollback registration directory must be owner-only")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        value = os.fstat(stream.fileno())
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != directory.st_uid
            or stat.S_IMODE(value.st_mode) != 0o600 or value.st_size > MAX_REGISTRATION_BYTES):
            raise RollbackRegistrationError("rollback registration must be a bounded owner-only regular file")
        raw = stream.read(MAX_REGISTRATION_BYTES + 1)
        if len(raw) > MAX_REGISTRATION_BYTES:
            raise RollbackRegistrationError("rollback registration is oversized")
        return json.loads(raw)


def registered_image(path: Path, *, runtime: Path) -> bool:
    """Fail closed on absent/drifted metadata without reading the large image."""
    try:
        path = path.parent.resolve() / path.name
        operation, registration_path = _paths(path, runtime)
        claim = _read_claim(registration_path, image=path)
        if not isinstance(claim, dict):
            return False
        return (claim.get("contract_version") == CONTRACT and claim.get("owner") == OWNER
                and claim.get("operation_id") == operation and claim.get("image_path") == str(path)
                and claim.get("phase") in {"source", "publication"}
                and all(DIGEST.fullmatch(str(claim.get(key, ""))) for key in
                        ("image_sha256", "candidate_sha256", "prestate_sha256", "request_sha256", "source_sha256"))
                and claim.get("identity") == _identity(path))
    except (OSError, ValueError, TypeError, KeyError):
        return False

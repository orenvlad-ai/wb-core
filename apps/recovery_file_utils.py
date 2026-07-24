"""Small file helpers shared by bounded production recovery runners."""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading the complete production backup into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()

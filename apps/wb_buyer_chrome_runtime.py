"""Pinned Chrome runtime for the isolated, persistent WB Buyer login.

The official .deb is verified by size and SHA256, then unpacked without running
its maintainer scripts.  Only the compressed package and sandbox helper live on
the root filesystem; the executable tree is rebuilt in RAM after a reboot.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile
from urllib.request import urlopen


VERSION = "154.0.8037.57-1"
PACKAGE_URL = (
    "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/"
    f"google-chrome-stable_{VERSION}_amd64.deb"
)
PACKAGE_SHA256 = "66c0645f6a19871bab2844b8537c11a0db2e7d3bea8ef85a1c7cb52a54e65a3e"
PACKAGE_BYTES = 142_114_088
EXTRACTED_BYTES = 456_967_168
ROOT = Path("/opt/wb-core-runtime/wb-buyer-chrome")
PACKAGE = ROOT / f"google-chrome-stable_{VERSION}_amd64.deb"
SANDBOX = ROOT / "chrome-sandbox"
RUNTIME = Path(f"/dev/shm/wb-buyer-chrome-{VERSION}")
CHROME = RUNTIME / "opt/google/chrome/chrome"
PROFILE = Path("/opt/wb-core-runtime/wb_buyer_chrome_profile")
STATE = Path("/opt/wb-core-runtime/wb_buyer_chrome_auth_state")
USER = "wbchab"  # Dedicated pilot UID; changing OS identity can invalidate cookies.
POLICY = Path(__file__).resolve().parents[1] / "artifacts/registry_upload_http_entrypoint/root_storage_policy_v1.json"


def _available(path: Path) -> int:
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def _mem_available() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("Chrome memory information unavailable")


def _root_reserve() -> int:
    payload = json.loads(POLICY.read_text(encoding="utf-8"))
    return int(payload["thresholds_bytes"]["normal_available"])


def _package_identity(package: Path) -> tuple[str, str]:
    raw = subprocess.run(["dpkg-deb", "--field", str(package)], check=True, capture_output=True, text=True).stdout
    fields = dict(line.split(": ", 1) for line in raw.splitlines() if ": " in line and not line.startswith(" "))
    return fields.get("Package", ""), fields.get("Version", "")


def _verified_package(package: Path) -> bool:
    if not package.is_file() or package.stat().st_size != PACKAGE_BYTES:
        return False
    digest = hashlib.sha256()
    with package.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == PACKAGE_SHA256 and _package_identity(package) == ("google-chrome-stable", VERSION)


def _ensure_user() -> pwd.struct_passwd:
    try:
        user = pwd.getpwnam(USER)
    except KeyError as exc:
        raise RuntimeError("dedicated Chrome pilot identity unavailable") from exc
    if user.pw_uid == 0 or user.pw_shell != "/usr/sbin/nologin" or user.pw_dir != "/nonexistent":
        raise RuntimeError("Chrome runner identity invalid")
    return user


def _adopt_private_dir(path: Path, user: pwd.struct_passwd) -> None:
    if path.is_symlink():
        raise RuntimeError("Chrome private path must not be a symlink")
    if not path.exists():
        path.mkdir(mode=0o700)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise RuntimeError("Chrome private path permissions invalid")
    allowed_uids = {0, user.pw_uid}
    if path.stat().st_uid not in allowed_uids:
        raise RuntimeError("Chrome private path owner unexpected")
    if path.stat().st_uid == user.pw_uid:
        return  # Repeat deploys must not walk a live, already adopted profile.
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            entry = os.path.join(directory, name)
            if os.lstat(entry).st_uid not in allowed_uids:
                raise RuntimeError("Chrome profile contains foreign-owned entry")
            os.lchown(entry, user.pw_uid, user.pw_gid)
    os.chown(path, user.pw_uid, user.pw_gid)
    os.chmod(path, 0o700)


def install() -> None:
    """Governed deploy dependency; never touches the canonical Playwright profile."""
    if os.geteuid() != 0:
        raise RuntimeError("Chrome install requires root")
    if _available(Path("/")) < _root_reserve() + (0 if _verified_package(PACKAGE) else PACKAGE_BYTES):
        raise RuntimeError("root storage reserve would be breached")
    if _available(Path("/dev/shm")) < PACKAGE_BYTES + EXTRACTED_BYTES + 256 * 1024**2 or _mem_available() < 3 * 1024**3:
        raise RuntimeError("insufficient RAM for Chrome staging")
    ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    if ROOT.is_symlink() or ROOT.stat().st_uid != 0:
        raise RuntimeError("Chrome runtime root invalid")
    if not _verified_package(PACKAGE):
        if PACKAGE.exists():
            raise RuntimeError("existing Chrome package failed checksum")
        with tempfile.TemporaryDirectory(prefix="wb-buyer-chrome-package-", dir="/dev/shm") as temp:
            staged = Path(temp) / "chrome.deb"
            digest = hashlib.sha256()
            with urlopen(PACKAGE_URL, timeout=30) as response, staged.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
                    if output.tell() > PACKAGE_BYTES:
                        raise RuntimeError("Chrome package exceeded pinned size")
            if staged.stat().st_size != PACKAGE_BYTES or digest.hexdigest() != PACKAGE_SHA256 or _package_identity(staged) != ("google-chrome-stable", VERSION):
                raise RuntimeError("Chrome package identity mismatch")
            if _available(Path("/")) < _root_reserve() + PACKAGE_BYTES:
                raise RuntimeError("root storage reserve changed before copy")
            target = ROOT / f".{PACKAGE.name}.{os.getpid()}.tmp"
            shutil.copyfile(staged, target)
            os.chmod(target, 0o644)
            if not _verified_package(target):
                target.unlink(missing_ok=True)
                raise RuntimeError("Chrome package copy verification failed")
            target.replace(PACKAGE)
    user = _ensure_user()
    # A live pilot is never re-owned while Chrome can still write its profile.
    for unit in ("wbc0081-chrome-pipe.service", "wbc0081-chrome-restart.service", "wbc0081-chrome-ab.service"):
        state = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState", "--value"], capture_output=True, text=True).stdout.strip()
        if state in {"active", "activating", "deactivating"}:
            raise RuntimeError("Chrome pilot must finish before profile adoption")
    _adopt_private_dir(PROFILE, user)
    _adopt_private_dir(STATE, user)
    ensure_runtime()


def ensure_runtime() -> Path:
    """Rebuild the verified executable tree in RAM after a host reboot."""
    if os.geteuid() != 0 or not _verified_package(PACKAGE):
        raise RuntimeError("pinned Chrome package unavailable")
    ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    with (ROOT / "runtime.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = RUNTIME / ".package-sha256"
        if marker.is_file() and marker.read_text(encoding="ascii") == PACKAGE_SHA256 and CHROME.is_file() and os.access(CHROME, os.X_OK):
            return CHROME
        if RUNTIME.exists():
            raise RuntimeError("unexpected Chrome RAM runtime")
        if _available(Path("/dev/shm")) < EXTRACTED_BYTES + 256 * 1024**2 or _mem_available() < 3 * 1024**3:
            raise RuntimeError("insufficient RAM for Chrome runtime")
        stage = Path(tempfile.mkdtemp(prefix=".wb-buyer-chrome-", dir="/dev/shm"))
        try:
            subprocess.run(["dpkg-deb", "--extract", str(PACKAGE), str(stage)], check=True)
            chrome = stage / "opt/google/chrome/chrome"
            source_helper = chrome.parent / "chrome-sandbox"
            if not chrome.is_file() or not source_helper.is_file():
                raise RuntimeError("Chrome package missing executable or sandbox")
            if not SANDBOX.exists():
                temp_helper = ROOT / f".chrome-sandbox.{os.getpid()}.tmp"
                shutil.copyfile(source_helper, temp_helper)
                os.chown(temp_helper, 0, 0)
                os.chmod(temp_helper, 0o4755)
                temp_helper.replace(SANDBOX)
            if SANDBOX.stat().st_uid != 0 or stat.S_IMODE(SANDBOX.stat().st_mode) != 0o4755:
                raise RuntimeError("Chrome sandbox helper invalid")
            source_helper.unlink()
            source_helper.symlink_to(SANDBOX)
            if "not found" in subprocess.run(["ldd", str(chrome)], check=True, capture_output=True, text=True).stdout:
                raise RuntimeError("Chrome shared library unavailable")
            marker = stage / ".package-sha256"
            marker.write_text(PACKAGE_SHA256, encoding="ascii")
            os.chmod(stage, 0o755)
            stage.replace(RUNTIME)
            return CHROME
        finally:
            if stage.exists():
                shutil.rmtree(stage)


if __name__ == "__main__":
    install()

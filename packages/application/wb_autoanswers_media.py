"""Bounded, SSRF-safe WB media download and deterministic video sampling."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request

from packages.application.wb_autoanswers_runtime import AutoanswersRepository


MAX_PHOTO_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_FRAMES = 4
MAX_REDIRECTS = 5
DEFAULT_MEDIA_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_ALLOWED_HOST_SUFFIXES = (
    "wildberries.ru",
    "wbbasket.ru",
    "wbstatic.net",
    "geobasket.ru",
)
IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
VIDEO_MIME_TYPES = frozenset(
    {
        "video/mp4",
        "video/mpeg",
        "video/mp2t",
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
    }
)


class MediaProcessingError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MediaFetcher(Protocol):
    def fetch(self, url: str, destination: Path, *, max_bytes: int) -> Mapping[str, Any]: ...


class VideoFrameExtractor(Protocol):
    def extract(self, video_path: Path, output_dir: Path, *, max_frames: int) -> list[Path]: ...


class MediaUrlRefresher(Protocol):
    def __call__(self, feedback_id: str) -> bool: ...


def _allowed_url(url: str, allowed_suffixes: tuple[str, ...]) -> bool:
    parsed = urllib_parse.urlsplit(str(url or ""))
    host = str(parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in {None, 443}
        and any(host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes)
    )


def _public_addresses(host: str, resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo) -> list[str]:
    try:
        records = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaProcessingError("media host resolution failed", code="media_dns_failed", retryable=True) from exc
    addresses = sorted({str(record[4][0]).split("%", 1)[0] for record in records})
    if not addresses:
        raise MediaProcessingError("media host resolution was empty", code="media_dns_failed", retryable=True)
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise MediaProcessingError("media host resolved outside public address space", code="media_ssrf_blocked")
    return addresses


def _magic_mime(body: bytes) -> str | None:
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if len(body) >= 12 and body[4:8] == b"ftyp":
        return "video/mp4"
    if body.startswith(b"#EXTM3U"):
        return "application/vnd.apple.mpegurl"
    if body.startswith(b"\x47"):
        return "video/mp2t"
    return None


def _normalize_mime(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _validate_media_body(path: Path, declared_mime: str) -> str:
    with path.open("rb") as handle:
        prefix = handle.read(4096)
    detected = _magic_mime(prefix)
    declared = _normalize_mime(declared_mime)
    allowed = IMAGE_MIME_TYPES | VIDEO_MIME_TYPES
    if detected is None:
        raise MediaProcessingError("media content signature is unsupported", code="media_mime_invalid")
    if declared and declared not in allowed and declared != "application/octet-stream":
        raise MediaProcessingError("media MIME type is unsupported", code="media_mime_invalid")
    if declared in IMAGE_MIME_TYPES and detected not in IMAGE_MIME_TYPES:
        raise MediaProcessingError("media MIME does not match image content", code="media_mime_mismatch")
    if declared in VIDEO_MIME_TYPES and detected not in VIDEO_MIME_TYPES:
        raise MediaProcessingError("media MIME does not match video content", code="media_mime_mismatch")
    return detected


class _SafeRedirectHandler(urllib_request.HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], None]) -> None:
        super().__init__()
        self.validator = validator
        self.count = 0

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise MediaProcessingError("media redirect limit exceeded", code="media_redirect_limit")
        self.validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpMediaFetcher:
    """Download one WB/CDN object without exposing its signed URL.

    The initial URL and every redirect are host-allowlisted and DNS-checked
    before a connection is made.  Byte and wall-clock limits apply while the
    response is streamed to a private temporary file.
    """

    def __init__(
        self,
        *,
        allowed_host_suffixes: tuple[str, ...] = DEFAULT_ALLOWED_HOST_SUFFIXES,
        timeout: int = 30,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    ) -> None:
        self.allowed_host_suffixes = tuple(item.lower() for item in allowed_host_suffixes)
        self.timeout = max(1, int(timeout))
        self.resolver = resolver

    def _validate_url(self, url: str) -> None:
        if not _allowed_url(url, self.allowed_host_suffixes):
            raise MediaProcessingError("media URL is outside the allowlist", code="media_url_blocked")
        host = str(urllib_parse.urlsplit(url).hostname or "")
        _public_addresses(host, self.resolver)

    def fetch(self, url: str, destination: Path, *, max_bytes: int) -> Mapping[str, Any]:
        self._validate_url(url)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        size = 0
        started = time.monotonic()
        redirect_handler = _SafeRedirectHandler(self._validate_url)
        opener = urllib_request.build_opener(redirect_handler)
        request = urllib_request.Request(
            url,
            method="GET",
            headers={
                "Accept": "image/avif,image/webp,image/png,image/jpeg,video/mp4,application/vnd.apple.mpegurl,*/*;q=0.1",
                "User-Agent": "SellerOS-WB-Autoanswers-Media/1.0",
            },
        )
        try:
            with opener.open(request, timeout=self.timeout) as response, temporary.open("wb") as output:
                self._validate_url(response.geturl())
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise MediaProcessingError("media exceeds byte limit", code="media_too_large")
                while True:
                    if time.monotonic() - started > self.timeout:
                        raise MediaProcessingError("media download timed out", code="media_timeout", retryable=True)
                    chunk = response.read(min(1024 * 1024, max_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaProcessingError("media exceeds byte limit", code="media_too_large")
                    digest.update(chunk)
                    output.write(chunk)
                declared_mime = _normalize_mime(response.headers.get("Content-Type"))
            detected_mime = _validate_media_body(temporary, declared_mime)
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            return {
                "local_path": str(destination),
                "sha256": digest.hexdigest(),
                "byte_size": size,
                "mime_type": detected_mime,
            }
        except urllib_error.HTTPError as exc:
            if temporary.exists():
                temporary.unlink()
            if exc.code in {401, 403, 404, 410}:
                raise MediaProcessingError("media URL expired or unavailable", code="media_url_expired", retryable=True) from exc
            raise MediaProcessingError("media HTTP download failed", code="media_fetch_failed", retryable=exc.code >= 500) from exc
        except MediaProcessingError:
            if temporary.exists():
                temporary.unlink()
            raise
        except Exception as exc:
            if temporary.exists():
                temporary.unlink()
            raise MediaProcessingError("media download failed", code="media_fetch_failed", retryable=True) from exc


class FfmpegVideoFrameExtractor:
    def __init__(self, *, ffmpeg_binary: str = "ffmpeg", timeout_seconds: int = 60) -> None:
        self.ffmpeg_binary = ffmpeg_binary
        self.timeout_seconds = int(timeout_seconds)

    def extract(self, video_path: Path, output_dir: Path, *, max_frames: int = MAX_VIDEO_FRAMES) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        frame_pattern = output_dir / "frame-%02d.jpg"
        frame_limit = min(MAX_VIDEO_FRAMES, max(1, int(max_frames)))
        # HLS media segments are commonly only 4-5 seconds long and retain
        # their absolute stream timestamps.  Sampling those one at a time
        # with a 15-second fps cadence can legitimately exit 0 while writing
        # no frame.  For the bounded single-frame HLS path, select the first
        # decodable frame instead; the segment selection itself already
        # provides deterministic spacing across the video.
        video_filter = (
            "select='eq(n,0)',scale='min(1280,iw)':-2"
            if frame_limit == 1
            else "fps=1/15,scale='min(1280,iw)':-2"
        )
        try:
            completed = subprocess.run(
                [
                    self.ffmpeg_binary,
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-i",
                    str(video_path),
                    "-vf",
                    video_filter,
                    "-frames:v",
                    str(frame_limit),
                    "-y",
                    str(frame_pattern),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaProcessingError("video frame extractor unavailable", code="video_extract_unavailable") from exc
        if completed.returncode != 0:
            raise MediaProcessingError("video frame extraction failed", code="video_extract_failed")
        frames = sorted(output_dir.glob("frame-*.jpg"))[:MAX_VIDEO_FRAMES]
        for frame in frames:
            os.chmod(frame, 0o600)
        return frames


def _playlist_uris(body: str) -> list[str]:
    return [line.strip() for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _playlist_map_uri(body: str) -> str | None:
    match = re.search(r'^#EXT-X-MAP:.*?URI="([^"]+)"', body, flags=re.MULTILINE)
    return match.group(1) if match else None


def _evenly_spaced(values: Sequence[str], limit: int) -> list[str]:
    if len(values) <= limit:
        return list(values)
    if limit <= 1:
        return [values[0]]
    indexes = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    return [values[index] for index in indexes]


class AutoanswersMediaProcessor:
    def __init__(
        self,
        *,
        repository: AutoanswersRepository,
        runtime_dir: Path,
        fetcher: MediaFetcher | None = None,
        frame_extractor: VideoFrameExtractor | None = None,
        refresh_urls: MediaUrlRefresher | None = None,
        ttl_seconds: int = DEFAULT_MEDIA_TTL_SECONDS,
    ) -> None:
        self.repository = repository
        self.root = Path(runtime_dir) / "wb_autoanswers_media"
        self.fetcher = fetcher or HttpMediaFetcher()
        self.frame_extractor = frame_extractor or FfmpegVideoFrameExtractor()
        self.refresh_urls = refresh_urls
        self.ttl_seconds = max(60, int(ttl_seconds))

    def _row_after_refresh(self, feedback_id: str, content_version: int, kind: str, ordinal: int) -> dict[str, Any] | None:
        if self.refresh_urls is None or not self.refresh_urls(feedback_id):
            return None
        for candidate in self.repository.media_rows(feedback_id, content_version):
            if candidate["kind"] == kind and int(candidate["ordinal"]) == ordinal:
                return candidate
        return None

    def _fetch(
        self,
        row: Mapping[str, Any],
        *,
        feedback_id: str,
        content_version: int,
        url_field: str,
        destination: Path,
        max_bytes: int,
    ) -> Mapping[str, Any]:
        resolved_url = str(row.get(url_field) or "")
        try:
            metadata = self.fetcher.fetch(resolved_url, destination, max_bytes=max_bytes)
        except MediaProcessingError as exc:
            if exc.code != "media_url_expired":
                raise
            refreshed = self._row_after_refresh(
                feedback_id, content_version, str(row["kind"]), int(row["ordinal"])
            )
            if refreshed is None:
                raise
            resolved_url = str(refreshed.get(url_field) or "")
            metadata = self.fetcher.fetch(resolved_url, destination, max_bytes=max_bytes)
        return {**dict(metadata), "_resolved_url": resolved_url}

    @staticmethod
    def _persisted_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {str(key): value for key, value in metadata.items() if not str(key).startswith("_")}

    @staticmethod
    def _frame_records(frames: Sequence[Path]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for frame in frames[:MAX_VIDEO_FRAMES]:
            body = frame.read_bytes()
            if _magic_mime(body[:4096]) != "image/jpeg":
                raise MediaProcessingError("extracted frame is not JPEG", code="video_frame_mime_invalid")
            records.append(
                {
                    "local_path": str(frame),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "mime_type": "image/jpeg",
                    "byte_size": len(body),
                }
            )
        return records

    def _hls_frames(
        self,
        *,
        playlist_path: Path,
        source_url: str,
        directory: Path,
        initial_byte_size: int,
    ) -> list[dict[str, Any]]:
        body = playlist_path.read_text(encoding="utf-8-sig")
        uris = _playlist_uris(body)
        if not uris:
            raise MediaProcessingError("HLS playlist has no media entries", code="video_playlist_empty")
        # A master playlist points to media playlists.  Choose the first entry
        # deterministically; this bounds cost and makes reruns reproducible.
        variant_byte_size = 0
        if "#EXT-X-STREAM-INF" in body:
            media_url = urllib_parse.urljoin(source_url, uris[0])
            media_playlist = directory / "media-playlist.m3u8"
            metadata = self.fetcher.fetch(media_url, media_playlist, max_bytes=2 * 1024 * 1024)
            if metadata.get("mime_type") != "application/vnd.apple.mpegurl":
                raise MediaProcessingError("HLS variant is not a playlist", code="video_playlist_invalid")
            variant_byte_size = int(metadata.get("byte_size") or 0)
            source_url = media_url
            body = media_playlist.read_text(encoding="utf-8-sig")
            uris = _playlist_uris(body)
        segments = _evenly_spaced(uris, MAX_VIDEO_FRAMES)
        if not segments:
            raise MediaProcessingError("HLS playlist has no segments", code="video_playlist_empty")
        init_uri = _playlist_map_uri(body)
        init_body = b""
        remaining = MAX_VIDEO_BYTES - int(initial_byte_size) - variant_byte_size
        if remaining <= 0:
            raise MediaProcessingError("video exceeds aggregate byte limit", code="media_too_large")
        if init_uri:
            init_path = directory / "hls-init.bin"
            init_meta = self.fetcher.fetch(
                urllib_parse.urljoin(source_url, init_uri), init_path, max_bytes=min(remaining, 20 * 1024 * 1024)
            )
            init_body = init_path.read_bytes()
            remaining -= int(init_meta["byte_size"])
        frames: list[Path] = []
        for index, segment_ref in enumerate(segments):
            if remaining <= 0:
                raise MediaProcessingError("video exceeds aggregate byte limit", code="media_too_large")
            segment = directory / f"segment-{index:02d}.bin"
            metadata = self.fetcher.fetch(
                urllib_parse.urljoin(source_url, segment_ref),
                segment,
                max_bytes=remaining,
            )
            remaining -= int(metadata["byte_size"])
            sample = segment
            if init_body:
                sample = directory / f"sample-{index:02d}.mp4"
                sample.write_bytes(init_body + segment.read_bytes())
                os.chmod(sample, 0o600)
            extracted = self.frame_extractor.extract(
                sample, directory / f"frames-{index:02d}", max_frames=1
            )
            if extracted:
                frames.append(extracted[0])
        return self._frame_records(frames)

    def process(
        self,
        *,
        feedback_id: str,
        content_version: int,
        asset_kinds: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        self.cleanup_expired()
        rows = self.repository.media_rows(feedback_id, content_version)
        safe_id = hashlib.sha256(feedback_id.encode("utf-8")).hexdigest()[:24]
        directory = self.root / safe_id / str(int(content_version))
        results: dict[str, Any] = {
            "photos_downloaded": 0,
            "video_previews": 0,
            "video_frames": 0,
            "uncertainty": [],
        }
        for row in rows:
            if asset_kinds is not None and str(row["kind"]) not in asset_kinds:
                continue
            if row["kind"] == "video_frame" or row["fetch_status"] in {"downloaded", "frames_extracted"}:
                continue
            try:
                if row["kind"] == "photo":
                    destination = directory / f"photo-{int(row['ordinal']):02d}.image"
                    metadata = self._fetch(
                        row,
                        feedback_id=feedback_id,
                        content_version=content_version,
                        url_field="source_full_url",
                        destination=destination,
                        max_bytes=MAX_PHOTO_BYTES,
                    )
                    if metadata["mime_type"] not in IMAGE_MIME_TYPES:
                        raise MediaProcessingError("photo content is not an image", code="photo_mime_invalid")
                    self.repository.update_media_result(
                        feedback_id=feedback_id,
                        content_version=content_version,
                        kind="photo",
                        ordinal=int(row["ordinal"]),
                        fetch_status="downloaded",
                        **self._persisted_metadata(metadata),
                    )
                    results["photos_downloaded"] += 1
                    continue

                preview_metadata: Mapping[str, Any] | None = None
                if str(row.get("source_preview_url") or ""):
                    preview_fetch = self._fetch(
                        row,
                        feedback_id=feedback_id,
                        content_version=content_version,
                        url_field="source_preview_url",
                        destination=directory / f"video-{int(row['ordinal']):02d}-preview.image",
                        max_bytes=MAX_PHOTO_BYTES,
                    )
                    if preview_fetch["mime_type"] not in IMAGE_MIME_TYPES:
                        raise MediaProcessingError("video preview is not an image", code="video_preview_mime_invalid")
                    preview_metadata = self._persisted_metadata(preview_fetch)
                    results["video_previews"] += 1

                source_path = urllib_parse.urlsplit(str(row.get("source_full_url") or "")).path.lower()
                source_suffix = ".m3u8" if source_path.endswith(".m3u8") else ".mp4"
                video_path = directory / f"video-{int(row['ordinal']):02d}{source_suffix}"
                metadata = self._fetch(
                    row,
                    feedback_id=feedback_id,
                    content_version=content_version,
                    url_field="source_full_url",
                    destination=video_path,
                    max_bytes=MAX_VIDEO_BYTES,
                )
                if metadata["mime_type"] == "application/vnd.apple.mpegurl":
                    frames = self._hls_frames(
                        playlist_path=video_path,
                        source_url=str(metadata["_resolved_url"]),
                        directory=directory / f"hls-{int(row['ordinal']):02d}",
                        initial_byte_size=int(metadata["byte_size"]),
                    )
                elif metadata["mime_type"] in VIDEO_MIME_TYPES:
                    frames = self._frame_records(
                        self.frame_extractor.extract(
                            video_path,
                            directory / f"frames-{int(row['ordinal']):02d}",
                            max_frames=MAX_VIDEO_FRAMES,
                        )
                    )
                else:
                    raise MediaProcessingError("video content is unsupported", code="video_mime_invalid")
                if not frames:
                    raise MediaProcessingError("video yielded no frames", code="video_no_frames")
                if preview_metadata is None:
                    # WB does not always provide a separate preview URL.  Use
                    # the first deterministic frame as a bounded derived
                    # preview without claiming that the full video was seen.
                    preview_metadata = dict(frames[0])
                    results["video_previews"] += 1
                self.repository.replace_video_frames(
                    feedback_id=feedback_id, content_version=content_version, frames=frames
                )
                self.repository.update_media_result(
                    feedback_id=feedback_id,
                    content_version=content_version,
                    kind="video",
                    ordinal=int(row["ordinal"]),
                    fetch_status="frames_extracted",
                    preview=preview_metadata,
                    **self._persisted_metadata(metadata),
                )
                results["video_frames"] += len(frames)
            except MediaProcessingError as exc:
                self.repository.update_media_result(
                    feedback_id=feedback_id,
                    content_version=content_version,
                    kind=str(row["kind"]),
                    ordinal=int(row["ordinal"]),
                    fetch_status="fetch_failed",
                    uncertainty_code=exc.code,
                )
                results["uncertainty"].append(exc.code)
        results["media_uncertain"] = bool(results["uncertainty"])
        return results

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """Remove only version directories older than the configured TTL."""

        if not self.root.is_dir():
            return 0
        cutoff = (now or datetime.now(timezone.utc)).timestamp() - self.ttl_seconds
        removed = 0
        for feedback_dir in self.root.iterdir():
            if not feedback_dir.is_dir():
                continue
            for version_dir in feedback_dir.iterdir():
                try:
                    modified = version_dir.stat().st_mtime
                except FileNotFoundError:
                    continue
                if version_dir.is_dir() and modified < cutoff:
                    self.repository.expire_media_directory(version_dir)
                    shutil.rmtree(version_dir)
                    removed += 1
            try:
                feedback_dir.rmdir()
            except OSError:
                pass
        return removed

"""Bounded server-side media download and video frame extraction."""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Protocol
from urllib import parse as urllib_parse, request as urllib_request

from packages.application.wb_autoanswers_runtime import AutoanswersRepository


MAX_PHOTO_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_FRAMES = 6
DEFAULT_ALLOWED_HOST_SUFFIXES = ("wildberries.ru", "wbbasket.ru", "wbstatic.net")


class MediaProcessingError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MediaFetcher(Protocol):
    def fetch(self, url: str, destination: Path, *, max_bytes: int) -> Mapping[str, Any]: ...


class VideoFrameExtractor(Protocol):
    def extract(self, video_path: Path, output_dir: Path, *, max_frames: int) -> list[Path]: ...


def _allowed_url(url: str, allowed_suffixes: tuple[str, ...]) -> bool:
    parsed = urllib_parse.urlsplit(str(url or ""))
    host = str(parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and any(host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes)


class HttpMediaFetcher:
    def __init__(self, *, allowed_host_suffixes: tuple[str, ...] = DEFAULT_ALLOWED_HOST_SUFFIXES, timeout: int = 30) -> None:
        self.allowed_host_suffixes = tuple(item.lower() for item in allowed_host_suffixes)
        self.timeout = int(timeout)

    def fetch(self, url: str, destination: Path, *, max_bytes: int) -> Mapping[str, Any]:
        if not _allowed_url(url, self.allowed_host_suffixes):
            raise MediaProcessingError("media URL is outside the allowlist", code="media_url_blocked")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        size = 0
        request = urllib_request.Request(url, method="GET", headers={"Accept": "*/*"})
        try:
            with urllib_request.urlopen(request, timeout=self.timeout) as response, temporary.open("wb") as output:
                final_url = response.geturl()
                if not _allowed_url(final_url, self.allowed_host_suffixes):
                    raise MediaProcessingError("media redirect left the allowlist", code="media_redirect_blocked")
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise MediaProcessingError("media exceeds byte limit", code="media_too_large")
                while True:
                    chunk = response.read(min(1024 * 1024, max_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaProcessingError("media exceeds byte limit", code="media_too_large")
                    digest.update(chunk)
                    output.write(chunk)
                mime_type = str(response.headers.get_content_type() or mimetypes.guess_type(destination.name)[0] or "application/octet-stream")
            os.replace(temporary, destination)
            return {"local_path": str(destination), "sha256": digest.hexdigest(), "byte_size": size, "mime_type": mime_type}
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
        output_dir.mkdir(parents=True, exist_ok=True)
        frame_pattern = output_dir / "frame-%02d.jpg"
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
                    "fps=1/15,scale='min(1280,iw)':-2",
                    "-frames:v",
                    str(min(MAX_VIDEO_FRAMES, max(1, int(max_frames)))),
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
        return sorted(output_dir.glob("frame-*.jpg"))[:MAX_VIDEO_FRAMES]


class AutoanswersMediaProcessor:
    def __init__(
        self,
        *,
        repository: AutoanswersRepository,
        runtime_dir: Path,
        fetcher: MediaFetcher | None = None,
        frame_extractor: VideoFrameExtractor | None = None,
    ) -> None:
        self.repository = repository
        self.root = Path(runtime_dir) / "wb_autoanswers_media"
        self.fetcher = fetcher or HttpMediaFetcher()
        self.frame_extractor = frame_extractor or FfmpegVideoFrameExtractor()

    def process(self, *, feedback_id: str, content_version: int) -> dict[str, Any]:
        rows = self.repository.media_rows(feedback_id, content_version)
        safe_id = hashlib.sha256(feedback_id.encode("utf-8")).hexdigest()[:24]
        directory = self.root / safe_id / str(int(content_version))
        results = {"photos_downloaded": 0, "video_frames": 0, "uncertainty": []}
        for row in rows:
            if row["kind"] == "video_frame" or row["fetch_status"] in {"downloaded", "frames_extracted"}:
                continue
            extension = ".mp4" if row["kind"] == "video" else ".jpg"
            destination = directory / f"{row['kind']}-{int(row['ordinal']):02d}{extension}"
            try:
                metadata = self.fetcher.fetch(
                    str(row["source_full_url"]),
                    destination,
                    max_bytes=MAX_VIDEO_BYTES if row["kind"] == "video" else MAX_PHOTO_BYTES,
                )
                if row["kind"] == "photo":
                    self.repository.update_media_result(
                        feedback_id=feedback_id,
                        content_version=content_version,
                        kind="photo",
                        ordinal=int(row["ordinal"]),
                        fetch_status="downloaded",
                        **metadata,
                    )
                    results["photos_downloaded"] += 1
                else:
                    frames = self.frame_extractor.extract(
                        Path(str(metadata["local_path"])), directory / "frames", max_frames=MAX_VIDEO_FRAMES
                    )
                    frame_records = []
                    for frame in frames:
                        body = frame.read_bytes()
                        frame_records.append(
                            {
                                "local_path": str(frame),
                                "sha256": hashlib.sha256(body).hexdigest(),
                                "mime_type": "image/jpeg",
                                "byte_size": len(body),
                            }
                        )
                    if not frame_records:
                        raise MediaProcessingError("video yielded no frames", code="video_no_frames")
                    self.repository.replace_video_frames(
                        feedback_id=feedback_id, content_version=content_version, frames=frame_records
                    )
                    self.repository.update_media_result(
                        feedback_id=feedback_id,
                        content_version=content_version,
                        kind="video",
                        ordinal=int(row["ordinal"]),
                        fetch_status="frames_extracted",
                        **metadata,
                    )
                    results["video_frames"] += len(frame_records)
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

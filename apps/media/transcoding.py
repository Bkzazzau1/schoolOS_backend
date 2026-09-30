"""Turning a video's real bytes into a small preview frame - the video counterpart to thumbnails.py's image
path. Real frame extraction needs ffmpeg (or an equivalent decoder) installed on the server; this module is the
seam so a video thumbnail job runs exactly the same way whether or not one is actually available, the same
override-for-tests shape apps/media/storage/registry.py already uses for storage. Where no real transcoder is
installed, TranscoderUnavailable is a known, expected condition - see uploads.py: run_thumbnail - never a job
failure: the video itself is still real, stored and downloadable; it simply has no preview frame yet.
"""

import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path

from .thumbnails import THUMBNAIL_MAX_SIDE, probe_dimensions


class TranscoderError(Exception):
    """Something the transcoder itself refused or could not do. `code` is a stable string."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TranscoderUnavailable(TranscoderError):
    """No real video transcoder exists on this server."""

    def __init__(self, message: str = "No video transcoder is available on this server."):
        super().__init__("transcoder_unavailable", message)


class VideoTranscoder(ABC):
    @abstractmethod
    def build_thumbnail(self, data: bytes) -> tuple[bytes, int, int, str]:
        """A JPEG preview frame from a video's real bytes, its width and height, and its mime type. Raises
        TranscoderUnavailable when this server has no real way to decode video; raises TranscoderError for a
        genuine decode failure (a corrupt file, one ffmpeg cannot read)."""


class FfmpegTranscoder(VideoTranscoder):
    """Shells out to a real `ffmpeg` binary to grab the first frame and shrink it to the same target size
    thumbnails.py uses for images, so a video preview looks consistent with a photo one. Raises
    TranscoderUnavailable when ffmpeg is not actually installed on this server - the day it is, this starts
    producing real thumbnails with no other code change."""

    def build_thumbnail(self, data: bytes) -> tuple[bytes, int, int, str]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise TranscoderUnavailable()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input"
            source.write_bytes(data)
            frame = Path(tmp) / "frame.jpg"
            scale = f"scale='min({THUMBNAIL_MAX_SIDE},iw)':'min({THUMBNAIL_MAX_SIDE},ih)':force_original_aspect_ratio=decrease"
            try:
                result = subprocess.run(
                    [ffmpeg, "-y", "-i", str(source), "-vframes", "1", "-vf", scale, "-q:v", "4", str(frame)],
                    capture_output=True, timeout=30,
                )
            except subprocess.TimeoutExpired as error:
                raise TranscoderError("video_frame_extraction_timed_out", "This video took too long to read a preview frame from.") from error
            if result.returncode != 0 or not frame.exists():
                raise TranscoderError("video_frame_extraction_failed", "Could not read a preview frame from this video.")
            thumbnail_bytes = frame.read_bytes()
        width, height = probe_dimensions(thumbnail_bytes) or (0, 0)
        return thumbnail_bytes, width, height, "image/jpeg"


_override: VideoTranscoder | None = None


@contextmanager
def use_transcoder(transcoder: VideoTranscoder):
    """Swaps in a fake transcoder for the duration of a `with` block, so a test can prove the job/derivative/
    status machinery end to end without a real ffmpeg binary - the same shape `apps/media/storage/registry.py:
    use_storage` already uses."""
    global _override
    previous, _override = _override, transcoder
    try:
        yield transcoder
    finally:
        _override = previous


def get_transcoder() -> VideoTranscoder:
    if _override is not None:
        return _override
    return FfmpegTranscoder()

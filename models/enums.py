"""
Domain enums for mst-ytdl.

Live in models/ (NOT configs/) because they are pure domain data used by
models, services, tasks and the API; the models layer must not depend upward
on configs. Re-exported from models/__init__.py — always
``from models import MediaFormat, DownloadStatus``.
"""

from __future__ import annotations

from enum import Enum


class MediaFormat(str, Enum):
    """Output container / codec the user wants."""

    MP3 = "mp3"
    M4A = "m4a"
    OPUS = "opus"
    FLAC = "flac"
    WAV = "wav"
    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"

    @property
    def is_audio(self) -> bool:
        """True for audio-only outputs."""
        return self in AUDIO_FORMATS

    @property
    def is_video(self) -> bool:
        return not self.is_audio

    @property
    def lossy_bitrate(self) -> bool:
        """Formats where a kbps target makes sense (re-encoded lossy audio)."""
        return self in (MediaFormat.MP3, MediaFormat.M4A)


AUDIO_FORMATS = frozenset(
    {MediaFormat.MP3, MediaFormat.M4A, MediaFormat.OPUS, MediaFormat.FLAC, MediaFormat.WAV}
)
VIDEO_FORMATS = frozenset({MediaFormat.MP4, MediaFormat.MKV, MediaFormat.WEBM})

# Selectable qualities. "best" is always valid.
AUDIO_BITRATES = ("64", "96", "128", "160", "192", "256", "320")
VIDEO_HEIGHTS = ("144", "240", "360", "480", "720", "1080", "1440", "2160")


class DownloadStatus(str, Enum):
    """Lifecycle of one item (and, derived, of a job)."""

    QUEUED = "queued"  # waiting for a worker / a free slot
    RESOLVING = "resolving"  # job level: expanding a playlist
    DOWNLOADING = "downloading"
    PROCESSING = "processing"  # ffmpeg: merge / convert / embed
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"  # job level only: some items done, some failed/cancelled

    @property
    def is_terminal(self) -> bool:
        return self in (
            DownloadStatus.COMPLETED,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELLED,
            DownloadStatus.PARTIAL,
        )

    @property
    def is_running(self) -> bool:
        """A worker is (or should be) actively working on it."""
        return self in (DownloadStatus.DOWNLOADING, DownloadStatus.PROCESSING)


class ControlAction(str, Enum):
    """A user request the running worker must honour at its next checkpoint."""

    PAUSE = "pause"
    CANCEL = "cancel"


class JobKind(str, Enum):
    SINGLE = "single"
    PLAYLIST = "playlist"
    CART = "cart"  # hand-picked videos (the download cart), items known up front — no expansion

    @property
    def is_multi(self) -> bool:
        """Many files in one folder (numbered, zipped together)."""
        return self is not JobKind.SINGLE


class DeliveryKind(str, Enum):
    """What a server-mode job hands to the browser."""

    FILE = "file"  # a single video → the file itself
    ZIP = "zip"  # playlist / cart → one archive
    NONE = "none"  # nothing finished (all failed / cancelled) — the record only schedules cleanup


class PlaylistMode(str, Enum):
    """How to treat URLs that can be either a video or a playlist."""

    AUTO = "auto"
    SINGLE = "single"
    PLAYLIST = "playlist"


class ResolveState(str, Enum):
    """Playlist expansion progress of a job (singles are READY immediately)."""

    PENDING = "pending"
    RESOLVING = "resolving"
    READY = "ready"
    FAILED = "failed"

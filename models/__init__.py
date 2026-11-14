"""
Domain models — pure Pydantic data + validation, re-exported here.
Call sites always do ``from models import DownloadJob``, never module paths.
"""

from models.download import (
    DownloadItem,
    DownloadJob,
    DownloadOptions,
    DownloadRequest,
    EntryInfo,
    JobView,
    MediaInfo,
    QueueStats,
    RuntimeSettings,
    derive_job_status,
)
from models.enums import (
    AUDIO_BITRATES,
    AUDIO_FORMATS,
    VIDEO_FORMATS,
    VIDEO_HEIGHTS,
    ControlAction,
    DownloadStatus,
    JobKind,
    MediaFormat,
    PlaylistMode,
    ResolveState,
)
from models.stream import (
    QualityOption,
    StreamFormat,
    StreamInfo,
    SubtitleSource,
    SubtitleTrack,
)

__all__ = [
    "AUDIO_BITRATES",
    "AUDIO_FORMATS",
    "VIDEO_FORMATS",
    "VIDEO_HEIGHTS",
    "ControlAction",
    "DownloadItem",
    "DownloadJob",
    "DownloadOptions",
    "DownloadRequest",
    "DownloadStatus",
    "EntryInfo",
    "JobKind",
    "JobView",
    "MediaFormat",
    "MediaInfo",
    "PlaylistMode",
    "QueueStats",
    "QualityOption",
    "ResolveState",
    "RuntimeSettings",
    "StreamFormat",
    "StreamInfo",
    "SubtitleSource",
    "SubtitleTrack",
    "derive_job_status",
]

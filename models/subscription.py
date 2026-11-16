"""
Subscription models — follow a channel or playlist and download its new uploads.

Pure data + validation (pydantic only):
    SubscriptionKind     channel | playlist
    SubscriptionRequest  body of ``POST /api/subscriptions`` (URL + the options to snapshot)
    Subscription         the persisted record (Redis hash, one JSON value per field)
    SubscriptionView     what the API returns (the record without its owner)
    SubscriptionCheck    result of one poll (``POST /api/subscriptions/{id}/check``)
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from models.download import DownloadOptions, OptionFields


class SubscriptionKind(str, Enum):
    """What is being followed: a channel's uploads tab, or a playlist."""

    CHANNEL = "channel"
    PLAYLIST = "playlist"


class SubscriptionRequest(OptionFields):
    """Body of ``POST /api/subscriptions``. The option fields are snapshotted into the subscription."""

    url: str = Field(min_length=1, max_length=2000)
    skip_shorts: bool = True  # ignore YouTube Shorts (duration <= 60 s, /shorts/ URLs, "#shorts" titles)


class Subscription(BaseModel):
    """One followed channel / playlist. Stored as a Redis hash (``P:SUB:<sub_id>``)."""

    sub_id: str
    url: str  # canonical URL handed to yt-dlp
    kind: SubscriptionKind
    title: str = ""
    uploader: str | None = None
    thumbnail: str | None = None
    options: DownloadOptions  # snapshot taken at subscribe time
    skip_shorts: bool = True
    enabled: bool = True
    owner: str | None = None  # server mode: the visitor who subscribed (None = personal mode)
    created_at: float = 0.0
    last_check: float | None = None  # when it was last polled (success or not)
    last_error: str | None = None  # why the last poll failed / was deferred (None = fine)
    last_new: int = 0  # videos queued by the last poll
    downloaded: int = 0  # videos queued over the subscription's lifetime
    seen_count: int = 0  # filled by the service on read: how many video ids are known


class SubscriptionView(BaseModel):
    """API shape of a subscription (the owner id stays server-side)."""

    sub_id: str
    url: str
    kind: SubscriptionKind
    title: str
    uploader: str | None = None
    thumbnail: str | None = None
    options: DownloadOptions
    skip_shorts: bool
    enabled: bool
    created_at: float
    last_check: float | None = None
    last_error: str | None = None
    last_new: int = 0
    downloaded: int = 0
    seen_count: int = 0

    @classmethod
    def of(cls, sub: Subscription) -> "SubscriptionView":
        """Build the view from a record."""
        return cls.model_validate(sub.model_dump(exclude={"owner"}))


class SubscriptionCheck(BaseModel):
    """Outcome of a manual poll."""

    subscription: SubscriptionView
    new: int = 0  # videos queued by this poll
    error: str | None = None

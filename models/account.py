"""Account views: the signed-in user's own playlists and home feed (read from their YouTube cookies)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from models.download import EntryInfo


class AccountPlaylist(BaseModel):
    """One playlist of the account (``url`` opens through the normal playlist preview)."""

    id: str
    title: str
    url: str
    thumbnail: str | None = None
    count: int | None = None
    uploader: str | None = None
    music: bool = False
    pinned: bool = False  # Liked videos / Liked music / Watch later — present on every account


class AccountPlaylists(BaseModel):
    signed_in: bool
    playlists: list[AccountPlaylist] = Field(default_factory=list)
    message: str | None = None


class AccountFeed(BaseModel):
    signed_in: bool
    entries: list[EntryInfo] = Field(default_factory=list)
    message: str | None = None

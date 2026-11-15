"""The signed-in user's own playlists and home feed (needs their YouTube cookies)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import Owner, rate
from models import AccountFeed, AccountPlaylists
from services import account_service, analytics

router = APIRouter(tags=["account"])


@router.get("/account/playlists", response_model=AccountPlaylists, dependencies=[Depends(rate("lookup"))])
def account_playlists(who: Owner) -> AccountPlaylists:
    """Liked music / liked videos / watch later + the account's library playlists (open one via ``POST /info``)."""
    analytics.count("account")
    return account_service.playlists(who)


@router.get("/account/home", response_model=AccountFeed, dependencies=[Depends(rate("lookup"))])
def account_home(who: Owner, limit: int = Query(40, ge=1, le=60)) -> AccountFeed:
    """The account's YouTube home feed as tracks."""
    return account_service.home(who, limit)

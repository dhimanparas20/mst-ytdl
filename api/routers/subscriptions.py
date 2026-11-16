"""Subscriptions: follow a channel / playlist; new uploads are queued by the periodic poll.

Scoped to the caller in server mode (``Owner``): another visitor's subscription is a 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import Owner, rate
from models import SubscriptionCheck, SubscriptionRequest, SubscriptionView
from services import subscription_service

router = APIRouter(tags=["subscriptions"])


@router.get("/subscriptions", response_model=list[SubscriptionView])
def list_subscriptions(who: Owner) -> list[SubscriptionView]:
    """Your subscriptions, newest first."""
    return [SubscriptionView.of(s) for s in subscription_service.list_for(who)]


@router.post("/subscriptions", response_model=SubscriptionView, status_code=201, dependencies=[Depends(rate("submit"))])
def create_subscription(body: SubscriptionRequest, who: Owner) -> SubscriptionView:
    """
    Follow a channel or playlist with the given download options (snapshotted). Videos already
    there are marked as seen — only future uploads are downloaded.
    """
    return SubscriptionView.of(subscription_service.create(body, owner=who))


@router.post("/subscriptions/{sub_id}/pause", response_model=SubscriptionView)
def pause_subscription(sub_id: str, who: Owner) -> SubscriptionView:
    return SubscriptionView.of(subscription_service.pause(sub_id, owner=who))


@router.post("/subscriptions/{sub_id}/resume", response_model=SubscriptionView)
def resume_subscription(sub_id: str, who: Owner) -> SubscriptionView:
    return SubscriptionView.of(subscription_service.resume(sub_id, owner=who))


@router.post("/subscriptions/{sub_id}/check", response_model=SubscriptionCheck, dependencies=[Depends(rate("submit"))])
def check_subscription(sub_id: str, who: Owner) -> SubscriptionCheck:
    """Poll now (also while paused); new videos are queued like a normal download. 409 while a poll is running."""
    sub, new, error = subscription_service.check(sub_id, owner=who)
    return SubscriptionCheck(subscription=SubscriptionView.of(sub), new=new, error=error)


@router.delete("/subscriptions/{sub_id}", status_code=204)
def delete_subscription(sub_id: str, who: Owner) -> None:
    """Unfollow (files already downloaded stay)."""
    subscription_service.delete(sub_id, owner=who)

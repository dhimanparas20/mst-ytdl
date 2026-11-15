"""
services/ — business-logic layer (AGENTS.md §2).
Public surface: import from here, never from service module paths directly.
"""

from services.access import AccessService, access_service
from services.admin_config import AdminConfigService, admin_config
from services.analytics import AnalyticsService, analytics
from services.cache import CacheService, cache_service
from services.cookies import CookieService, cookie_service
from services.delivery import DeliveryService, delivery_service
from services.dispatch import Dispatcher, get_dispatcher, set_dispatcher
from services.downloader import DownloadRunner, download_runner
from services.errors import (
    ConflictError,
    ForbiddenError,
    InvalidRequestError,
    NotFoundError,
    RateLimitedError,
    ServiceError,
    UnavailableError,
    UpstreamError,
)
from services.files import FileEntry, FileManagerService, file_manager
from services.library import LibraryEntry, LibraryService, library_service
from services.lyrics import LyricsService, lyrics_service
from services.maintenance import MaintenanceService, maintenance
from services.manager import DownloadManager, download_manager
from services.metadata import MetadataService, metadata_service
from services.runtime import RuntimeService, SlotLimiter, runtime_service, slot_limiter
from services.store import JobStore, job_store
from services.streaming import MediaStream, StreamService, stream_service

__all__ = [
    "AccessService",
    "AdminConfigService",
    "AnalyticsService",
    "FileEntry",
    "FileManagerService",
    "MaintenanceService",
    "file_manager",
    "maintenance",
    "analytics",
    "admin_config",
    "CacheService",
    "DeliveryService",
    "ForbiddenError",
    "RateLimitedError",
    "UnavailableError",
    "access_service",
    "cache_service",
    "delivery_service",
    "CookieService",
    "cookie_service",
    "ConflictError",
    "Dispatcher",
    "DownloadManager",
    "DownloadRunner",
    "InvalidRequestError",
    "JobStore",
    "LibraryEntry",
    "LibraryService",
    "LyricsService",
    "lyrics_service",
    "MetadataService",
    "MediaStream",
    "NotFoundError",
    "StreamService",
    "UpstreamError",
    "stream_service",
    "RuntimeService",
    "ServiceError",
    "SlotLimiter",
    "download_manager",
    "download_runner",
    "get_dispatcher",
    "job_store",
    "library_service",
    "metadata_service",
    "runtime_service",
    "set_dispatcher",
    "slot_limiter",
]

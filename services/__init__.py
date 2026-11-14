"""
services/ — business-logic layer (AGENTS.md §2).
Public surface: import from here, never from service module paths directly.
"""

from services.dispatch import Dispatcher, get_dispatcher, set_dispatcher
from services.downloader import DownloadRunner, download_runner
from services.errors import ConflictError, InvalidRequestError, NotFoundError, ServiceError, UpstreamError
from services.library import LibraryEntry, LibraryService, library_service
from services.manager import DownloadManager, download_manager
from services.metadata import MetadataService, metadata_service
from services.runtime import RuntimeService, SlotLimiter, runtime_service, slot_limiter
from services.store import JobStore, job_store
from services.streaming import MediaStream, StreamService, stream_service

__all__ = [
    "ConflictError",
    "Dispatcher",
    "DownloadManager",
    "DownloadRunner",
    "InvalidRequestError",
    "JobStore",
    "LibraryEntry",
    "LibraryService",
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

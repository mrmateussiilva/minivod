"""Compatibility bridge for the proven HLS/Xtream core during HTTP migration."""
from __future__ import annotations

import threading

from app.config import Settings
from app import hls_server as legacy


def configure(settings: Settings) -> None:
    settings.cache.mkdir(parents=True, exist_ok=True)
    settings.db.parent.mkdir(parents=True, exist_ok=True)
    legacy.CONFIG = legacy.Config(
        db=settings.db, cache=settings.cache, host="0.0.0.0", port=8079,
        segment_time=settings.segment_time, playlist_wait=settings.playlist_wait,
        transcode_incompatible=settings.transcode_incompatible,
        max_ffmpeg_jobs=settings.max_ffmpeg_jobs, base_url=settings.base_url,
        admin_user=settings.admin_user, admin_password=settings.admin_password,
    )
    legacy.ffmpeg_semaphore = threading.BoundedSemaphore(settings.max_ffmpeg_jobs)
    with legacy.raw_db_connect(settings.db) as conn:
        legacy.init_xtream_schema(conn)


def core():
    return legacy

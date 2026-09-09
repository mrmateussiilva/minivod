from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    db: Path
    cache: Path
    base_url: str | None
    admin_user: str | None
    admin_password: str | None
    segment_time: int
    playlist_wait: int
    max_ffmpeg_jobs: int
    transcode_incompatible: bool


def settings_from_env() -> Settings:
    return Settings(
        db=Path(os.getenv("MINIVOD_DB", "/data/vod.db")).expanduser().resolve(),
        cache=Path(os.getenv("MINIVOD_CACHE", "/cache")).expanduser().resolve(),
        base_url=os.getenv("MINIVOD_BASE_URL") or None,
        admin_user=os.getenv("MINIVOD_ADMIN_USER") or None,
        admin_password=os.getenv("MINIVOD_ADMIN_PASSWORD") or None,
        segment_time=max(2, int(os.getenv("MINIVOD_SEGMENT_TIME", "6"))),
        playlist_wait=max(1, int(os.getenv("MINIVOD_PLAYLIST_WAIT", "30"))),
        max_ffmpeg_jobs=max(1, int(os.getenv("MINIVOD_MAX_FFMPEG_JOBS", "2"))),
        transcode_incompatible=os.getenv("MINIVOD_COPY_ONLY", "0") != "1",
    )

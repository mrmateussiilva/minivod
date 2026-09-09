from __future__ import annotations

import hashlib
import html
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.cover_support import (
    find_collection_cover,
    find_collection_cover_candidates,
    image_content_type,
    is_image_file,
    is_within,
)
from app.db import connect

PASSWORD_ITERATIONS = 240_000


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "0 B"
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "0 B"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "00:00:00"
    value = max(0, int(seconds))
    hours, rem = divmod(value, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def display_collection_name(name: object) -> str:
    return "Outros" if str(name) == "[ROOT]" else str(name or "Sem Coleção")


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    if salt_hex is None:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return digest.hex(), salt_hex


def get_dashboard_stats(db_path: Path, cache_path: Path) -> dict[str, Any]:
    with connect(db_path, readonly=True) as conn:
        stats = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM videos WHERE active = 1) AS total_videos,
                (SELECT COUNT(*) FROM collections WHERE active = 1 AND video_count > 0) AS total_collections,
                (SELECT COUNT(*) FROM xtream_users WHERE enabled = 1) AS active_users,
                (SELECT COUNT(*) FROM xtream_users) AS total_users,
                COALESCE((SELECT SUM(size_bytes) FROM videos WHERE active = 1), 0) AS total_bytes,
                (SELECT COUNT(*) FROM collections WHERE active = 1 AND video_count > 0 AND cover_path IS NOT NULL) AS configured_covers
            """
        ).fetchone()

        largest = conn.execute(
            """
            SELECT id, name, video_count
            FROM collections
            WHERE active = 1 AND video_count > 0
            ORDER BY video_count DESC
            LIMIT 6
            """
        ).fetchall()

        library = conn.execute(
            "SELECT path FROM collections WHERE path IS NOT NULL AND active = 1 LIMIT 1"
        ).fetchone()

    total_collections = int(stats["total_collections"] if stats else 0)
    configured_covers = int(stats["configured_covers"] if stats else 0)
    missing_covers = max(0, total_collections - configured_covers)

    library_root = (
        str(Path(str(library["path"])).parent)
        if library and library["path"]
        else "Automático / Relativo"
    )

    return {
        "total_videos": int(stats["total_videos"] if stats else 0),
        "total_collections": total_collections,
        "active_users": int(stats["active_users"] if stats else 0),
        "total_users": int(stats["total_users"] if stats else 0),
        "total_bytes": int(stats["total_bytes"] if stats else 0),
        "total_bytes_formatted": format_bytes(int(stats["total_bytes"] if stats else 0)),
        "configured_covers": configured_covers,
        "missing_covers": missing_covers,
        "largest_collections": [
            {
                "id": int(row["id"]),
                "name": display_collection_name(row["name"]),
                "video_count": int(row["video_count"]),
            }
            for row in largest
        ],
        "db_path": str(db_path),
        "cache_path": str(cache_path),
        "library_root": library_root,
    }


def list_admin_collections(db_path: Path) -> list[dict[str, Any]]:
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT id, name, slug, path, cover_path, video_count, active, updated_at
            FROM collections
            WHERE active = 1
            ORDER BY CASE WHEN name = '[ROOT]' THEN 'Outros' ELSE name END COLLATE NOCASE
            """
        ).fetchall()

    results = []
    for row in rows:
        cover_exists = bool(row["cover_path"] and is_image_file(Path(str(row["cover_path"]))))
        results.append(
            {
                "id": int(row["id"]),
                "name": display_collection_name(row["name"]),
                "raw_name": str(row["name"]),
                "slug": str(row["slug"]),
                "video_count": int(row["video_count"]),
                "has_cover": cover_exists,
                "cover_url": f"/covers/{row['id']}" if cover_exists else None,
                "updated_at": str(row["updated_at"]),
            }
        )
    return results


def get_collection_details(
    db_path: Path, collection_id: int, page: int = 1, per_page: int = 25
) -> dict[str, Any] | None:
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    offset = (page - 1) * per_page

    with connect(db_path, readonly=True) as conn:
        collection = conn.execute(
            """
            SELECT id, name, slug, path, cover_path, video_count, active, created_at, updated_at
            FROM collections
            WHERE id = ? AND active = 1
            """,
            (collection_id,),
        ).fetchone()

        if collection is None:
            return None

        total_videos = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE collection_id = ? AND active = 1",
            (collection_id,),
        ).fetchone()[0]

        videos = conn.execute(
            """
            SELECT id, title, filename, size_bytes, duration, video_codec, audio_codec, width, height, active
            FROM videos
            WHERE collection_id = ? AND active = 1
            ORDER BY id
            LIMIT ? OFFSET ?
            """,
            (collection_id, per_page, offset),
        ).fetchall()

    cover_exists = bool(
        collection["cover_path"] and is_image_file(Path(str(collection["cover_path"])))
    )

    total_pages = max(1, math.ceil(total_videos / per_page))

    return {
        "id": int(collection["id"]),
        "name": display_collection_name(collection["name"]),
        "slug": str(collection["slug"]),
        "path": str(collection["path"] or ""),
        "has_cover": cover_exists,
        "cover_url": f"/covers/{collection['id']}" if cover_exists else None,
        "total_videos": total_videos,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "videos": [
            {
                "id": int(v["id"]),
                "title": str(v["title"]),
                "filename": str(v["filename"]),
                "size_formatted": format_bytes(v["size_bytes"]),
                "duration_formatted": format_duration(v["duration"]),
                "video_codec": str(v["video_codec"] or "desconhecido"),
                "audio_codec": str(v["audio_codec"] or "desconhecido"),
                "resolution": f"{v['width']}x{v['height']}" if v["width"] and v["height"] else "-",
            }
            for v in videos
        ],
    }


def list_admin_videos(
    db_path: Path,
    q: str = "",
    collection_id: int | None = None,
    status: str = "active",
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[dict[str, Any]], int, int, list[dict[str, Any]]]:
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    offset = (page - 1) * per_page

    where_clauses: list[str] = []
    args: list[Any] = []

    if status == "active":
        where_clauses.append("v.active = 1")
    elif status == "inactive":
        where_clauses.append("v.active = 0")

    if collection_id and collection_id > 0:
        where_clauses.append("v.collection_id = ?")
        args.append(collection_id)

    q = q.strip()
    if q:
        where_clauses.append("(v.title LIKE ? OR v.filename LIKE ? OR v.relative_path LIKE ?)")
        term = f"%{q}%"
        args.extend([term, term, term])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with connect(db_path, readonly=True) as conn:
        collections = conn.execute(
            """
            SELECT id, name
            FROM collections
            WHERE active = 1
            ORDER BY CASE WHEN name = '[ROOT]' THEN 'Outros' ELSE name END COLLATE NOCASE
            """
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM videos v {where_sql}",
            args,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT
                v.id, v.title, v.filename, v.size_bytes, v.duration,
                v.video_codec, v.audio_codec, v.width, v.height, v.fps,
                v.active, v.probe_ok, v.created_at,
                c.id AS collection_id, c.name AS collection_name
            FROM videos v
            LEFT JOIN collections c ON c.id = v.collection_id
            {where_sql}
            ORDER BY v.id DESC
            LIMIT ? OFFSET ?
            """,
            [*args, per_page, offset],
        ).fetchall()

    total_pages = max(1, math.ceil(total / per_page))

    items = []
    for r in rows:
        items.append(
            {
                "id": int(r["id"]),
                "title": str(r["title"]),
                "filename": str(r["filename"]),
                "collection_id": int(r["collection_id"]) if r["collection_id"] else None,
                "collection_name": display_collection_name(r["collection_name"]) if r["collection_name"] else "Sem coleção",
                "size_formatted": format_bytes(r["size_bytes"]),
                "duration_formatted": format_duration(r["duration"]),
                "video_codec": str(r["video_codec"] or "desconhecido"),
                "audio_codec": str(r["audio_codec"] or "desconhecido"),
                "resolution": f"{r['width']}x{r['height']}" if r["width"] and r["height"] else "-",
                "active": bool(r["active"]),
                "probe_ok": bool(r["probe_ok"]),
            }
        )

    col_list = [
        {"id": int(c["id"]), "name": display_collection_name(c["name"])}
        for c in collections
    ]

    return items, total, total_pages, col_list


def get_video_details(db_path: Path, cache_path: Path, video_id: int) -> dict[str, Any] | None:
    with connect(db_path, readonly=True) as conn:
        video = conn.execute(
            """
            SELECT
                v.*,
                c.id AS collection_id,
                c.name AS collection_name,
                c.cover_path AS collection_cover_path
            FROM videos v
            LEFT JOIN collections c ON c.id = v.collection_id
            WHERE v.id = ?
            """,
            (video_id,),
        ).fetchone()

    if video is None:
        return None

    video_cache_dir = cache_path / str(video_id)
    playlist = video_cache_dir / "index.m3u8"
    playlist_exists = playlist.exists() and playlist.stat().st_size > 0
    complete = False
    if playlist_exists:
        try:
            complete = "#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            complete = False

    segments_count = 0
    if video_cache_dir.exists():
        segments_count = sum(1 for item in video_cache_dir.glob("segment-*.ts") if item.is_file())

    cover_exists = bool(
        video["collection_cover_path"] and is_image_file(Path(str(video["collection_cover_path"])))
    )

    return {
        "id": int(video["id"]),
        "title": str(video["title"]),
        "filename": str(video["filename"]),
        "path": str(video["path"]),
        "relative_path": str(video["relative_path"]),
        "collection_id": int(video["collection_id"]) if video["collection_id"] else None,
        "collection_name": display_collection_name(video["collection_name"]) if video["collection_name"] else "Sem coleção",
        "has_cover": cover_exists,
        "cover_url": f"/covers/{video['collection_id']}" if cover_exists and video["collection_id"] else None,
        "size_bytes": int(video["size_bytes"] or 0),
        "size_formatted": format_bytes(video["size_bytes"]),
        "duration": float(video["duration"] or 0),
        "duration_formatted": format_duration(video["duration"]),
        "format_name": str(video["format_name"] or "-"),
        "bitrate": int(video["bitrate"] or 0),
        "bitrate_formatted": f"{int((video['bitrate'] or 0) / 1000)} kbps" if video["bitrate"] else "-",
        "video_codec": str(video["video_codec"] or "-"),
        "audio_codec": str(video["audio_codec"] or "-"),
        "width": video["width"],
        "height": video["height"],
        "resolution": f"{video['width']}x{video['height']}" if video["width"] and video["height"] else "-",
        "fps": f"{float(video['fps']):.2f}" if video["fps"] else "-",
        "active": bool(video["active"]),
        "probe_ok": bool(video["probe_ok"]),
        "probe_error": str(video["probe_error"] or "") if not video["probe_ok"] else None,
        "created_at": str(video["created_at"]),
        "hls_url": f"/vod/{video_id}/index.m3u8",
        "cache_exists": playlist_exists,
        "cache_complete": complete,
        "segments_count": segments_count,
    }


def list_admin_users(db_path: Path) -> list[dict[str, Any]]:
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT id, username, enabled, max_connections, exp_date, created_at, updated_at
            FROM xtream_users
            ORDER BY username COLLATE NOCASE
            """
        ).fetchall()

    users = []
    now = int(time.time())
    for r in rows:
        exp_date = r["exp_date"]
        is_expired = exp_date is not None and int(exp_date) <= now
        exp_str = "Nunca expira"
        if exp_date is not None:
            exp_str = datetime.fromtimestamp(int(exp_date), tz=timezone.utc).strftime("%Y-%m-%d")

        users.append(
            {
                "id": int(r["id"]),
                "username": str(r["username"]),
                "enabled": bool(r["enabled"]),
                "is_expired": is_expired,
                "status_label": "Expirado" if is_expired else ("Ativo" if r["enabled"] else "Desativado"),
                "max_connections": int(r["max_connections"] or 1),
                "expires_str": exp_str,
                "created_at": datetime.fromtimestamp(int(r["created_at"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if r["created_at"] else "-",
            }
        )
    return users


def create_admin_user(
    db_path: Path,
    username: str,
    password: str,
    max_connections: int,
    expires_days: int | None,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", username):
        raise ValueError("O nome de usuário deve ter entre 3 e 64 caracteres e conter apenas letras, números, '.', '_' ou '-'.")
    if len(password) < 6:
        raise ValueError("A senha deve ter pelo menos 6 caracteres.")
    if max_connections < 1:
        raise ValueError("O máximo de conexões deve ser ao menos 1.")
    if expires_days is not None and expires_days < 1:
        raise ValueError("Os dias de expiração devem ser pelo menos 1.")

    password_hash, salt = hash_password(password)
    now = int(time.time())
    exp_date = now + expires_days * 86400 if expires_days else None

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO xtream_users (
                username, password_hash, password_salt, enabled,
                max_connections, exp_date, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (username, password_hash, salt, max_connections, exp_date, now, now),
        )


def set_user_status(db_path: Path, user_id: int, enabled: bool) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE xtream_users
            SET enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if enabled else 0, int(time.time()), user_id),
        )
        return cur.rowcount > 0


def set_user_password(db_path: Path, user_id: int, new_password: str) -> bool:
    if len(new_password) < 6:
        raise ValueError("A nova senha deve ter pelo menos 6 caracteres.")

    password_hash, salt = hash_password(new_password)
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE xtream_users
            SET password_hash = ?, password_salt = ?, updated_at = ?
            WHERE id = ?
            """,
            (password_hash, salt, int(time.time()), user_id),
        )
        return cur.rowcount > 0


def get_collection_cover_record(db_path: Path, collection_id: int) -> dict[str, Any] | None:
    with connect(db_path, readonly=True) as conn:
        row = conn.execute(
            """
            SELECT c.id, c.name, c.path, c.cover_path,
                (SELECT v.path FROM videos v WHERE v.collection_id = c.id
                 AND v.active = 1 ORDER BY v.id LIMIT 1) AS sample_video_path
            FROM collections c
            WHERE c.id = ? AND c.active = 1
            """,
            (collection_id,),
        ).fetchone()

    if row is None:
        return None

    root = None
    if row["path"]:
        root = Path(str(row["path"]))
    elif row["sample_video_path"]:
        root = Path(str(row["sample_video_path"])).parent

    return {
        "id": int(row["id"]),
        "name": display_collection_name(row["name"]),
        "root": root,
        "cover_path": row["cover_path"],
    }


def get_collection_cover_candidates(
    db_path: Path, collection_id: int, page: int = 1, per_page: int = 24
) -> tuple[list[dict[str, str]], int, int, str] | None:
    record = get_collection_cover_record(db_path, collection_id)
    if record is None or not record["root"]:
        return None

    root = record["root"]
    all_candidates = find_collection_cover_candidates(root)
    total = len(all_candidates)
    page = max(1, page)
    per_page = max(1, min(per_page, 60))
    total_pages = max(1, math.ceil(total / per_page))

    offset = (page - 1) * per_page
    sliced = all_candidates[offset : offset + per_page]

    candidates = []
    for path in sliced:
        relative = str(path.relative_to(root))
        candidates.append(
            {
                "relative_path": relative,
                "preview_url": f"/admin/collections/{collection_id}/cover-preview?path={relative}",
            }
        )

    return candidates, total, total_pages, record["name"]


def set_collection_cover(db_path: Path, collection_id: int, relative_path: str) -> bool:
    record = get_collection_cover_record(db_path, collection_id)
    if record is None or not record["root"]:
        return False

    root = record["root"]
    candidate = root / relative_path
    if not is_within(candidate, root) or not is_image_file(candidate):
        return False

    with connect(db_path) as conn:
        conn.execute(
            "UPDATE collections SET cover_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (str(candidate.resolve()), collection_id),
        )
    return True


def auto_select_collection_cover(db_path: Path, collection_id: int) -> bool:
    record = get_collection_cover_record(db_path, collection_id)
    if record is None or not record["root"]:
        return False

    root = record["root"]
    cover = find_collection_cover(root) if root else None
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE collections SET cover_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (str(cover.resolve()) if cover else None, collection_id),
        )
    return True


def remove_collection_cover(db_path: Path, collection_id: int) -> bool:
    record = get_collection_cover_record(db_path, collection_id)
    if record is None:
        return False

    with connect(db_path) as conn:
        conn.execute(
            "UPDATE collections SET cover_path = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (collection_id,),
        )
    return True

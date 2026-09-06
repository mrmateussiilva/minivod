#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from cover_support import ensure_cover_column, find_collection_cover, is_image_file


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".m4v",
    ".webm",
    ".ts",
    ".m2ts",
    ".mpeg",
    ".mpg",
}


@dataclass(slots=True)
class ProbeResult:
    duration: float | None
    format_name: str | None
    bitrate: int | None
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    fps: float | None


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            path TEXT,
            cover_path TEXT,
            video_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id INTEGER,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            relative_path TEXT NOT NULL UNIQUE,

            size_bytes INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL DEFAULT 0,

            duration REAL,
            format_name TEXT,
            bitrate INTEGER,

            video_codec TEXT,
            audio_codec TEXT,

            width INTEGER,
            height INTEGER,
            fps REAL,

            active INTEGER NOT NULL DEFAULT 1,
            probe_ok INTEGER NOT NULL DEFAULT 0,
            probe_error TEXT,

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY(collection_id)
                REFERENCES collections(id)
                ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_videos_collection
            ON videos(collection_id);

        CREATE INDEX IF NOT EXISTS idx_videos_active
            ON videos(active);

        CREATE INDEX IF NOT EXISTS idx_videos_mtime
            ON videos(mtime_ns);

        CREATE INDEX IF NOT EXISTS idx_videos_relative_path
            ON videos(relative_path);
        """
    )
    ensure_cover_column(conn)
    conn.commit()


def slugify(value: str) -> str:
    import re
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in normalized
        if not unicodedata.combining(c)
    )

    value = value.lower().strip()
    value = value.replace("&", " e ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)

    return value.strip("-") or "root"


def title_from_filename(path: Path) -> str:
    stem = path.stem

    # remove hífens só para exibição; o filename real permanece intacto
    words = stem.replace("-", " ").split()

    if not words:
        return stem

    return " ".join(words)


def is_video(path: Path) -> bool:
    name = path.name.lower()

    if name.endswith(".part"):
        return False

    return path.suffix.lower() in VIDEO_EXTENSIONS


def discover_videos(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and is_video(path)
        ),
        key=lambda p: str(p.relative_to(root)).casefold(),
    )


def get_collection(root: Path, path: Path) -> tuple[str, str, str | None]:
    relative = path.relative_to(root)

    if len(relative.parts) == 1:
        name = "[ROOT]"
        return name, "root", None

    name = relative.parts[0]
    return name, slugify(name), str(root / name)


def parse_fps(value: str | None) -> float | None:
    if not value:
        return None

    if "/" in value:
        numerator, denominator = value.split("/", 1)

        try:
            numerator_f = float(numerator)
            denominator_f = float(denominator)

            if denominator_f == 0:
                return None

            return numerator_f / denominator_f
        except ValueError:
            return None

    try:
        return float(value)
    except ValueError:
        return None


def ffprobe(path: Path) -> ProbeResult:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        (
            "format=duration,format_name,bit_rate:"
            "stream=index,codec_type,codec_name,width,height,"
            "avg_frame_rate"
        ),
        "-of", "json",
        str(path),
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )

    if result.returncode != 0:
        error = result.stderr.strip() or "ffprobe falhou"
        raise RuntimeError(error)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON inválido do ffprobe: {exc}") from exc

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    duration = None
    bitrate = None

    try:
        if fmt.get("duration") is not None:
            duration = float(fmt["duration"])
    except (TypeError, ValueError):
        pass

    try:
        if fmt.get("bit_rate") is not None:
            bitrate = int(fmt["bit_rate"])
    except (TypeError, ValueError):
        pass

    video_stream = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
        ),
        None,
    )

    audio_stream = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "audio"
        ),
        None,
    )

    video_codec = None
    audio_codec = None
    width = None
    height = None
    fps = None

    if video_stream:
        video_codec = video_stream.get("codec_name")

        try:
            width = int(video_stream["width"])
        except (KeyError, TypeError, ValueError):
            pass

        try:
            height = int(video_stream["height"])
        except (KeyError, TypeError, ValueError):
            pass

        fps = parse_fps(video_stream.get("avg_frame_rate"))

    if audio_stream:
        audio_codec = audio_stream.get("codec_name")

    return ProbeResult(
        duration=duration,
        format_name=fmt.get("format_name"),
        bitrate=bitrate,
        video_codec=video_codec,
        audio_codec=audio_codec,
        width=width,
        height=height,
        fps=fps,
    )


def ensure_collection(
    conn: sqlite3.Connection,
    name: str,
    slug: str,
    path: str | None,
) -> int:
    conn.execute(
        """
        INSERT INTO collections (
            name,
            slug,
            path,
            active
        )
        VALUES (?, ?, ?, 1)
        ON CONFLICT(name)
        DO UPDATE SET
            slug = excluded.slug,
            path = excluded.path,
            active = 1,
            updated_at = CURRENT_TIMESTAMP
        """,
        (name, slug, path),
    )

    row = conn.execute(
        "SELECT id FROM collections WHERE name = ?",
        (name,),
    ).fetchone()

    if row is None:
        raise RuntimeError(f"não foi possível obter coleção: {name}")

    return int(row["id"])


def ensure_collection_cover(
    conn: sqlite3.Connection,
    collection_id: int,
    collection_root: Path,
) -> bool:
    """Keep a valid selected cover stable; choose only when it is missing."""
    row = conn.execute(
        "SELECT cover_path FROM collections WHERE id = ?",
        (collection_id,),
    ).fetchone()
    current = Path(str(row["cover_path"])) if row and row["cover_path"] else None
    if current is not None and is_image_file(current):
        return False

    cover = find_collection_cover(collection_root)
    conn.execute(
        "UPDATE collections SET cover_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (str(cover) if cover else None, collection_id),
    )
    return cover is not None


def existing_video(
    conn: sqlite3.Connection,
    absolute_path: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            id,
            size_bytes,
            mtime_ns,
            probe_ok
        FROM videos
        WHERE path = ?
        """,
        (absolute_path,),
    ).fetchone()


def mark_all_inactive(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE videos SET active = 0")
    conn.execute("UPDATE collections SET active = 0")
    conn.commit()


def refresh_collection_counts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE collections
        SET
            video_count = (
                SELECT COUNT(*)
                FROM videos
                WHERE videos.collection_id = collections.id
                  AND videos.active = 1
            ),
            updated_at = CURRENT_TIMESTAMP
        """
    )
    conn.commit()


def scan(
    root: Path,
    db_path: Path,
    force_probe: bool,
) -> None:
    if shutil.which("ffprobe") is None:
        raise SystemExit(
            "ERRO: ffprobe não encontrado. "
            "Instale com: sudo apt install ffmpeg"
        )

    root = root.expanduser().resolve()
    db_path = db_path.expanduser().resolve()

    if not root.exists() or not root.is_dir():
        raise SystemExit(f"ERRO: diretório inválido: {root}")

    conn = connect(db_path)

    try:
        init_db(conn)
        mark_all_inactive(conn)

        videos = discover_videos(root)

        stats = {
            "found": len(videos),
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "probed": 0,
            "probe_errors": 0,
            "covers_found": 0,
            "covers_missing": 0,
        }
        checked_covers: set[int] = set()

        started = time.monotonic()

        print(f"Raiz:  {root}")
        print(f"Banco: {db_path}")
        print(f"Vídeos encontrados: {len(videos)}")
        print()

        for number, path in enumerate(videos, start=1):
            try:
                stat = path.stat()
            except OSError as exc:
                print(f"[{number}/{len(videos)}] ERRO stat: {path}: {exc}")
                continue

            absolute_path = str(path.resolve())
            relative_path = str(path.relative_to(root))

            collection_name, collection_slug, collection_path = get_collection(
                root,
                path,
            )

            collection_id = ensure_collection(
                conn,
                collection_name,
                collection_slug,
                collection_path,
            )

            if collection_id not in checked_covers:
                checked_covers.add(collection_id)
                collection_root = Path(collection_path) if collection_path else root
                if ensure_collection_cover(conn, collection_id, collection_root):
                    stats["covers_found"] += 1
                else:
                    cover_row = conn.execute(
                        "SELECT cover_path FROM collections WHERE id = ?",
                        (collection_id,),
                    ).fetchone()
                    if not cover_row or not cover_row["cover_path"]:
                        stats["covers_missing"] += 1

            current = existing_video(conn, absolute_path)

            unchanged = (
                current is not None
                and int(current["size_bytes"]) == stat.st_size
                and int(current["mtime_ns"]) == stat.st_mtime_ns
                and not force_probe
            )

            if unchanged:
                conn.execute(
                    """
                    UPDATE videos
                    SET
                        collection_id = ?,
                        title = ?,
                        filename = ?,
                        relative_path = ?,
                        active = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        collection_id,
                        title_from_filename(path),
                        path.name,
                        relative_path,
                        current["id"],
                    ),
                )

                stats["unchanged"] += 1

                if number % 100 == 0 or number == len(videos):
                    print(
                        f"[{number}/{len(videos)}] "
                        f"cache: {relative_path}"
                    )

                continue

            probe: ProbeResult | None = None
            probe_error: str | None = None

            try:
                probe = ffprobe(path)
                stats["probed"] += 1
            except Exception as exc:
                probe_error = str(exc)[:1000]
                stats["probe_errors"] += 1

            if probe is None:
                probe = ProbeResult(
                    duration=None,
                    format_name=None,
                    bitrate=None,
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    fps=None,
                )

            if current is None:
                conn.execute(
                    """
                    INSERT INTO videos (
                        collection_id,
                        title,
                        filename,
                        path,
                        relative_path,
                        size_bytes,
                        mtime_ns,
                        duration,
                        format_name,
                        bitrate,
                        video_codec,
                        audio_codec,
                        width,
                        height,
                        fps,
                        active,
                        probe_ok,
                        probe_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        collection_id,
                        title_from_filename(path),
                        path.name,
                        absolute_path,
                        relative_path,
                        stat.st_size,
                        stat.st_mtime_ns,
                        probe.duration,
                        probe.format_name,
                        probe.bitrate,
                        probe.video_codec,
                        probe.audio_codec,
                        probe.width,
                        probe.height,
                        probe.fps,
                        0 if probe_error else 1,
                        probe_error,
                    ),
                )

                stats["new"] += 1
                action = "NOVO"

            else:
                conn.execute(
                    """
                    UPDATE videos
                    SET
                        collection_id = ?,
                        title = ?,
                        filename = ?,
                        relative_path = ?,
                        size_bytes = ?,
                        mtime_ns = ?,
                        duration = ?,
                        format_name = ?,
                        bitrate = ?,
                        video_codec = ?,
                        audio_codec = ?,
                        width = ?,
                        height = ?,
                        fps = ?,
                        active = 1,
                        probe_ok = ?,
                        probe_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        collection_id,
                        title_from_filename(path),
                        path.name,
                        relative_path,
                        stat.st_size,
                        stat.st_mtime_ns,
                        probe.duration,
                        probe.format_name,
                        probe.bitrate,
                        probe.video_codec,
                        probe.audio_codec,
                        probe.width,
                        probe.height,
                        probe.fps,
                        0 if probe_error else 1,
                        probe_error,
                        current["id"],
                    ),
                )

                stats["updated"] += 1
                action = "UPDATE"

            if probe_error:
                print(
                    f"[{number}/{len(videos)}] "
                    f"{action} ERRO ffprobe: {relative_path}"
                )
            else:
                resolution = (
                    f"{probe.width}x{probe.height}"
                    if probe.width and probe.height
                    else "?"
                )

                print(
                    f"[{number}/{len(videos)}] "
                    f"{action}: {relative_path} "
                    f"[{probe.video_codec or '?'} {resolution}]"
                )

            if number % 50 == 0:
                conn.commit()

        conn.commit()

        refresh_collection_counts(conn)

        inactive = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE active = 0"
        ).fetchone()[0]

        active = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE active = 1"
        ).fetchone()[0]

        collections = conn.execute(
            """
            SELECT COUNT(*)
            FROM collections
            WHERE active = 1
              AND video_count > 0
            """
        ).fetchone()[0]

        cover_rows = conn.execute(
            """
            SELECT cover_path
            FROM collections
            WHERE active = 1
              AND video_count > 0
            """
        ).fetchall()
        covers_available = sum(
            1
            for row in cover_rows
            if row["cover_path"] and is_image_file(Path(str(row["cover_path"])))
        )

        elapsed = time.monotonic() - started

        print()
        print("=" * 72)
        print("SCAN FINALIZADO")
        print("=" * 72)
        print(f"Encontrados:       {stats['found']}")
        print(f"Novos:             {stats['new']}")
        print(f"Atualizados:       {stats['updated']}")
        print(f"Sem mudanças:      {stats['unchanged']}")
        print(f"ffprobe executado: {stats['probed']}")
        print(f"Erros ffprobe:     {stats['probe_errors']}")
        print(f"Ativos no banco:   {active}")
        print(f"Ausentes/inativos: {inactive}")
        print(f"Coleções ativas:   {collections}")
        print(f"Capas disponíveis: {covers_available}")
        print(f"Sem capa:          {collections - covers_available}")
        print(f"Tempo:              {elapsed:.1f}s")
        print(f"Banco:              {db_path}")

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Indexa uma biblioteca VOD em SQLite usando ffprobe "
            "de forma incremental."
        )
    )

    parser.add_argument(
        "path",
        nargs="?",
        default="~/downloads/iptv",
        help="diretório raiz da biblioteca",
    )

    parser.add_argument(
        "--db",
        default="~/downloads/vod.db",
        help="arquivo SQLite de destino",
    )

    parser.add_argument(
        "--force-probe",
        action="store_true",
        help="executa ffprobe novamente em todos os vídeos",
    )

    args = parser.parse_args()

    scan(
        root=Path(args.path),
        db_path=Path(args.db),
        force_probe=args.force_probe,
    )


if __name__ == "__main__":
    main()

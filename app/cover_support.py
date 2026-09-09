from __future__ import annotations

import sqlite3
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PRIORITY_FILENAMES = {
    f"{name}{extension}"
    for name in ("cover", "poster", "folder", "capa")
    for extension in IMAGE_EXTENSIONS
}
AVOID_NAME_PARTS = {
    "thumb",
    "thumbnail",
    "watermark",
    "preview",
    "avatar",
    "icon",
    "logo",
    "sprite",
}
PHOTO_DIRECTORIES = {"fotos", "photos", "images", "pictures", "screens"}
IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def ensure_cover_column(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("PRAGMA table_info(collections)")
    }
    if columns and "cover_path" not in columns:
        conn.execute("ALTER TABLE collections ADD COLUMN cover_path TEXT")
        conn.commit()


def image_content_type(path: Path) -> str | None:
    return IMAGE_CONTENT_TYPES.get(path.suffix.casefold())


def is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _sorted_images(root: Path) -> list[Path]:
    if not root.is_dir():
        return []

    images: list[Path] = []
    for path in root.rglob("*"):
        if is_image_file(path) and is_within(path, root):
            images.append(path)
    return sorted(images, key=lambda item: str(item.relative_to(root)).casefold())


def _prefer_non_auxiliary(paths: list[Path]) -> list[Path]:
    preferred = [
        path
        for path in paths
        if not any(part in path.name.casefold() for part in AVOID_NAME_PARTS)
    ]
    return preferred or paths


def find_collection_cover_candidates(root: Path) -> list[Path]:
    """Return deterministic candidates in the same order as auto-selection."""
    root = root.resolve()
    images = _sorted_images(root)
    if not images:
        return []

    usable = _prefer_non_auxiliary(images)
    explicit = [path for path in usable if path.name.casefold() in PRIORITY_FILENAMES]
    root_images = [path for path in usable if path.parent == root]
    photo_images = [
        path
        for path in usable
        if any(part.casefold() in PHOTO_DIRECTORIES for part in path.relative_to(root).parts[:-1])
    ]

    ordered: list[Path] = []
    for group in (explicit, root_images, photo_images, usable):
        for path in group:
            if path not in ordered:
                ordered.append(path)
    return ordered


def find_collection_cover(root: Path) -> Path | None:
    candidates = find_collection_cover_candidates(root)
    return candidates[0] if candidates else None

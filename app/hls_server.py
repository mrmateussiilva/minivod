#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


SEGMENT_RE = re.compile(r"^segment-\d{6}\.ts$")
PLAYLIST_NAME = "index.m3u8"
MANIFEST_NAME = ".source.json"
LOG_NAME = "ffmpeg.log"

VIDEO_COPY_CODECS = {"h264"}
AUDIO_COPY_CODECS = {"aac"}

PASSWORD_ITERATIONS = 240_000

generation_lock = threading.Lock()
generation_processes: dict[int, subprocess.Popen] = {}


@dataclass(slots=True)
class Config:
    db: Path
    cache: Path
    host: str
    port: int
    segment_time: int
    playlist_wait: int
    transcode_incompatible: bool
    base_url: str | None
    admin_user: str | None
    admin_password: str | None


CONFIG: Config


# =====================================================================
# DATABASE
# =====================================================================

def db_connect(readonly: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.db, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")

    if readonly:
        conn.execute("PRAGMA query_only=ON")

    return conn


def raw_db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_xtream_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS xtream_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_connections INTEGER NOT NULL DEFAULT 1,
            exp_date INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_xtream_users_username
            ON xtream_users(username);
        """
    )
    conn.commit()


def get_video(video_id: int) -> sqlite3.Row | None:
    with db_connect(readonly=True) as conn:
        return conn.execute(
            """
            SELECT
                v.id,
                v.collection_id,
                v.title,
                v.filename,
                v.path,
                v.relative_path,
                v.size_bytes,
                v.mtime_ns,
                v.duration,
                v.format_name,
                v.bitrate,
                v.video_codec,
                v.audio_codec,
                v.width,
                v.height,
                v.fps,
                v.active,
                v.probe_ok,
                v.created_at,
                c.name AS collection_name
            FROM videos v
            LEFT JOIN collections c ON c.id = v.collection_id
            WHERE v.id = ?
              AND v.active = 1
            LIMIT 1
            """,
            (video_id,),
        ).fetchone()


# =====================================================================
# USERS / AUTH
# =====================================================================

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


def verify_password(password: str, expected_hash: str, salt_hex: str) -> bool:
    actual_hash, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(actual_hash, expected_hash)


def lookup_user(username: str) -> sqlite3.Row | None:
    with db_connect(readonly=True) as conn:
        return conn.execute(
            """
            SELECT *
            FROM xtream_users
            WHERE username = ?
            LIMIT 1
            """,
            (username,),
        ).fetchone()


def authenticate(username: str, password: str) -> sqlite3.Row | None:
    if not username or not password:
        return None

    user = lookup_user(username)

    if user is None:
        return None

    if not int(user["enabled"]):
        return None

    exp_date = user["exp_date"]
    if exp_date is not None and int(exp_date) <= int(time.time()):
        return None

    if not verify_password(
        password,
        str(user["password_hash"]),
        str(user["password_salt"]),
    ):
        return None

    return user


def create_or_update_user(
    db_path: Path,
    username: str,
    max_connections: int,
    expires_days: int | None,
) -> None:
    password = getpass.getpass(f"Senha para '{username}': ")
    confirm = getpass.getpass("Repita a senha: ")

    if not password:
        raise SystemExit("ERRO: senha vazia.")

    if password != confirm:
        raise SystemExit("ERRO: as senhas não conferem.")

    password_hash, salt = hash_password(password)
    now = int(time.time())

    exp_date = None
    if expires_days is not None:
        exp_date = now + max(1, expires_days) * 86400

    with raw_db_connect(db_path) as conn:
        init_xtream_schema(conn)

        conn.execute(
            """
            INSERT INTO xtream_users (
                username,
                password_hash,
                password_salt,
                enabled,
                max_connections,
                exp_date,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(username)
            DO UPDATE SET
                password_hash = excluded.password_hash,
                password_salt = excluded.password_salt,
                enabled = 1,
                max_connections = excluded.max_connections,
                exp_date = excluded.exp_date,
                updated_at = excluded.updated_at
            """,
            (
                username,
                password_hash,
                salt,
                max(1, max_connections),
                exp_date,
                now,
                now,
            ),
        )
        conn.commit()

    print(f"Usuário '{username}' criado/atualizado.")


def set_user_enabled(db_path: Path, username: str, enabled: bool) -> None:
    now = int(time.time())

    with raw_db_connect(db_path) as conn:
        init_xtream_schema(conn)

        cur = conn.execute(
            """
            UPDATE xtream_users
            SET enabled = ?, updated_at = ?
            WHERE username = ?
            """,
            (1 if enabled else 0, now, username),
        )
        conn.commit()

        if cur.rowcount == 0:
            raise SystemExit(f"ERRO: usuário não encontrado: {username}")

    print(
        f"Usuário '{username}' "
        + ("habilitado." if enabled else "desabilitado.")
    )


def list_users(db_path: Path) -> None:
    with raw_db_connect(db_path) as conn:
        init_xtream_schema(conn)

        rows = conn.execute(
            """
            SELECT username, enabled, max_connections, exp_date, created_at
            FROM xtream_users
            ORDER BY username COLLATE NOCASE
            """
        ).fetchall()

    if not rows:
        print("Nenhum usuário Xtream.")
        return

    print(f"{'USUÁRIO':<24} {'ATIVO':<7} {'MAX':<5} {'EXPIRA'}")
    print("-" * 64)

    for row in rows:
        exp = "nunca"
        if row["exp_date"] is not None:
            exp = datetime.fromtimestamp(
                int(row["exp_date"]),
                tz=timezone.utc,
            ).isoformat()

        print(
            f"{row['username']:<24} "
            f"{'sim' if row['enabled'] else 'não':<7} "
            f"{row['max_connections']:<5} "
            f"{exp}"
        )


def admin_users() -> list[sqlite3.Row]:
    with db_connect(readonly=True) as conn:
        return conn.execute(
            """
            SELECT id, username, enabled, max_connections, exp_date
            FROM xtream_users
            ORDER BY username COLLATE NOCASE
            """
        ).fetchall()


def admin_create_user(
    username: str,
    password: str,
    max_connections: int,
    expires_days: int | None,
) -> None:
    password_hash, salt = hash_password(password)
    now = int(time.time())
    exp_date = None if expires_days is None else now + expires_days * 86400

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO xtream_users (
                username, password_hash, password_salt, enabled,
                max_connections, exp_date, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                username,
                password_hash,
                salt,
                max_connections,
                exp_date,
                now,
                now,
            ),
        )
        conn.commit()


def admin_set_user_enabled(user_id: int, enabled: bool) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE xtream_users
            SET enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if enabled else 0, int(time.time()), user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def admin_set_user_password(user_id: int, password: str) -> bool:
    password_hash, salt = hash_password(password)

    with db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE xtream_users
            SET password_hash = ?, password_salt = ?, updated_at = ?
            WHERE id = ?
            """,
            (password_hash, salt, int(time.time()), user_id),
        )
        conn.commit()
        return cur.rowcount > 0


# =====================================================================
# HLS CACHE
# =====================================================================

def cache_dir(video_id: int) -> Path:
    return CONFIG.cache / str(video_id)


def playlist_path(video_id: int) -> Path:
    return cache_dir(video_id) / PLAYLIST_NAME


def manifest_path(video_id: int) -> Path:
    return cache_dir(video_id) / MANIFEST_NAME


def expected_manifest(video: sqlite3.Row) -> dict:
    return {
        "video_id": int(video["id"]),
        "source": str(video["path"]),
        "size_bytes": int(video["size_bytes"]),
        "mtime_ns": int(video["mtime_ns"]),
        "segment_time": CONFIG.segment_time,
        "transcode_incompatible": CONFIG.transcode_incompatible,
    }


def cache_matches(video: sqlite3.Row) -> bool:
    manifest_file = manifest_path(int(video["id"]))

    if not manifest_file.exists():
        return False

    try:
        current = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    return current == expected_manifest(video)


def write_manifest(video: sqlite3.Row) -> None:
    manifest_path(int(video["id"])).write_text(
        json.dumps(expected_manifest(video), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def playlist_complete(path: Path) -> bool:
    if not path.exists():
        return False

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    return "#EXT-X-ENDLIST" in text


def invalidate_cache(video_id: int) -> None:
    directory = cache_dir(video_id)
    if directory.exists():
        shutil.rmtree(directory)


def ffmpeg_command(video: sqlite3.Row) -> list[str]:
    source = Path(str(video["path"]))
    out_dir = cache_dir(int(video["id"]))
    segment_pattern = out_dir / "segment-%06d.ts"
    playlist = out_dir / PLAYLIST_NAME

    video_codec = (video["video_codec"] or "").lower()
    audio_codec = (video["audio_codec"] or "").lower()

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-sn",
        "-dn",
    ]

    if CONFIG.transcode_incompatible:
        if video_codec in VIDEO_COPY_CODECS:
            cmd += ["-c:v", "copy"]
        else:
            cmd += [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
            ]

        if not audio_codec:
            pass
        elif audio_codec in AUDIO_COPY_CODECS:
            cmd += ["-c:a", "copy"]
        else:
            cmd += [
                "-c:a", "aac",
                "-b:a", "128k",
                "-ac", "2",
            ]
    else:
        cmd += ["-c", "copy"]

    cmd += [
        "-f", "hls",
        "-hls_time", str(CONFIG.segment_time),
        "-hls_list_size", "0",
        "-hls_segment_type", "mpegts",
        "-hls_flags", "independent_segments+temp_file",
        "-hls_segment_filename", str(segment_pattern),
        str(playlist),
    ]

    return cmd


def watch_process(
    video_id: int,
    process: subprocess.Popen,
    log_handle,
) -> None:
    try:
        process.wait()
    finally:
        try:
            log_handle.close()
        except Exception:
            pass

        with generation_lock:
            current = generation_processes.get(video_id)
            if current is process:
                generation_processes.pop(video_id, None)


def ensure_generation(video: sqlite3.Row) -> tuple[bool, str]:
    video_id = int(video["id"])
    source = Path(str(video["path"]))

    if not source.exists():
        return False, "arquivo original não existe"

    if not cache_matches(video):
        with generation_lock:
            running = generation_processes.get(video_id)

            if running is not None and running.poll() is None:
                return True, "gerando"

            invalidate_cache(video_id)

    directory = cache_dir(video_id)
    directory.mkdir(parents=True, exist_ok=True)

    playlist = playlist_path(video_id)

    if cache_matches(video) and playlist_complete(playlist):
        return True, "cache"

    with generation_lock:
        running = generation_processes.get(video_id)

        if running is not None and running.poll() is None:
            return True, "gerando"

        write_manifest(video)

        log_file = directory / LOG_NAME
        log_handle = log_file.open("ab", buffering=0)

        process = subprocess.Popen(
            ffmpeg_command(video),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        generation_processes[video_id] = process

        threading.Thread(
            target=watch_process,
            args=(video_id, process, log_handle),
            daemon=True,
        ).start()

    return True, "iniciado"


def wait_for_playlist(video_id: int) -> tuple[bool, str]:
    playlist = playlist_path(video_id)
    deadline = time.monotonic() + CONFIG.playlist_wait

    while time.monotonic() < deadline:
        if playlist.exists() and playlist.stat().st_size > 0:
            return True, "ready"

        with generation_lock:
            process = generation_processes.get(video_id)

        if process is not None and process.poll() is not None:
            return False, f"ffmpeg terminou com código {process.returncode}"

        time.sleep(0.25)

    return False, "playlist ainda não ficou pronta"


# =====================================================================
# FORMAT HELPERS
# =====================================================================

def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "00:00:00"

    value = max(0, int(seconds))
    hours, rem = divmod(value, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def sql_timestamp_to_unix(value: str | None) -> str:
    if not value:
        return str(int(time.time()))

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return str(int(dt.timestamp()))
    except Exception:
        return str(int(time.time()))


# =====================================================================
# HTTP SERVER
# =====================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "MiniVOD/0.2"

    _head_only = False

    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args

        # Xtream usa credenciais na URL. Não grave senhas em texto puro no log.
        message = re.sub(
            r"(password=)[^&\\s]+",
            r"\\1***",
            message,
            flags=re.IGNORECASE,
        )
        message = re.sub(
            r"(/movie/[^/\\s]+/)[^/\\s]+/",
            r"\\1***/",
            message,
        )

        print(
            f"{self.client_address[0]} "
            f"- {self.log_date_time_string()} - "
            f"{message}"
        )

    def end_headers(self) -> None:
        is_admin = urlparse(self.path).path.startswith("/admin")
        if is_admin:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Range, Content-Type, Authorization",
            )
            self.send_header(
                "Access-Control-Expose-Headers",
                "Content-Length, Content-Range",
            )
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def base_url(self) -> str:
        if CONFIG.base_url:
            return CONFIG.base_url.rstrip("/")

        host = self.headers.get("Host")
        if host:
            return f"http://{host}"

        return f"http://127.0.0.1:{CONFIG.port}"

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if not self._head_only:
            self.wfile.write(body)

    def send_text(
        self,
        status: int,
        text: str,
        content_type: str,
    ) -> None:
        body = text.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if not self._head_only:
            self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self.send_json(404, {"error": "arquivo não encontrado"})
            return

        size = path.stat().st_size

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.end_headers()

        if self._head_only:
            return

        try:
            with path.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile, length=1024 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_form_body(self) -> dict[str, list[str]]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0

        if size <= 0:
            return {}

        raw = self.rfile.read(size).decode("utf-8", errors="replace")
        return parse_qs(raw, keep_blank_values=True)

    def request_params(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return unquote(parsed.path), parse_qs(
            parsed.query,
            keep_blank_values=True,
        )

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_HEAD(self) -> None:
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_POST(self) -> None:
        path, query = self.request_params()

        if path.startswith("/admin"):
            if not self.require_admin():
                return
            self.handle_admin_post(path)
            return

        if path not in {"/player_api.php", "/player_api"}:
            self.send_json(404, {"error": "rota não encontrada"})
            return

        form = self.read_form_body()
        params = dict(query)
        params.update(form)
        self.handle_player_api(params)

    def do_GET(self) -> None:
        path, query = self.request_params()

        if path.startswith("/admin"):
            if not self.require_admin():
                return
            if path == "/admin":
                self.handle_admin()
            else:
                self.send_json(404, {"error": "rota não encontrada"})
            return

        if path == "/":
            self.handle_root()
            return

        if path == "/health":
            self.handle_health()
            return

        if path == "/collections":
            self.handle_collections()
            return

        if path in {"/player_api.php", "/player_api"}:
            self.handle_player_api(query)
            return

        if path in {"/get.php", "/get"}:
            self.handle_get_php(query)
            return

        if path in {"/xmltv.php", "/xmltv"}:
            self.handle_xmltv(query)
            return

        if path == "/compat":
            self.handle_compat()
            return

        match = re.fullmatch(r"/collections/(\d+)/videos", path)
        if match:
            self.handle_collection_videos(int(match.group(1)), query)
            return

        match = re.fullmatch(r"/videos/(\d+)", path)
        if match:
            self.handle_video(int(match.group(1)))
            return

        match = re.fullmatch(r"/vod/(\d+)/index\.m3u8", path)
        if match:
            self.handle_playlist(int(match.group(1)))
            return

        match = re.fullmatch(r"/vod/(\d+)/(segment-\d{6}\.ts)", path)
        if match:
            self.handle_segment(
                int(match.group(1)),
                match.group(2),
            )
            return

        match = re.fullmatch(r"/vod/(\d+)/status", path)
        if match:
            self.handle_status(int(match.group(1)))
            return

        match = re.fullmatch(
            r"/movie/([^/]+)/([^/]+)/(\d+)\.m3u8",
            path,
        )
        if match:
            self.handle_xtream_movie_playlist(
                unquote(match.group(1)),
                unquote(match.group(2)),
                int(match.group(3)),
            )
            return

        match = re.fullmatch(
            r"/movie/([^/]+)/([^/]+)/(\d+)",
            path,
        )
        if match:
            self.handle_xtream_movie_playlist(
                unquote(match.group(1)),
                unquote(match.group(2)),
                int(match.group(3)),
            )
            return

        match = re.fullmatch(
            r"/movie/([^/]+)/([^/]+)/(\d+)/(segment-\d{6}\.ts)",
            path,
        )
        if match:
            self.handle_xtream_movie_segment(
                unquote(match.group(1)),
                unquote(match.group(2)),
                int(match.group(3)),
                match.group(4),
            )
            return

        self.send_json(
            404,
            {
                "error": "rota não encontrada",
                "routes": [
                    "/",
                    "/health",
                    "/collections",
                    "/collections/{id}/videos",
                    "/videos/{id}",
                    "/vod/{id}/index.m3u8",
                    "/vod/{id}/status",
                    "/player_api.php",
                    "/get.php",
                    "/xmltv.php",
                    "/compat",
                    "/movie/{username}/{password}/{id}.m3u8",
                ],
            },
        )

    # -----------------------------------------------------------------
    # Admin
    # -----------------------------------------------------------------

    def require_admin(self) -> bool:
        if not CONFIG.admin_user or not CONFIG.admin_password:
            self.send_json(404, {"error": "rota não encontrada"})
            return False

        authorization = self.headers.get("Authorization", "")
        authenticated = False
        if authorization.startswith("Basic "):
            try:
                raw = base64.b64decode(
                    authorization[6:], validate=True
                ).decode("utf-8")
                username, password = raw.split(":", 1)
                authenticated = (
                    hmac.compare_digest(username, CONFIG.admin_user)
                    and hmac.compare_digest(password, CONFIG.admin_password)
                )
            except (ValueError, UnicodeDecodeError, binascii.Error):
                authenticated = False

        if authenticated:
            return True

        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MiniVOD Admin"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def send_admin_html(self, status: int, content: str) -> None:
        self.send_text(status, content, "text/html; charset=utf-8")

    def admin_page(self, error: str | None = None) -> str:
        rows = admin_users()
        user_rows: list[str] = []
        for row in rows:
            user_id = int(row["id"])
            username = html.escape(str(row["username"]))
            status = "Ativo" if int(row["enabled"]) else "Desativado"
            expires = "Nunca expira"
            if row["exp_date"] is not None:
                expires = datetime.fromtimestamp(
                    int(row["exp_date"]), tz=timezone.utc
                ).strftime("%Y-%m-%d")
            action = "disable" if int(row["enabled"]) else "enable"
            action_label = "Desativar" if action == "disable" else "Habilitar"
            user_rows.append(
                f"<tr><td>{username}</td><td>{status}</td>"
                f"<td>Máx: {int(row['max_connections'])}</td>"
                f"<td>{expires}</td><td>"
                f"<form method=\"post\" action=\"/admin/users/{user_id}/{action}\">"
                f"<button>{action_label}</button></form>"
                f"<form method=\"post\" action=\"/admin/users/{user_id}/password\">"
                "<input type=\"password\" name=\"password\" minlength=\"6\" "
                "required placeholder=\"Nova senha\">"
                "<button>Alterar senha</button></form>"
                "</td></tr>"
            )

        error_html = ""
        if error:
            error_html = f"<p class=\"error\">{html.escape(error)}</p>"
        users_html = "".join(user_rows) or "<tr><td colspan=\"5\">Nenhum usuário.</td></tr>"
        return f"""<!doctype html>
<html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><title>MiniVOD Admin</title>
<style>body{{background:#111;color:#ddd;font:14px sans-serif;max-width:960px;margin:32px auto;padding:0 16px}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #444;padding:10px;text-align:left}}form{{display:inline-block;margin:2px}}input{{background:#222;border:1px solid #555;color:#eee;padding:7px}}button{{background:#333;border:1px solid #666;color:#eee;padding:7px;cursor:pointer}}button:hover{{background:#444}}.error{{color:#ff8f8f}}section{{border-top:1px solid #555;margin-top:24px;padding-top:16px}}label{{display:block;margin:10px 0 4px}}</style>
</head><body><h1>MiniVOD Admin</h1>{error_html}<section><h2>Usuários</h2>
<table><thead><tr><th>Usuário</th><th>Status</th><th>Conexões</th><th>Expiração</th><th>Ações</th></tr></thead><tbody>{users_html}</tbody></table></section>
<section><h2>Novo usuário</h2><form method=\"post\" action=\"/admin/users\"><label>Usuário</label><input name=\"username\" minlength=\"3\" maxlength=\"64\" required pattern=\"[A-Za-z0-9._-]+\"><label>Senha</label><input type=\"password\" name=\"password\" minlength=\"6\" required><label>Máximo de conexões</label><input type=\"number\" name=\"max_connections\" value=\"1\" min=\"1\" required><label>Expira em dias</label><input type=\"number\" name=\"expires_days\" min=\"1\"><p><button>Criar usuário</button></p></form></section>
</body></html>"""

    def handle_admin(self) -> None:
        self.send_admin_html(200, self.admin_page())

    def redirect_admin(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/admin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def form_value(self, form: dict[str, list[str]], name: str) -> str:
        return form.get(name, [""])[0].strip()

    def handle_admin_post(self, path: str) -> None:
        form = self.read_form_body()
        if path == "/admin/users":
            self.handle_admin_create_user(form)
            return

        match = re.fullmatch(r"/admin/users/(\d+)/(enable|disable)", path)
        if match:
            if not admin_set_user_enabled(
                int(match.group(1)), match.group(2) == "enable"
            ):
                self.send_admin_html(404, self.admin_page("Usuário não encontrado."))
                return
            self.redirect_admin()
            return

        match = re.fullmatch(r"/admin/users/(\d+)/password", path)
        if match:
            password = self.form_value(form, "password")
            if len(password) < 6:
                self.send_admin_html(400, self.admin_page("A senha deve ter ao menos 6 caracteres."))
                return
            if not admin_set_user_password(int(match.group(1)), password):
                self.send_admin_html(404, self.admin_page("Usuário não encontrado."))
                return
            self.redirect_admin()
            return

        self.send_json(404, {"error": "rota não encontrada"})

    def handle_admin_create_user(self, form: dict[str, list[str]]) -> None:
        username = self.form_value(form, "username")
        password = self.form_value(form, "password")
        max_connections = self.form_value(form, "max_connections")
        expires_days = self.form_value(form, "expires_days")

        if not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", username):
            self.send_admin_html(400, self.admin_page("Usuário inválido."))
            return
        if len(password) < 6:
            self.send_admin_html(400, self.admin_page("A senha deve ter ao menos 6 caracteres."))
            return
        try:
            max_value = int(max_connections)
            expires_value = int(expires_days) if expires_days else None
            if max_value < 1 or (expires_value is not None and expires_value < 1):
                raise ValueError
        except ValueError:
            self.send_admin_html(400, self.admin_page("Conexões e expiração devem ser números positivos."))
            return

        try:
            admin_create_user(username, password, max_value, expires_value)
        except sqlite3.IntegrityError:
            self.send_admin_html(409, self.admin_page("Este usuário já existe."))
            return
        self.redirect_admin()

    # -----------------------------------------------------------------
    # Base API
    # -----------------------------------------------------------------

    def handle_root(self) -> None:
        with db_connect(readonly=True) as conn:
            videos = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE active = 1"
            ).fetchone()[0]

            collections = conn.execute(
                """
                SELECT COUNT(*)
                FROM collections
                WHERE active = 1 AND video_count > 0
                """
            ).fetchone()[0]

        self.send_json(
            200,
            {
                "name": "MiniVOD",
                "version": "0.2",
                "status": "ok",
                "videos": videos,
                "collections": collections,
                "xtream": "/player_api.php",
                "m3u": "/get.php",
            },
        )

    def handle_health(self) -> None:
        try:
            with db_connect(readonly=True) as conn:
                active = conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE active = 1"
                ).fetchone()[0]

            self.send_json(
                200,
                {
                    "status": "ok",
                    "videos": active,
                    "db": str(CONFIG.db),
                    "cache": str(CONFIG.cache),
                },
            )
        except Exception as exc:
            self.send_json(500, {"status": "error", "error": str(exc)})

    def handle_collections(self) -> None:
        with db_connect(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT id, name, slug, video_count
                FROM collections
                WHERE active = 1
                  AND video_count > 0
                ORDER BY name COLLATE NOCASE
                """
            ).fetchall()

        self.send_json(
            200,
            [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "slug": row["slug"],
                    "video_count": row["video_count"],
                }
                for row in rows
            ],
        )

    def handle_collection_videos(
        self,
        collection_id: int,
        query: dict[str, list[str]],
    ) -> None:
        try:
            limit = max(1, min(int(query.get("limit", ["500"])[0]), 1000))
            offset = max(0, int(query.get("offset", ["0"])[0]))
        except ValueError:
            self.send_json(400, {"error": "limit/offset inválido"})
            return

        with db_connect(readonly=True) as conn:
            collection = conn.execute(
                """
                SELECT id, name, slug, video_count
                FROM collections
                WHERE id = ? AND active = 1
                """,
                (collection_id,),
            ).fetchone()

            if collection is None:
                self.send_json(404, {"error": "coleção não encontrada"})
                return

            rows = conn.execute(
                """
                SELECT
                    id,
                    title,
                    filename,
                    duration,
                    video_codec,
                    audio_codec,
                    width,
                    height,
                    size_bytes
                FROM videos
                WHERE collection_id = ?
                  AND active = 1
                ORDER BY filename COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                (collection_id, limit, offset),
            ).fetchall()

        self.send_json(
            200,
            {
                "collection": {
                    "id": collection["id"],
                    "name": collection["name"],
                    "slug": collection["slug"],
                    "video_count": collection["video_count"],
                },
                "offset": offset,
                "limit": limit,
                "videos": [
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "filename": row["filename"],
                        "duration": row["duration"],
                        "video_codec": row["video_codec"],
                        "audio_codec": row["audio_codec"],
                        "width": row["width"],
                        "height": row["height"],
                        "size_bytes": row["size_bytes"],
                        "hls": f"/vod/{row['id']}/index.m3u8",
                    }
                    for row in rows
                ],
            },
        )

    def handle_video(self, video_id: int) -> None:
        video = get_video(video_id)

        if video is None:
            self.send_json(404, {"error": "vídeo não encontrado"})
            return

        self.send_json(
            200,
            {
                "id": video["id"],
                "collection_id": video["collection_id"],
                "collection_name": video["collection_name"],
                "title": video["title"],
                "filename": video["filename"],
                "relative_path": video["relative_path"],
                "size_bytes": video["size_bytes"],
                "duration": video["duration"],
                "format_name": video["format_name"],
                "bitrate": video["bitrate"],
                "video_codec": video["video_codec"],
                "audio_codec": video["audio_codec"],
                "width": video["width"],
                "height": video["height"],
                "fps": video["fps"],
                "hls": f"/vod/{video_id}/index.m3u8",
            },
        )

    # -----------------------------------------------------------------
    # Native HLS
    # -----------------------------------------------------------------

    def prepare_playlist(self, video_id: int) -> tuple[sqlite3.Row | None, str | None]:
        video = get_video(video_id)

        if video is None:
            return None, "vídeo não encontrado"

        ok, state = ensure_generation(video)

        if not ok:
            return None, state

        ready, error = wait_for_playlist(video_id)

        if not ready:
            return None, error

        return video, None

    def handle_playlist(self, video_id: int) -> None:
        video, error = self.prepare_playlist(video_id)

        if video is None:
            self.send_json(503 if error != "vídeo não encontrado" else 404, {
                "error": error,
                "status": f"/vod/{video_id}/status",
            })
            return

        self.send_file(
            playlist_path(video_id),
            "application/vnd.apple.mpegurl",
        )

    def send_segment_for_video(self, video: sqlite3.Row, filename: str) -> None:
        video_id = int(video["id"])

        if not cache_matches(video):
            self.send_json(
                404,
                {"error": "cache inválido; solicite a playlist novamente"},
            )
            return

        segment = cache_dir(video_id) / filename
        deadline = time.monotonic() + 10

        while time.monotonic() < deadline:
            if segment.exists():
                self.send_file(segment, "video/mp2t")
                return

            with generation_lock:
                process = generation_processes.get(video_id)

            if process is None or process.poll() is not None:
                break

            time.sleep(0.2)

        self.send_json(404, {"error": "segmento ainda não disponível"})

    def handle_segment(self, video_id: int, filename: str) -> None:
        if not SEGMENT_RE.fullmatch(filename):
            self.send_json(400, {"error": "segmento inválido"})
            return

        video = get_video(video_id)

        if video is None:
            self.send_json(404, {"error": "vídeo não encontrado"})
            return

        self.send_segment_for_video(video, filename)

    def handle_status(self, video_id: int) -> None:
        video = get_video(video_id)

        if video is None:
            self.send_json(404, {"error": "vídeo não encontrado"})
            return

        playlist = playlist_path(video_id)

        with generation_lock:
            process = generation_processes.get(video_id)

        running = process is not None and process.poll() is None

        directory = cache_dir(video_id)
        segments = 0

        if directory.exists():
            segments = sum(
                1
                for item in directory.glob("segment-*.ts")
                if item.is_file()
            )

        self.send_json(
            200,
            {
                "video_id": video_id,
                "running": running,
                "playlist_exists": playlist.exists(),
                "complete": playlist_complete(playlist),
                "segments": segments,
                "cache_valid": cache_matches(video),
                "log": str(directory / LOG_NAME),
            },
        )

    # -----------------------------------------------------------------
    # Xtream
    # -----------------------------------------------------------------

    def auth_from_params(
        self,
        params: dict[str, list[str]],
    ) -> tuple[str, str, sqlite3.Row | None]:
        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]
        return username, password, authenticate(username, password)

    def xtream_auth_failure(self) -> None:
        self.send_json(
            200,
            {
                "user_info": {
                    "auth": 0,
                    "status": "Disabled",
                }
            },
        )

    def xtream_server_info(self) -> dict:
        parsed = urlparse(self.base_url())
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port

        if port is None:
            port = 443 if parsed.scheme == "https" else 80

        now = int(time.time())

        return {
            "url": host,
            "port": str(port),
            "https_port": "",
            "server_protocol": parsed.scheme or "http",
            "rtmp_port": "",
            "timezone": "America/Sao_Paulo",
            "timestamp_now": now,
            "time_now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def xtream_user_info(
        self,
        username: str,
        password: str,
        user: sqlite3.Row,
    ) -> dict:
        exp_date = user["exp_date"]

        return {
            "username": username,
            "password": password,
            "message": "",
            "auth": 1,
            "status": "Active",
            "exp_date": str(exp_date) if exp_date is not None else None,
            "is_trial": "0",
            "active_cons": "0",
            "created_at": str(user["created_at"]),
            "max_connections": str(user["max_connections"]),
            "allowed_output_formats": ["m3u8", "ts"],
        }

    def handle_player_api(
        self,
        params: dict[str, list[str]],
    ) -> None:
        username, password, user = self.auth_from_params(params)

        if user is None:
            self.xtream_auth_failure()
            return

        action = params.get("action", [""])[0]

        if not action:
            self.send_json(
                200,
                {
                    "user_info": self.xtream_user_info(
                        username,
                        password,
                        user,
                    ),
                    "server_info": self.xtream_server_info(),
                },
            )
            return

        if action == "get_vod_categories":
            self.xtream_vod_categories()
            return

        if action == "get_vod_streams":
            category_id = params.get("category_id", [None])[0]
            self.xtream_vod_streams(category_id)
            return

        if action == "get_vod_info":
            raw_id = params.get("vod_id", ["0"])[0]

            try:
                vod_id = int(raw_id)
            except ValueError:
                vod_id = 0

            self.xtream_vod_info(vod_id)
            return

        # Superfície vazia para players que sondam Live/Series/EPG.
        if action in {
            "get_live_categories",
            "get_live_streams",
            "get_series_categories",
            "get_series",
        }:
            self.send_json(200, [])
            return

        if action == "get_series_info":
            self.send_json(
                200,
                {"seasons": [], "info": {}, "episodes": {}},
            )
            return

        if action in {"get_short_epg", "get_simple_data_table"}:
            self.send_json(200, {"epg_listings": []})
            return

        self.send_json(200, [])

    def xtream_vod_categories(self) -> None:
        with db_connect(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT id, name
                FROM collections
                WHERE active = 1
                  AND video_count > 0
                ORDER BY name COLLATE NOCASE
                """
            ).fetchall()

        self.send_json(
            200,
            [
                {
                    "category_id": str(row["id"]),
                    "category_name": row["name"],
                    "parent_id": 0,
                }
                for row in rows
            ],
        )

    def xtream_vod_streams(self, category_id: str | None) -> None:
        sql = """
            SELECT
                v.id,
                v.title,
                v.collection_id,
                v.created_at
            FROM videos v
            JOIN collections c ON c.id = v.collection_id
            WHERE v.active = 1
              AND c.active = 1
        """
        args: list[object] = []

        if category_id not in (None, "", "0"):
            try:
                category_int = int(category_id)
            except ValueError:
                self.send_json(200, [])
                return

            sql += " AND v.collection_id = ?"
            args.append(category_int)

        sql += " ORDER BY v.id"

        with db_connect(readonly=True) as conn:
            rows = conn.execute(sql, args).fetchall()

        payload = []

        for num, row in enumerate(rows, start=1):
            payload.append(
                {
                    "num": num,
                    "name": row["title"],
                    "stream_type": "movie",
                    "stream_id": int(row["id"]),
                    "stream_icon": "",
                    "added": sql_timestamp_to_unix(row["created_at"]),
                    "category_id": str(row["collection_id"]),
                    "direct_source": "",
                    "rating": 0,
                    "rating_5based": 0,
                    "custom_sid": None,
                    "container_extension": "m3u8",
                }
            )

        self.send_json(200, payload)

    def xtream_vod_info(self, vod_id: int) -> None:
        video = get_video(vod_id)

        if video is None:
            self.send_json(200, {})
            return

        duration_secs = int(video["duration"] or 0)
        bitrate_kbps = int((video["bitrate"] or 0) / 1000)

        self.send_json(
            200,
            {
                "info": {
                    "imdb_id": "",
                    "movie_image": "",
                    "genre": "",
                    "plot": "",
                    "cast": "",
                    "director": "",
                    "rating": 0,
                    "releasedate": "",
                    "duration_secs": duration_secs,
                    "duration": format_duration(video["duration"]),
                    "bitrate": bitrate_kbps,
                    "kinopoisk_url": "",
                    "episode_run_time": "",
                    "youtube_trailer": "",
                    "actors": "",
                    "name": video["title"],
                    "name_o": video["title"],
                    "cover_big": "",
                    "description": "",
                    "age": "",
                    "rating_mpaa": "",
                    "rating_count_kinopoisk": 0,
                    "country": "",
                    "backdrop_path": [],
                    "audio": [
                        {
                            "codec": video["audio_codec"] or "",
                        }
                    ] if video["audio_codec"] else [],
                    "video": [
                        {
                            "codec": video["video_codec"] or "",
                            "width": video["width"],
                            "height": video["height"],
                            "fps": video["fps"],
                        }
                    ],
                },
                "movie_data": {
                    "stream_id": int(video["id"]),
                    "name": video["title"],
                    "added": sql_timestamp_to_unix(video["created_at"]),
                    "category_id": str(video["collection_id"]),
                    "container_extension": "m3u8",
                    "custom_sid": "",
                    "direct_source": "",
                },
            },
        )

    def handle_get_php(
        self,
        params: dict[str, list[str]],
    ) -> None:
        username, password, user = self.auth_from_params(params)

        if user is None:
            self.send_text(
                401,
                "Invalid username or password\n",
                "text/plain; charset=utf-8",
            )
            return

        playlist_type = params.get("type", ["m3u_plus"])[0]
        output = params.get("output", ["m3u8"])[0].lower()

        with db_connect(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT
                    v.id,
                    v.title,
                    c.name AS collection_name
                FROM videos v
                JOIN collections c ON c.id = v.collection_id
                WHERE v.active = 1
                  AND c.active = 1
                ORDER BY c.name COLLATE NOCASE, v.title COLLATE NOCASE
                """
            ).fetchall()

        encoded_user = quote(username, safe="")
        encoded_pass = quote(password, safe="")
        base = self.base_url()

        lines = ["#EXTM3U"]

        for row in rows:
            name = str(row["title"]).replace('"', "'")
            group = str(row["collection_name"]).replace('"', "'")

            if playlist_type == "m3u":
                lines.append(f"#EXTINF:-1,{name}")
            else:
                lines.append(
                    '#EXTINF:-1 '
                    f'tvg-id="vod-{row["id"]}" '
                    f'tvg-name="{name}" '
                    'tvg-logo="" '
                    f'group-title="{group}",'
                    f'{name}'
                )

            # Mesmo se output=ts, este servidor entrega VOD como HLS:
            # playlist .m3u8 + segmentos MPEG-TS .ts.
            lines.append(
                f"{base}/movie/"
                f"{encoded_user}/{encoded_pass}/"
                f"{row['id']}.m3u8"
            )

        self.send_text(
            200,
            "\n".join(lines) + "\n",
            "audio/x-mpegurl; charset=utf-8",
        )

    def handle_xmltv(self, params: dict[str, list[str]]) -> None:
        _, _, user = self.auth_from_params(params)

        if user is None:
            self.send_text(
                401,
                "Unauthorized\n",
                "text/plain; charset=utf-8",
            )
            return

        self.send_text(
            200,
            '<?xml version="1.0" encoding="UTF-8"?>\n<tv></tv>\n',
            "application/xml; charset=utf-8",
        )

    def handle_compat(self) -> None:
        self.send_json(
            200,
            {
                "xtream": True,
                "vod": True,
                "live": False,
                "series": False,
                "epg": False,
                "endpoints": {
                    "player_api": "/player_api.php",
                    "playlist": "/get.php",
                    "xmltv": "/xmltv.php",
                },
            },
        )

    def handle_xtream_movie_playlist(
        self,
        username: str,
        password: str,
        video_id: int,
    ) -> None:
        if authenticate(username, password) is None:
            self.send_json(401, {"error": "unauthorized"})
            return

        video, error = self.prepare_playlist(video_id)

        if video is None:
            self.send_json(503 if error != "vídeo não encontrado" else 404, {
                "error": error,
            })
            return

        source_playlist = playlist_path(video_id)

        try:
            text = source_playlist.read_text(
                encoding="utf-8",
                errors="strict",
            )
        except OSError as exc:
            self.send_json(500, {"error": str(exc)})
            return

        encoded_user = quote(username, safe="")
        encoded_pass = quote(password, safe="")

        rewritten: list[str] = []

        for line in text.splitlines():
            stripped = line.strip()

            if SEGMENT_RE.fullmatch(stripped):
                rewritten.append(
                    f"/movie/{encoded_user}/{encoded_pass}/"
                    f"{video_id}/{stripped}"
                )
            else:
                rewritten.append(line)

        self.send_text(
            200,
            "\n".join(rewritten) + "\n",
            "application/vnd.apple.mpegurl",
        )

    def handle_xtream_movie_segment(
        self,
        username: str,
        password: str,
        video_id: int,
        filename: str,
    ) -> None:
        if authenticate(username, password) is None:
            self.send_json(401, {"error": "unauthorized"})
            return

        if not SEGMENT_RE.fullmatch(filename):
            self.send_json(400, {"error": "segmento inválido"})
            return

        video = get_video(video_id)

        if video is None:
            self.send_json(404, {"error": "vídeo não encontrado"})
            return

        self.send_segment_for_video(video, filename)


# =====================================================================
# CLI
# =====================================================================

def main() -> None:
    global CONFIG

    parser = argparse.ArgumentParser(
        description=(
            "MiniVOD HLS/MPEG-TS com API Xtream-compatible "
            "para biblioteca VOD local."
        )
    )

    parser.add_argument(
        "--db",
        default="~/downloads/vod.db",
        help="caminho do vod.db",
    )

    parser.add_argument(
        "--cache",
        default="~/vod-cache",
        help="diretório do cache HLS",
    )

    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="interface de escuta",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8079,
        help="porta HTTP",
    )

    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "URL pública/base, ex.: http://192.168.15.7:8079 "
            "ou https://vod.exemplo.com"
        ),
    )

    parser.add_argument(
        "--admin-user",
        default=os.environ.get("MINIVOD_ADMIN_USER"),
        help="usuário do painel administrativo (opcional)",
    )

    parser.add_argument(
        "--admin-password",
        default=os.environ.get("MINIVOD_ADMIN_PASSWORD"),
        help="senha do painel administrativo (opcional)",
    )

    parser.add_argument(
        "--segment-time",
        type=int,
        default=6,
        help="duração alvo dos segmentos .ts",
    )

    parser.add_argument(
        "--playlist-wait",
        type=int,
        default=30,
        help="segundos para esperar a playlist inicial",
    )

    parser.add_argument(
        "--copy-only",
        action="store_true",
        help=(
            "nunca transcodifica; apenas remuxa. "
            "Pode falhar em codecs incompatíveis."
        ),
    )

    # Gestão simples de usuários.
    parser.add_argument(
        "--create-user",
        metavar="USERNAME",
        help="cria ou redefine um usuário Xtream e sai",
    )

    parser.add_argument(
        "--max-connections",
        type=int,
        default=1,
        help="metadado max_connections do usuário criado",
    )

    parser.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="expiração do usuário em N dias; omitido = nunca",
    )

    parser.add_argument(
        "--disable-user",
        metavar="USERNAME",
        help="desabilita um usuário Xtream e sai",
    )

    parser.add_argument(
        "--enable-user",
        metavar="USERNAME",
        help="habilita um usuário Xtream e sai",
    )

    parser.add_argument(
        "--list-users",
        action="store_true",
        help="lista usuários Xtream e sai",
    )

    args = parser.parse_args()

    db = Path(args.db).expanduser().resolve()
    cache = Path(args.cache).expanduser().resolve()

    if not db.exists():
        raise SystemExit(f"ERRO: banco não existe: {db}")

    # Operações administrativas não precisam iniciar o servidor.
    if args.create_user:
        create_or_update_user(
            db,
            args.create_user,
            args.max_connections,
            args.expires_days,
        )
        return

    if args.disable_user:
        set_user_enabled(db, args.disable_user, False)
        return

    if args.enable_user:
        set_user_enabled(db, args.enable_user, True)
        return

    if args.list_users:
        list_users(db)
        return

    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ERRO: ffmpeg não encontrado. "
            "Instale com: sudo apt install ffmpeg"
        )

    cache.mkdir(parents=True, exist_ok=True)

    CONFIG = Config(
        db=db,
        cache=cache,
        host=args.host,
        port=args.port,
        segment_time=max(2, args.segment_time),
        playlist_wait=max(1, args.playlist_wait),
        transcode_incompatible=not args.copy_only,
        base_url=args.base_url,
        admin_user=args.admin_user or None,
        admin_password=args.admin_password or None,
    )

    # Cria schema de usuários antes de entrar em modo leitura.
    with raw_db_connect(db) as conn:
        init_xtream_schema(conn)

    server = ThreadingHTTPServer(
        (CONFIG.host, CONFIG.port),
        Handler,
    )

    print("=" * 72)
    print("MINI VOD HLS + XTREAM")
    print("=" * 72)
    print(f"Banco:        {CONFIG.db}")
    print(f"Cache:        {CONFIG.cache}")
    print(f"HTTP:         http://{CONFIG.host}:{CONFIG.port}")
    print(f"Base URL:     {CONFIG.base_url or '(Host da requisição)'}")
    print(f"Segmentos:    {CONFIG.segment_time}s")
    print(
        "Compat:       "
        + (
            "copy H.264/AAC; transcode codecs incompatíveis"
            if CONFIG.transcode_incompatible
            else "copy-only"
        )
    )
    print()
    print("API local:")
    print("  GET  /")
    print("  GET  /health")
    print("  GET  /collections")
    print("  GET  /collections/{id}/videos")
    print("  GET  /videos/{id}")
    print("  GET  /vod/{id}/index.m3u8")
    print("  GET  /vod/{id}/status")
    print()
    print("Xtream:")
    print("  GET/POST /player_api.php")
    print("  GET      /get.php")
    print("  GET      /movie/{user}/{pass}/{id}.m3u8")
    print()
    print("Ctrl+C para encerrar.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

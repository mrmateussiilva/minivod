from __future__ import annotations

import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import urllib.request
import urllib.response

import os
from pathlib import Path
import tempfile

_init_temp = tempfile.TemporaryDirectory()
_init_root = Path(_init_temp.name)
_init_db = _init_root / "vod.db"
_init_cache = _init_root / "cache"
_init_cache.mkdir()
os.environ["MINIVOD_DB"] = str(_init_db)
os.environ["MINIVOD_CACHE"] = str(_init_cache)

import uvicorn

from app.config import Settings
from app.main import app
from app.services.runtime import configure
from app import hls_server as legacy


class AdminFastAPITests(unittest.TestCase):
    admin_user = "admin"
    admin_pass = "adminpass123"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tempdir.name)
        cls.db = cls.root / "vod.db"
        cls.cache = cls.root / "cache"
        cls.cache.mkdir()

        cls.col_dir = cls.root / "Filmes"
        cls.col_dir.mkdir()
        cls.cover_file = cls.col_dir / "cover.jpg"
        cls.cover_file.write_bytes(b"test-jpeg-content")

        with closing(sqlite3.connect(cls.db)) as conn:
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
                    probe_ok INTEGER NOT NULL DEFAULT 1,
                    probe_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

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
                """
            )
            conn.execute(
                "INSERT INTO collections (id, name, slug, path, cover_path, video_count, active) "
                "VALUES (1, 'Filmes', 'filmes', ?, ?, 1, 1)",
                (str(cls.col_dir), str(cls.cover_file)),
            )
            conn.execute(
                """
                INSERT INTO videos (
                    id, collection_id, title, filename, path, relative_path,
                    size_bytes, mtime_ns, duration, format_name, bitrate,
                    video_codec, audio_codec, width, height, fps, active, probe_ok
                ) VALUES (1, 1, 'Matrix (1999)', 'matrix.mp4', ?, 'Filmes/matrix.mp4', 1048576, 1, 8160, 'mov,mp4,m4a,3gp,3g2,mj2', 5000000, 'h264', 'aac', 1920, 1080, 23.98, 1, 1)
                """,
                (str(cls.col_dir / "matrix.mp4"),),
            )
            pass_hash, salt = legacy.hash_password("clientpass")
            conn.execute(
                """
                INSERT INTO xtream_users (
                    id, username, password_hash, password_salt, enabled, max_connections, created_at, updated_at
                ) VALUES (1, 'client01', ?, ?, 1, 2, 1000, 1000)
                """,
                (pass_hash, salt),
            )
            conn.commit()

        cls.settings = Settings(
            db=cls.db,
            cache=cls.cache,
            base_url="http://127.0.0.1:8189",
            admin_user=cls.admin_user,
            admin_password=cls.admin_pass,
            segment_time=6,
            playlist_wait=1,
            max_ffmpeg_jobs=2,
            transcode_incompatible=True,
        )
        configure(cls.settings)

        cls.port = 8189
        cls.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=cls.port,
                log_level="warning",
            )
        )
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()

        token = base64.b64encode(f"{cls.admin_user}:{cls.admin_pass}".encode()).decode()
        cls.auth_headers = {"Authorization": f"Basic {token}"}
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        # Wait for server to start
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with urlopen(f"{cls.base_url}/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=3)
        cls.tempdir.cleanup()

    def do_request(
        self,
        path: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> tuple[int, dict, str]:
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_303(self, req, fp, code, msg, headers):
                infourl = urllib.response.addinfourl(fp, headers, req.get_full_url())
                infourl.code = code
                return infourl
            http_error_301 = http_error_303
            http_error_302 = http_error_303
            http_error_307 = http_error_303
            http_error_308 = http_error_303

        url = f"{self.base_url}{path}"
        req_headers = dict(self.auth_headers)
        if headers:
            req_headers.update(headers)

        encoded_data = None
        if data is not None:
            encoded_data = urlencode(data).encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = Request(url, data=encoded_data, headers=req_headers, method=method)

        opener = urllib.request.build_opener(NoRedirectHandler) if not follow_redirects else urllib.request.build_opener()

        try:
            with opener.open(req, timeout=5) as resp:
                status = getattr(resp, "status", getattr(resp, "code", 200))
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                body = resp.read().decode("utf-8", errors="replace")
                return status, resp_headers, body
        except HTTPError as e:
            status = e.code
            resp_headers = {k.lower(): v for k, v in e.headers.items()}
            body = e.read().decode("utf-8", errors="replace")
            return status, resp_headers, body

    def test_auth_required_and_rejection(self) -> None:
        # Without auth
        status, headers, _ = self.do_request("/admin", headers={"Authorization": ""})
        self.assertEqual(status, 401)
        self.assertIn("www-authenticate", headers)

        # Wrong credentials
        bad_token = base64.b64encode(b"wrong:creds").decode()
        status, _, _ = self.do_request("/admin", headers={"Authorization": f"Basic {bad_token}"})
        self.assertEqual(status, 401)

    def test_dashboard_page(self) -> None:
        status, _, body = self.do_request("/admin")
        self.assertEqual(status, 200)
        self.assertIn("Dashboard", body)
        self.assertIn("Vídeos Ativos", body)
        self.assertIn("Coleções", body)
        self.assertIn("MiniVOD", body)

    def test_collections_and_detail_pages(self) -> None:
        status, _, body = self.do_request("/admin/collections")
        self.assertEqual(status, 200)
        self.assertIn("Filmes", body)

        status_det, _, body_det = self.do_request("/admin/collections/1")
        self.assertEqual(status_det, 200)
        self.assertIn("Matrix (1999)", body_det)

        status_nf, _, _ = self.do_request("/admin/collections/999")
        self.assertEqual(status_nf, 404)

    def test_videos_page_filtering_and_detail(self) -> None:
        status, _, body = self.do_request("/admin/videos?q=Matrix")
        self.assertEqual(status, 200)
        self.assertIn("Matrix (1999)", body)

        status_det, _, body_det = self.do_request("/admin/videos/1")
        self.assertEqual(status_det, 200)
        self.assertIn("Player de Teste HLS", body_det)
        self.assertIn("Hls.js", body_det)
        self.assertIn("1920x1080", body_det)

        status_nf, _, _ = self.do_request("/admin/videos/999")
        self.assertEqual(status_nf, 404)

    def test_covers_picker_and_preview(self) -> None:
        status, _, body = self.do_request("/admin/collections/1/covers")
        self.assertEqual(status, 200)
        self.assertIn("cover.jpg", body)

        status_prev, headers_prev, _ = self.do_request("/admin/collections/1/cover-preview?path=cover.jpg")
        self.assertEqual(status_prev, 200)
        self.assertIn("image/jpeg", headers_prev.get("content-type", ""))

        status_trav, _, _ = self.do_request("/admin/collections/1/cover-preview?path=../../etc/passwd")
        self.assertEqual(status_trav, 404)

    def test_users_page_and_mutations(self) -> None:
        status, _, body = self.do_request("/admin/users")
        self.assertEqual(status, 200)
        self.assertIn("client01", body)

        # Create user via POST
        status_create, headers_create, _ = self.do_request(
            "/admin/users",
            method="POST",
            data={
                "username": "novo_cliente",
                "password": "senhasegura123",
                "max_connections": "2",
            },
            follow_redirects=False,
        )
        self.assertEqual(status_create, 303)
        self.assertIn("/admin/users", headers_create.get("location", ""))

        # Check newly created user in list
        status_list, _, body_list = self.do_request("/admin/users")
        self.assertEqual(status_list, 200)
        self.assertIn("novo_cliente", body_list)

        # Toggle user (disable)
        status_dis, headers_dis, _ = self.do_request(
            "/admin/users/1/disable",
            method="POST",
            follow_redirects=False,
        )
        self.assertEqual(status_dis, 303)

        # Change password
        status_pass, headers_pass, _ = self.do_request(
            "/admin/users/1/password",
            method="POST",
            data={"password": "nova_senha_forte"},
            follow_redirects=False,
        )
        self.assertEqual(status_pass, 303)

    def test_cover_mutations(self) -> None:
        # Set cover via POST
        status_set, headers_set, _ = self.do_request(
            "/admin/collections/1/cover",
            method="POST",
            data={"cover": "cover.jpg"},
            follow_redirects=False,
        )
        self.assertEqual(status_set, 303)

        # Auto cover via POST
        status_auto, headers_auto, _ = self.do_request(
            "/admin/collections/1/cover-auto",
            method="POST",
            follow_redirects=False,
        )
        self.assertEqual(status_auto, 303)

        # Remove cover via POST
        status_rem, headers_rem, _ = self.do_request(
            "/admin/collections/1/cover-remove",
            method="POST",
            follow_redirects=False,
        )
        self.assertEqual(status_rem, 303)


if __name__ == "__main__":
    unittest.main()

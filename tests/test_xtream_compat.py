from __future__ import annotations

import importlib.util
import base64
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "app" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"não foi possível carregar {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hls = load_module("minivod_hls_server", "hls_server.py")
scanner = load_module("minivod_scan_vod", "scan_vod.py")
cover_support = load_module("minivod_cover_support", "cover_support.py")


class XtreamCompatibilityTests(unittest.TestCase):
    username = "client"
    password = "secret"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        root = Path(cls.tempdir.name)
        cls.db = root / "vod.db"
        cache = root / "cache"
        cache.mkdir()
        cls.bella_dir = root / "Bella Thorne OnlyFans"
        cls.bella_dir.mkdir()
        cls.cover_file = cls.bella_dir / "cover.jpg"
        cls.cover_file.write_bytes(b"test-jpeg")

        with sqlite3.connect(cls.db) as conn:
            scanner.init_db(conn)
            hls.init_xtream_schema(conn)
            conn.execute(
                "INSERT INTO collections (id, name, slug, path, cover_path, video_count, active) "
                "VALUES (1, 'Bella Thorne OnlyFans', 'bella', ?, ?, 2, 1)",
                (str(cls.bella_dir), str(cls.cover_file)),
            )
            conn.execute(
                "INSERT INTO collections (id, name, slug, video_count, active) "
                "VALUES (2, '[ROOT]', 'root', 1, 1)"
            )
            for video_id, collection_id, title in [
                (1, 1, 'bella "quoted"\\line\nnext'),
                (2, 1, "bella 002"),
                (3, 2, "root video"),
            ]:
                conn.execute(
                    """
                    INSERT INTO videos (
                        id, collection_id, title, filename, path, relative_path,
                        size_bytes, mtime_ns, duration, active, probe_ok
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, 123, 1, 1)
                    """,
                    (
                        video_id,
                        collection_id,
                        title,
                        f"{video_id}.mp4",
                        str(root / f"{video_id}.mp4"),
                        f"{video_id}.mp4",
                    ),
                )
            password_hash, salt = hls.hash_password(cls.password)
            conn.execute(
                """
                INSERT INTO xtream_users (
                    username, password_hash, password_salt, enabled,
                    max_connections, exp_date, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 1, NULL, 1, 1)
                """,
                (cls.username, password_hash, salt),
            )
            conn.commit()

        hls.CONFIG = hls.Config(
            db=cls.db,
            cache=cache,
            host="127.0.0.1",
            port=0,
            segment_time=6,
            playlist_wait=1,
            transcode_incompatible=False,
            base_url="https://vod.example.test",
            admin_user=None,
            admin_password=None,
        )
        cls.server = hls.ThreadingHTTPServer(("127.0.0.1", 0), hls.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.tempdir.cleanup()

    def request(self, path: str, **params: str) -> tuple[int, str]:
        query = urlencode(params)
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        try:
            with urlopen(url) as response:
                return response.status, response.read().decode()
        except HTTPError as exc:
            return exc.code, exc.read().decode()

    def api(self, action: str, **params: str):
        status, body = self.request(
            "/player_api.php",
            username=self.username,
            password=self.password,
            action=action,
            **params,
        )
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_vod_categories_and_filtering(self) -> None:
        categories = self.api("get_vod_categories")
        self.assertEqual(categories[0]["category_name"], "Bella Thorne OnlyFans")
        self.assertEqual(categories[0]["cover"], "https://vod.example.test/covers/1")
        self.assertIn({"category_id": "2", "category_name": "Outros", "parent_id": 0}, categories)

        streams = self.api("get_vod_streams", category_id="1")
        self.assertEqual([stream["stream_id"] for stream in streams], [1, 2])
        self.assertTrue(all(stream["category_id"] == "1" for stream in streams))
        self.assertEqual(streams[0]["stream_icon"], "https://vod.example.test/covers/1")
        self.assertEqual(
            [stream["stream_id"] for stream in self.api("get_vod_streams")],
            [1, 2, 3],
        )

    def test_series_categories_listing_and_info(self) -> None:
        self.assertEqual(
            self.api("get_series_categories"),
            [{"category_id": "1", "category_name": "Coleções", "parent_id": 0}],
        )
        series = self.api("get_series", category_id="1")
        self.assertEqual([item["series_id"] for item in series], [1, 2])
        self.assertEqual(series[1]["name"], "Outros")
        self.assertEqual(series[0]["cover"], "https://vod.example.test/covers/1")
        self.assertEqual(self.api("get_series"), series)

        info = self.api("get_series_info", series_id="1")
        self.assertEqual(info["seasons"][0]["episode_count"], 2)
        self.assertEqual(info["info"]["name"], "Bella Thorne OnlyFans")
        self.assertEqual(info["info"]["cover"], "https://vod.example.test/covers/1")
        self.assertEqual([episode["id"] for episode in info["episodes"]["1"]], ["1", "2"])
        self.assertEqual(info["episodes"]["1"][0]["info"]["duration"], "00:02:03")

    def test_m3u_plus_escapes_and_groups_collections(self) -> None:
        status, playlist = self.request(
            "/get.php",
            username=self.username,
            password=self.password,
            type="m3u_plus",
            output="hls",
        )
        self.assertEqual(status, 200)
        self.assertTrue(playlist.startswith("#EXTM3U\n"))
        self.assertIn('group-title="Bella Thorne OnlyFans"', playlist)
        self.assertIn('tvg-logo="https://vod.example.test/covers/1"', playlist)
        self.assertIn('group-title="Outros"', playlist)
        self.assertIn('tvg-name="bella \\"quoted\\"\\\\line next"', playlist)
        self.assertIn("/movie/client/secret/1.m3u8", playlist)

    def test_aliases_and_invalid_authentication(self) -> None:
        status, player = self.request(
            "/player_api",
            username=self.username,
            password=self.password,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(player)["user_info"]["allowed_output_formats"], ["m3u8"])

        status, playlist = self.request(
            "/get",
            username=self.username,
            password=self.password,
            type="m3u",
            output="m3u8",
        )
        self.assertEqual(status, 200)
        self.assertIn("#EXTINF:-1,bella", playlist)

        status, xmltv = self.request(
            "/xmltv",
            username=self.username,
            password=self.password,
        )
        self.assertEqual(status, 200)
        self.assertEqual(xmltv, '<?xml version="1.0" encoding="UTF-8"?>\n<tv></tv>\n')

        status, body = self.request(
            "/player_api.php",
            username=self.username,
            password="wrong",
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["user_info"]["auth"], 0)

    def test_admin_pages_auth_and_escaping(self) -> None:
        status, _ = self.request("/admin")
        self.assertEqual(status, 404)
        hls.CONFIG.admin_user = "admin"
        hls.CONFIG.admin_password = "admin-pass"
        token = base64.b64encode(b"admin:admin-pass").decode()
        headers = {"Authorization": f"Basic {token}"}
        with urlopen(Request(f"{self.base_url}/admin", headers=headers)) as response:
            page = response.read().decode()
            self.assertIn("Vídeos ativos", page)
            self.assertIn("Coleções", page)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        for path, expected in [
            ("/admin/collections", "Bella Thorne OnlyFans"),
            ("/admin/collections/1?per_page=25", "Detalhes"),
            ("/admin/videos?q=quoted", "bella &quot;quoted&quot;"),
            ("/admin/videos/1", "Abrir playlist HLS"),
            ("/admin/users", "Novo usuário"),
            ("/admin/collections/1/covers", "Selecionar"),
        ]:
            with urlopen(Request(f"{self.base_url}{path}", headers=headers)) as response:
                self.assertIn(expected, response.read().decode())

    def test_password_sanitization(self) -> None:
        message = hls.sanitize_log_message(
            'GET /series/user/secret/1.m3u8?password=query-secret HTTP/1.1'
        )
        self.assertNotIn("secret", message)
        self.assertNotIn("query-secret", message)
        self.assertIn("/series/user/***/1.m3u8", message)
        self.assertIn("password=***", message)

    def test_cover_selection_priority_and_stability(self) -> None:
        photos = self.bella_dir / "Fotos"
        photos.mkdir()
        (photos / "thumbnail.jpg").write_bytes(b"thumbnail")
        fallback = photos / "foto.jpg"
        fallback.write_bytes(b"fallback")
        self.assertEqual(cover_support.find_collection_cover(self.bella_dir), self.cover_file)

        self.cover_file.unlink()
        self.assertEqual(cover_support.find_collection_cover(self.bella_dir), fallback)
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("UPDATE collections SET cover_path = ? WHERE id = 1", (str(fallback),))
            self.assertFalse(scanner.ensure_collection_cover(conn, 1, self.bella_dir))
            fallback.unlink()
            replacement = self.bella_dir / "poster.png"
            replacement.write_bytes(b"png")
            self.assertTrue(scanner.ensure_collection_cover(conn, 1, self.bella_dir))
            self.assertEqual(
                conn.execute("SELECT cover_path FROM collections WHERE id = 1").fetchone()[0],
                str(replacement),
            )

    def test_cover_route_and_path_traversal_protection(self) -> None:
        with sqlite3.connect(self.db) as conn:
            current = conn.execute("SELECT cover_path FROM collections WHERE id = 1").fetchone()[0]
            if not current:
                conn.execute("UPDATE collections SET cover_path = ? WHERE id = 1", (str(self.cover_file),))
                conn.commit()
        with urlopen(f"{self.base_url}/covers/1") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertTrue(response.read())
        status, _ = self.request("/covers/999")
        self.assertEqual(status, 404)
        self.assertFalse(
            cover_support.is_within(self.bella_dir / "../outside.jpg", self.bella_dir)
        )
        self.assertFalse(hls.admin_set_collection_cover(1, "../outside.jpg"))

    def test_series_stream_alias_authenticates_like_movie(self) -> None:
        # The temporary catalog intentionally has no media files. Both routes
        # must nonetheless reach the same HLS preparation path after auth.
        movie_status, movie_body = self.request("/movie/client/secret/1.m3u8")
        series_status, series_body = self.request("/series/client/secret/1.m3u8")
        self.assertEqual(movie_status, 503)
        self.assertEqual(series_status, 503)
        self.assertEqual(movie_body, series_body)


if __name__ == "__main__":
    unittest.main()

"""End-to-end test against a fake Spotify API. Run: python3 test_spotify_export.py"""

import csv
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

SCRIPT = Path(__file__).with_name("spotify_export.py")


def track(n, **extra):
    return {"type": "track", "name": f"Song {n}", "uri": f"spotify:track:{n}",
            "artists": [{"name": "Artist A"}, {"name": "Artist B"}],
            "album": {"name": "Album", "artists": [{"name": "Artist A"}],
                      "release_date": "2020-01-01"},
            "disc_number": 1, "track_number": n, "duration_ms": 1000 * n,
            "external_ids": {"isrc": f"ISRC{n:04d}"}, **extra}


class FakeSpotify(http.server.BaseHTTPRequestHandler):
    rate_limited_once = False

    def log_message(self, *a):
        pass

    def send(self, code, body, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Authorization") != "Bearer good":
            return self.send(401, {"error": "bad token"})
        if u.path == "/me":
            return self.send(200, {"id": "me", "display_name": "Tester"})
        if u.path == "/me/tracks":
            if not FakeSpotify.rate_limited_once:
                FakeSpotify.rate_limited_once = True
                return self.send(429, {}, {"Retry-After": "0"})
            if q.get("offset") == "50":
                return self.send(200, {"items": [{"added_at": "t2", "track": track(2)}],
                                       "next": None})
            return self.send(200, {"items": [{"added_at": "t1", "track": track(1)}],
                                   "next": f"{base}/me/tracks?offset=50&limit=50"})
        if u.path == "/me/albums":
            return self.send(200, {"next": None, "items": [{"added_at": "t", "album": {
                "name": "Album", "artists": [{"name": "Artist A"}], "uri": "spotify:album:1",
                "external_ids": {"upc": "123"}}}]})
        if u.path == "/me/following":
            assert q["type"] == "artist"
            return self.send(200, {"artists": {"next": None, "items": [
                {"name": "Artist A", "genres": ["rock"], "uri": "spotify:artist:1"}]}})
        if u.path == "/me/playlists":
            def pl(i, name):
                return {"id": i, "name": name, "uri": f"spotify:playlist:{i}",
                        "owner": {"id": "me", "display_name": "Tester"}}
            return self.send(200, {"next": None, "items": [
                pl("p1", "Road/Trip"), pl("p2", "Someone else's"), pl("p3", "road_trip")]})
        if u.path == "/playlists/p1/items":
            return self.send(200, {"next": None, "items": [
                {"added_at": "a", "item": track(3)},
                {"added_at": "b", "is_local": True,
                 "item": track(4, uri="spotify:local:x", external_ids={})},
                {"added_at": "c", "item": {"type": "episode", "name": "Ep", "uri": "spotify:episode:1",
                                           "show": {"name": "Pod"}, "duration_ms": 5}},
                {"added_at": "d", "item": None},
            ]})
        if u.path == "/playlists/p2/items":
            return self.send(403, {"error": "forbidden"})
        if u.path == "/playlists/p3/items":  # legacy key
            return self.send(200, {"next": None, "items": [{"added_at": "a", "track": track(5)}]})
        self.send(404, {"error": u.path})


class ExportTest(unittest.TestCase):
    def test_export(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSpotify)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".spotify_token.json").write_text(json.dumps({
                "client_id": "cid", "access_token": "good", "refresh_token": "r",
                "expires_at": time.time() + 3600}))
            env = {**os.environ, "SPOTIFY_API_BASE": f"http://127.0.0.1:{server.server_port}"}
            env.pop("SPOTIFY_CLIENT_ID", None)
            r = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp, env=env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

            lib = json.loads((tmp / "export/library.json").read_text())
            self.assertEqual([t["title"] for t in lib["liked_songs"]], ["Song 1", "Song 2"])
            self.assertEqual(lib["liked_songs"][0]["isrc"], "ISRC0001")
            self.assertEqual(lib["saved_albums"][0]["upc"], "123")
            self.assertEqual(lib["followed_artists"][0]["name"], "Artist A")

            p1, p2, p3 = lib["playlists"]
            self.assertEqual([t["type"] for t in p1["tracks"]], ["track", "track", "episode"])
            self.assertTrue(p1["tracks"][1]["is_local"])
            self.assertFalse(p2["tracks_available"])
            self.assertEqual(p3["tracks"][0]["title"], "Song 5")

            files = sorted(p.name for p in (tmp / "export/playlists").iterdir())
            self.assertEqual(files, ["Road_Trip.csv", "road_trip (p3).csv"])
            rows = list(csv.DictReader(open(tmp / "export/liked_songs.csv", encoding="utf-8")))
            self.assertEqual(rows[1]["position"], "2")
            self.assertEqual(rows[0]["artists"], "Artist A; Artist B")
        server.shutdown()


if __name__ == "__main__":
    unittest.main()

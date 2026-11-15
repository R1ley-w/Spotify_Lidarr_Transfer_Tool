"""End-to-end test against a fake Lidarr. Run: python3 test_to_lidarr.py"""

import csv
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

SCRIPT = Path(__file__).with_name("to_lidarr.py")


def album(mbid, title, artist, **extra):
    return {"id": 0, "foreignAlbumId": mbid, "title": title, "monitored": False,
            "artist": {"artistName": artist, "foreignArtistId": f"artist-{artist}"}, **extra}


CATALOGUE = [
    album("rg-new", "New Album", "Artist A"),
    album("rg-unmon", "Old Album", "Artist A"),
    album("rg-mon", "Have It", "Artist B"),
    album("rg-deluxe", "Big Record", "Beyoncé"),
    album("rg-wrong", "New Album", "Impostor"),
]
IN_LIBRARY = [
    {"id": 7, "foreignAlbumId": "rg-unmon", "monitored": False},
    {"id": 8, "foreignAlbumId": "rg-mon", "monitored": True},
]


class FakeLidarr(http.server.BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def send(self, code, body):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def handle_any(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        if self.headers.get("X-Api-Key") != "key":
            return self.send(401, {})
        route = (self.command, u.path)
        if self.command != "GET":
            FakeLidarr.calls.append((self.command, u.path, body))
        if route == ("GET", "/api/v1/album"):
            return self.send(200, IN_LIBRARY)
        if route == ("GET", "/api/v1/album/lookup"):
            words = q["term"].lower().split()
            return self.send(200, [a for a in CATALOGUE
                                   if any(w in a["title"].lower() for w in words)])
        if route == ("GET", "/api/v1/rootfolder"):
            return self.send(200, [{"path": "/music", "defaultQualityProfileId": 2,
                                    "defaultMetadataProfileId": 0}])
        if route == ("GET", "/api/v1/qualityprofile"):
            return self.send(200, [{"id": 1, "name": "Any"}, {"id": 2, "name": "Lossless"}])
        if route == ("GET", "/api/v1/metadataprofile"):
            return self.send(200, [{"id": 5, "name": "Standard"}])
        if route == ("POST", "/api/v1/album"):
            return self.send(201, {"id": 99})
        if route in (("PUT", "/api/v1/album/monitor"), ("POST", "/api/v1/command")):
            return self.send(202, {})
        self.send(404, {})

    do_GET = do_POST = do_PUT = handle_any


def trk(album_name, artist, title="x", **extra):
    return {"type": "track", "title": title, "artists": [artist, "Guest"],
            "album": album_name, "album_artists": [artist], **extra}


LIBRARY = {
    "liked_songs": [trk("New Album", "Artist A", "One"), trk("New Album", "Artist A", "Two"),
                    trk("Local Thing", "Me", is_local=True)],
    "saved_albums": [{"name": "Have It", "artists": ["Artist B"], "total_tracks": 10},
                     {"name": "Nowhere", "artists": ["Ghost"], "total_tracks": 3}],
    "playlists": [{"name": "Mix", "tracks": [
        trk("Old Album", "Artist A"), trk("new album", "ARTIST A", "ONE"),
        trk("Big Record (Deluxe)", "Beyonce"),
        {"type": "episode", "title": "Ep", "artists": ["Pod"], "album": "Pod"}]}],
}


class LidarrTest(unittest.TestCase):
    def run_script(self, tmp, port, *args):
        env = {**os.environ, "LIDARR_API_KEY": "key", "LIDARR_URL": f"http://127.0.0.1:{port}"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--library",
                            str(tmp / "library.json"), *args],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(tmp / "lidarr_plan.csv", encoding="utf-8") as f:
            return {(r["artist"], r["album"]): r for r in csv.DictReader(f)}

    def test_plan_then_apply(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLidarr)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "library.json").write_text(json.dumps(LIBRARY))

            plan = self.run_script(tmp, server.server_port)
            self.assertEqual(FakeLidarr.calls, [], "dry run must not write to Lidarr")
            self.assertEqual(len(plan), 5)  # local file and episode ignored, case-dupes merged
            new = plan[("Artist A", "New Album")]
            self.assertEqual((new["status"], new["tracks_wanted"], new["musicbrainz_album_id"]),
                             ("add", "2", "rg-new"))  # "One" twice counts once
            self.assertEqual(new["sources"], "Liked Songs; Mix")
            self.assertEqual(plan[("Artist A", "Old Album")]["status"], "monitor")
            self.assertEqual(plan[("Artist B", "Have It")]["status"], "already monitored")
            self.assertEqual(plan[("Ghost", "Nowhere")]["status"], "not found")
            deluxe = plan[("Beyonce", "Big Record (Deluxe)")]
            self.assertEqual((deluxe["status"], deluxe["match"]), ("add", "loose"))

            few = self.run_script(tmp, server.server_port, "--min-tracks", "2")
            self.assertEqual(sorted(few), [("Artist A", "New Album"), ("Artist B", "Have It"),
                                           ("Ghost", "Nowhere")])  # 2 songs, or saved albums
            self.assertEqual(FakeLidarr.calls, [])

            plan = self.run_script(tmp, server.server_port, "--apply", "--search")
            self.assertEqual(plan[("Artist A", "New Album")]["status"], "added")
            self.assertEqual(plan[("Artist A", "Old Album")]["status"], "monitored")
            posts = [b for m, p, b in FakeLidarr.calls if (m, p) == ("POST", "/api/v1/album")]
            self.assertEqual(sorted(b["foreignAlbumId"] for b in posts), ["rg-deluxe", "rg-new"])
            body = next(b for b in posts if b["foreignAlbumId"] == "rg-new")
            self.assertTrue(body["addOptions"]["searchForNewAlbum"])
            self.assertEqual(body["artist"]["foreignArtistId"], "artist-Artist A")
            self.assertEqual((body["artist"]["qualityProfileId"],
                              body["artist"]["metadataProfileId"],
                              body["artist"]["rootFolderPath"]), (2, 5, "/music"))
            self.assertIn(("PUT", "/api/v1/album/monitor", {"albumIds": [7], "monitored": True}),
                          FakeLidarr.calls)
            self.assertIn(("POST", "/api/v1/command", {"name": "AlbumSearch", "albumIds": [7]}),
                          FakeLidarr.calls)


if __name__ == "__main__":
    unittest.main()

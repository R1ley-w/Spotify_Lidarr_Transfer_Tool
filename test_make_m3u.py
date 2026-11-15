"""Matching and M3U writing tests (no audio files needed). Run: python3 test_make_m3u.py"""

import csv
import tempfile
import unittest
from pathlib import Path

import make_m3u


def f(path, title, artist, album="", isrc="", duration=200.0):
    return {"path": Path("/music") / path, "title": title, "artist": artist,
            "album": album, "isrc": isrc, "duration": duration}


def t(title, artists, album="", isrc=None, ms=200_000, uri=None, kind="track"):
    return {"type": kind, "title": title, "artists": artists, "album": album,
            "album_artists": [], "isrc": isrc, "duration_ms": ms,
            "spotify_uri": uri or f"spotify:track:{title}"}


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.index = make_m3u.Index([
            f("a/halo.flac", "Halo", "Beyonce", "I Am", isrc="US-SM1-08-00001"),
            f("a/song_live.mp3", "Song", "Band", "Live Album", duration=300),
            f("a/song.mp3", "Song", "Band", "Studio Album", duration=201),
            f("a/duet.mp3", "Duet", "Alice feat. Bob"),
            f("a/other.mp3", "Song", "Different Band"),
            f("a/heroes.mp3", "Heroes", "David Bowie", duration=370),
        ])

    def name(self, track):
        hit, method = self.index.match(track)
        return (hit["path"].name if hit else None), method

    def test_isrc_wins_regardless_of_tags(self):
        self.assertEqual(self.name(t("Totally Different", ["X"], isrc="USSM10800001")),
                         ("halo.flac", "isrc"))

    def test_accents_and_case(self):
        self.assertEqual(self.name(t("HALO", ["Beyoncé"])), ("halo.flac", "exact"))

    def test_duration_and_album_pick_the_right_version(self):
        self.assertEqual(self.name(t("Song", ["Band"], "Studio Album")), ("song.mp3", "exact"))
        self.assertEqual(self.name(t("Song", ["Band"], "Live Album", ms=300_000)),
                         ("song_live.mp3", "exact"))

    def test_featured_artist_either_side(self):
        self.assertEqual(self.name(t("Duet", ["Alice", "Bob"]))[0], "duet.mp3")
        self.assertEqual(self.name(t("Duet", ["Bob"]))[0], "duet.mp3")

    def test_loose_title(self):
        self.assertEqual(self.name(t("Heroes - 2017 Remaster", ["David Bowie"], ms=371_000)),
                         ("heroes.mp3", "loose"))

    def test_rejects_wrong_artist_and_wrong_length(self):
        self.assertEqual(self.name(t("Halo", ["Someone Else"])), (None, None))
        self.assertEqual(self.name(t("Heroes", ["David Bowie"], ms=200_000)), (None, None))


class WriteTest(unittest.TestCase):
    def test_run(self):
        files = [f("Artist/Album/01 One.flac", "One", "Artist")]
        library = {
            "liked_songs": [t("One", ["Artist"])],
            "playlists": [
                {"name": "Mix: 1", "spotify_id": "p1", "tracks_available": True, "tracks": [
                    t("One", ["Artist"]), t("Missing", ["Nobody"]),
                    t("Ep", ["Pod"], kind="episode")]},
                {"name": "Hidden", "spotify_id": "p2", "tracks_available": False, "tracks": []},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            matched, total, _ = make_m3u.run(library, files, Path("/music"), out, prefix="/srv/music")
            self.assertEqual((matched, total), (2, 3))
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["Liked Songs.m3u", "Mix_ 1.m3u", "unmatched.csv"])
            lines = (out / "Mix_ 1.m3u").read_text().splitlines()
            self.assertEqual(lines, ["#EXTM3U", "#PLAYLIST:Mix_ 1", "#EXTINF:200,Artist - One",
                                     "/srv/music/Artist/Album/01 One.flac"])
            with open(out / "unmatched.csv", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([(r["playlist"], r["title"], r["position"]) for r in rows],
                             [("Mix_ 1", "Missing", "2")])

    def test_relative_paths(self):
        self.assertEqual(
            make_m3u.m3u_path(Path("/music/A/b.mp3"), Path("/music"), Path("/music/Playlists"), None),
            "../A/b.mp3")


if __name__ == "__main__":
    unittest.main()

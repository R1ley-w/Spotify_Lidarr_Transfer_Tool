"""Tests for album_filter. Run: python3 test_album_filter.py"""

import unittest

import album_filter as af


def trk(title, album, artist="Artist", **extra):
    return {"type": "track", "title": title, "artists": [artist, "Guest"],
            "album": album, "album_artists": [artist], **extra}


LIBRARY = {
    "liked_songs": [
        trk("One", "Two Songs"), trk("Two", "Two Songs"),
        trk("Solo", "One Song"),
        trk("Same", "Repeats"), trk("SAME", "repeats"),        # one song, liked twice
        trk("A", "Shared Title", "First"), trk("B", "Shared Title", "Second"),
        trk("Mine", "Local", is_local=True), trk("Mine 2", "Local", is_local=True),
        {"type": "episode", "title": "Ep 1", "artists": ["Pod"], "album": "Pod"},
        {"type": "episode", "title": "Ep 2", "artists": ["Pod"], "album": "Pod"},
    ],
    "playlists": [
        {"name": "Gym", "tracks": [trk("Other", "One Song"), trk("X", "Gym Only"),
                                   trk("Y", "Gym Only")]},
        {"name": "Chill", "tracks": [trk("P", "Chill Only"), trk("Q", "Chill Only")]},
    ],
    "saved_albums": [{"name": "Kept", "artists": ["Artist"]}],
}


def names(albums):
    return sorted(a["album"] for a in albums)


class FilterTest(unittest.TestCase):
    def liked(self, n):
        return af.wanted_albums(LIBRARY, n, liked=True, playlists=False, albums=False)

    def test_liked_songs_need_two_different_songs(self):
        self.assertEqual(names(self.liked(2)), ["Two Songs"])

    def test_min_one_keeps_every_album(self):
        self.assertEqual(names(self.liked(1)),
                         ["One Song", "Repeats", "Shared Title", "Shared Title", "Two Songs"])

    def test_songs_are_counted_across_lists(self):
        got = af.wanted_albums(LIBRARY, 2, liked=True, playlists=True, albums=False,
                               playlist_names=["gym"])
        self.assertEqual(names(got), ["Gym Only", "One Song", "Two Songs"])
        one = next(a for a in got if a["album"] == "One Song")
        self.assertEqual((one["tracks"], one["titles"], one["sources"]),
                         (2, ["Other", "Solo"], {"Liked Songs", "Gym"}))

    def test_saved_albums_always_qualify(self):
        got = af.wanted_albums(LIBRARY, 5, liked=True, playlists=False, albums=True)
        self.assertEqual(names(got), ["Kept"])

    def test_unknown_playlist(self):
        with self.assertRaises(SystemExit):
            af.count_albums(LIBRARY, playlist_names=["Nope"])


if __name__ == "__main__":
    unittest.main()

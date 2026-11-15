#!/usr/bin/env python3
"""Decide which albums are worth requesting from a Spotify export.

An album qualifies when at least --min-tracks different songs from it appear
in the scanned lists (Liked Songs by default). Albums with fewer are skipped,
so one stray liked song does not pull in a whole album.

Run on its own to preview the decision (writes album_requests.csv), or use it
through to_lidarr.py --min-tracks N. Standard library only.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

from make_m3u import norm

LIKED = "Liked Songs"
SAVED = "Saved Albums"


def select_playlists(library, names):
    """The playlists called `names` (case-insensitive); all of them if names is None."""
    playlists = library.get("playlists") or []
    if names is None:
        return playlists
    by_name = {}
    for pl in playlists:
        by_name.setdefault((pl.get("name") or "").casefold(), []).append(pl)
    chosen = []
    for name in names:
        if name.casefold() not in by_name:
            available = ", ".join(sorted(p.get("name") or "" for p in playlists))
            raise SystemExit(f"No playlist called '{name}'. Available: {available}")
        chosen += by_name[name.casefold()]
    return chosen


def count_albums(library, liked=True, playlists=True, albums=True, playlist_names=None):
    """Every album in the chosen sources, with the distinct songs seen from it.

    Returns a list of {"artist", "album", "tracks", "titles", "sources", "saved"}.
    A song that appears several times (liked and in a playlist, say) counts once.
    """
    found = {}

    def entry(artists, album):
        key = (norm(artists[0]), norm(album))
        return found.setdefault(key, {"artist": artists[0], "album": album, "saved": False,
                                      "sources": set(), "_songs": {}})

    def add_tracks(tracks, source):
        for t in tracks or []:
            artists = t.get("album_artists") or t.get("artists")
            if t.get("type") != "track" or t.get("is_local") or not artists or not t.get("album"):
                continue
            e = entry(artists, t["album"])
            e["sources"].add(source)
            # Title, not URI: the same song can have several Spotify IDs.
            e["_songs"].setdefault(norm(t.get("title")), t.get("title"))

    if liked:
        add_tracks(library.get("liked_songs"), LIKED)
    if playlists:
        for pl in select_playlists(library, playlist_names):
            add_tracks(pl.get("tracks"), pl.get("name") or "playlist")
    if albums:
        for a in library.get("saved_albums") or []:
            if a.get("artists") and a.get("name"):
                e = entry(a["artists"], a["name"])
                e["sources"].add(SAVED)
                e["saved"] = True

    result = []
    for e in found.values():
        titles = sorted(e.pop("_songs").values(), key=str.casefold)
        result.append({**e, "tracks": len(titles), "titles": titles})
    return sorted(result, key=lambda e: (norm(e["artist"]), norm(e["album"])))


def qualifies(album, min_tracks):
    """Saved albums always qualify: saving an album is asking for the whole album."""
    return album["saved"] or album["tracks"] >= min_tracks


def wanted_albums(library, min_tracks=1, **sources):
    """The albums to request. `sources` are the keyword arguments of count_albums."""
    return [a for a in count_albums(library, **sources) if qualifies(a, min_tracks)]


def write_report(path, albums, min_tracks):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["decision", "artist", "album", "songs", "sources", "song_titles"])
        for a in albums:
            w.writerow(["request" if qualifies(a, min_tracks) else "skip", a["artist"],
                        a["album"], a["tracks"], "; ".join(sorted(a["sources"])),
                        "; ".join(a["titles"])])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--library", type=Path, default=Path("export/library.json"))
    ap.add_argument("--min-tracks", type=int, default=2,
                    help="songs needed from an album before it is requested (default: 2)")
    ap.add_argument("--playlist", action="append", metavar="NAME",
                    help="also scan this playlist (repeatable)")
    ap.add_argument("--no-liked", action="store_true", help="do not scan Liked Songs")
    args = ap.parse_args()

    library = json.loads(args.library.read_text(encoding="utf-8"))
    albums = count_albums(library, liked=not args.no_liked, playlists=bool(args.playlist),
                          albums=False, playlist_names=args.playlist)
    keep = [a for a in albums if qualifies(a, args.min_tracks)]
    report = args.library.parent / "album_requests.csv"
    write_report(report, albums, args.min_tracks)

    for a in keep:
        print(f"{a['tracks']:3d}  {a['artist']} - {a['album']}")
    print(f"\n{len(keep)} albums have {args.min_tracks}+ songs and would be requested; "
          f"{len(albums) - len(keep)} would be skipped.\nFull list: {report}", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["mutagen"]
# ///
"""Match a Spotify export (library.json) against a local music folder and
write one .m3u playlist per Spotify playlist, for Navidrome and similar servers.

Needs the `mutagen` package to read audio tags. See README.md.
"""

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

from spotify_export import CSV_COLUMNS, safe_name

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
              ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv", ".mpc", ".dsf"}
MAX_DURATION_DIFF = 10  # seconds, for matches that don't rest on an ISRC


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Normalising names so "Beyoncé" == "beyonce" and "Song (Remastered)" == "Song"
# --------------------------------------------------------------------------

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ")
    return re.sub(r"[^\w]+", "", s, flags=re.UNICODE).replace("_", "")


def loose_title(s):
    """Drop decorations: "(feat. X)", "[Live]", " - Remastered 2011"."""
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s or "")
    s = re.sub(r"\s+-\s+.*$", "", s)
    s = re.sub(r"\s+(feat|ft|featuring)\.?\s+.*$", "", s, flags=re.I)
    return norm(s)


def artist_keys(s):
    """All plausible keys for an artist tag: whole string, plus each credited artist."""
    keys = {norm(s)}
    for part in re.split(r"\s*(?:;|,|/|&|\band\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b|\bwith\b)\s*",
                         s or "", flags=re.I):
        keys.add(norm(part))
    keys.discard("")
    return keys


def norm_isrc(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# --------------------------------------------------------------------------
# Scanning the music folder
# --------------------------------------------------------------------------

def _first(tags, *names):
    for n in names:
        v = tags.get(n)
        if v:
            v = v[0] if isinstance(v, (list, tuple)) else v
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            return str(v).strip()
    return ""


def read_file(path):
    """Return {"path", "title", "artist", "album", "isrc", "duration"} or None."""
    import mutagen
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return None
    if f is None:
        return None
    tags = f.tags or {}
    isrc = _first(tags, "isrc")
    if not isrc and path.suffix.lower() in (".m4a", ".mp4"):
        try:  # MP4 keeps ISRC in a freeform atom that the "easy" view hides
            isrc = _first(mutagen.File(path).tags or {}, "----:com.apple.iTunes:ISRC")
        except Exception:
            pass
    return {
        "path": path,
        "title": _first(tags, "title") or path.stem,
        "artist": _first(tags, "artist") or _first(tags, "albumartist"),
        "album": _first(tags, "album"),
        "isrc": norm_isrc(isrc),
        "duration": getattr(f.info, "length", None),
    }


def scan(music_dir):
    files = []
    paths = [Path(root) / n for root, _, names in os.walk(music_dir, followlinks=True)
             for n in names
             if Path(n).suffix.lower() in AUDIO_EXTS and not n.startswith("._")]
    for i, p in enumerate(sorted(paths), 1):
        if i % 500 == 0:
            log(f"  read {i}/{len(paths)} files...")
        entry = read_file(p)
        if entry:
            files.append(entry)
    return files


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

class Index:
    def __init__(self, files):
        self.by_isrc = defaultdict(list)
        self.by_title = defaultdict(list)
        self.by_loose = defaultdict(list)
        for f in files:
            f["_artists"] = artist_keys(f["artist"])
            f["_album"] = norm(f["album"])
            if f["isrc"]:
                self.by_isrc[norm_isrc(f["isrc"])].append(f)
            self.by_title[norm(f["title"])].append(f)
            self.by_loose[loose_title(f["title"])].append(f)

    def match(self, t):
        """Return (file, method) for an exported track, or (None, None)."""
        secs = (t.get("duration_ms") or 0) / 1000 or None
        album = norm(t.get("album"))

        def best(cands, need_duration):
            def diff(f):
                if secs is None or not f["duration"]:
                    return None
                return abs(f["duration"] - secs)
            if need_duration:
                cands = [f for f in cands if diff(f) is None or diff(f) <= MAX_DURATION_DIFF]
            if not cands:
                return None
            # Prefer the same album, then the closest duration.
            return min(cands, key=lambda f: (f["_album"] != album,
                                             diff(f) if diff(f) is not None else 1e9))

        isrc = norm_isrc(t.get("isrc"))
        if isrc and isrc in self.by_isrc:
            return best(self.by_isrc[isrc], False), "isrc"

        wanted = set()
        for a in t.get("artists") or []:
            wanted |= artist_keys(a)
        if not wanted or not t.get("title"):
            return None, None

        def by_artist(cands):
            return [f for f in cands if f["_artists"] & wanted]

        hit = best(by_artist(self.by_title.get(norm(t["title"]), [])), True)
        if hit:
            return hit, "exact"
        hit = best(by_artist(self.by_loose.get(loose_title(t["title"]), [])), True)
        if hit:
            return hit, "loose"
        return None, None


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def m3u_path(file_path, music_dir, out_dir, prefix):
    if prefix is not None:
        rel = Path(file_path).relative_to(music_dir).as_posix()
        return f"{prefix.rstrip('/')}/{rel}"
    return Path(os.path.relpath(file_path, out_dir)).as_posix()


def build_playlists(library, liked_name):
    lists = [(liked_name, library.get("liked_songs") or [])] if liked_name else []
    used = {liked_name.lower()} if liked_name else set()
    for pl in library.get("playlists") or []:
        if not pl.get("tracks_available", True):
            continue
        name = safe_name(pl["name"])
        if name.lower() in used:
            name = f"{name} ({pl['spotify_id']})"
        used.add(name.lower())
        lists.append((name, pl["tracks"]))
    return lists


def run(library, files, music_dir, out_dir, prefix=None, liked_name="Liked Songs"):
    """Write the .m3u files and unmatched.csv. Returns (matched, total, loose_matches)."""
    index = Index(files)
    out_dir.mkdir(parents=True, exist_ok=True)
    matched = total = 0
    unmatched, loose = [], []
    cache = {}

    for name, tracks in build_playlists(library, liked_name):
        lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
        found = 0
        for pos, t in enumerate(tracks, 1):
            if t.get("type") == "episode":
                continue
            total += 1
            key = t.get("spotify_uri") or (t.get("title"), tuple(t.get("artists") or []))
            if key not in cache:
                cache[key] = index.match(t)
            f, method = cache[key]
            if not f:
                unmatched.append({**t, "playlist": name, "position": pos})
                continue
            found += 1
            if method == "loose":
                loose.append((name, t, f))
            secs = round(f["duration"] or (t.get("duration_ms") or 0) / 1000)
            lines.append(f"#EXTINF:{secs},{', '.join(t.get('artists') or [])} - {t.get('title')}")
            lines.append(m3u_path(f["path"], music_dir, out_dir, prefix))
        matched += found
        (out_dir / f"{name}.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"  {name}: {found}/{sum(t.get('type') != 'episode' for t in tracks)} matched")

    with open(out_dir / "unmatched.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["playlist"] + CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for t in unmatched:
            w.writerow({**t, "artists": "; ".join(t.get("artists") or []),
                        "album_artists": "; ".join(t.get("album_artists") or [])})
    return matched, total, loose


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("music_dir", type=Path, help="root of your music folder")
    ap.add_argument("--library", type=Path, default=Path("export/library.json"),
                    help="export file from spotify_export.py (default: export/library.json)")
    ap.add_argument("--out", type=Path, default=Path("export/m3u"),
                    help="where to write the .m3u files (default: export/m3u)")
    ap.add_argument("--prefix",
                    help="write absolute paths starting with this instead of relative ones, "
                         "e.g. /music if that is where the server mounts your music folder")
    ap.add_argument("--no-liked", action="store_true", help="skip the Liked Songs playlist")
    args = ap.parse_args()

    try:
        import mutagen  # noqa: F401
    except ImportError:
        raise SystemExit("mutagen is missing. Run with `uv run make_m3u.py ...` "
                         "or install it: python3 -m pip install mutagen")
    if not args.music_dir.is_dir():
        raise SystemExit(f"Not a folder: {args.music_dir}")
    library = json.loads(args.library.read_text(encoding="utf-8"))

    music_dir = args.music_dir.resolve()
    log(f"Scanning {music_dir}...")
    files = scan(music_dir)
    log(f"  {len(files)} audio files\nMatching...")

    matched, total, loose = run(library, files, music_dir, args.out.resolve(),
                                args.prefix, None if args.no_liked else "Liked Songs")
    if loose:
        log(f"\n{len(loose)} matches ignored title decorations like '(Remastered)'; worth a glance:")
        for name, t, f in loose[:20]:
            log(f"  [{name}] {t['title']}  ->  {f['path'].name}")
        if len(loose) > 20:
            log(f"  ... and {len(loose) - 20} more")
    log(f"\nMatched {matched}/{total} playlist entries. "
        f"Playlists in {args.out}/, misses in {args.out}/unmatched.csv")


if __name__ == "__main__":
    main()

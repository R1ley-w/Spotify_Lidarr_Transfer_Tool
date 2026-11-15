#!/usr/bin/env python3
"""Send the albums in a Spotify export (library.json) to Lidarr as wanted albums.

By default this only looks albums up and writes a plan (lidarr_plan.csv).
Nothing is changed in Lidarr unless you pass --apply. Standard library only.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from album_filter import wanted_albums
from make_m3u import artist_keys, loose_title, norm

PLAN_COLUMNS = ["status", "artist", "album", "tracks_wanted", "sources",
                "lidarr_artist", "lidarr_album", "match", "musicbrainz_album_id"]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class Lidarr:
    def __init__(self, url, api_key):
        self.base = url.rstrip("/") + "/api/v1"
        self.api_key = api_key

    def call(self, method, path, params=None, body=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Api-Key": self.api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise LidarrError(e.code, e.read().decode(errors="replace")[:500])
        except urllib.error.URLError as e:
            raise SystemExit(f"Cannot reach Lidarr at {self.base}: {e.reason}")


class LidarrError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body}")
        self.code = code


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------

def choose(want, results):
    """Pick the lookup result that is the same album by the same artist."""
    artists = artist_keys(want["artist"])
    loose = None
    for r in results or []:
        name = (r.get("artist") or {}).get("artistName") or ""
        if not r.get("foreignAlbumId") or not (artist_keys(name) & artists):
            continue
        if norm(r.get("title")) == norm(want["album"]):
            return r, "exact"
        if loose is None and loose_title(r.get("title")) == loose_title(want["album"]):
            loose = r
    return (loose, "loose") if loose else (None, None)


def lookup(lidarr, want, cache):
    """Return {"album_id", "artist_id", "album", "artist", "match"}, None, or "error"."""
    term = f"{want['artist']} {want['album']}"
    if term in cache:
        return cache[term]
    for attempt in range(3):
        try:
            results = lidarr.call("GET", "/album/lookup", {"term": term})
            break
        except LidarrError as e:
            if e.code in (401, 403):
                raise SystemExit("Lidarr rejected the API key.")
            time.sleep(2 * (attempt + 1))
    else:
        return "error"  # not cached, so the next run retries it
    r, how = choose(want, results)
    cache[term] = r and {
        "album_id": r["foreignAlbumId"],
        "artist_id": (r.get("artist") or {}).get("foreignArtistId"),
        "album": r.get("title"),
        "artist": (r.get("artist") or {}).get("artistName"),
        "match": how,
    }
    time.sleep(0.2)  # be gentle with Lidarr's metadata server
    return cache[term]


def build_plan(lidarr, wanted, cache, save_cache=lambda: None):
    existing = {a["foreignAlbumId"]: a for a in lidarr.call("GET", "/album") or []}
    plan, seen = [], set()
    for i, want in enumerate(wanted, 1):
        if i % 25 == 0:
            log(f"  looked up {i}/{len(wanted)}...")
            save_cache()
        hit = lookup(lidarr, want, cache)
        row = {"artist": want["artist"], "album": want["album"],
               "tracks_wanted": want["tracks"], "sources": "; ".join(sorted(want["sources"]))}
        if hit == "error":
            row["status"] = "lookup failed"
        elif not hit:
            row["status"] = "not found"
        else:
            row.update(lidarr_artist=hit["artist"], lidarr_album=hit["album"],
                       match=hit["match"], musicbrainz_album_id=hit["album_id"],
                       _artist_id=hit["artist_id"])
            have = existing.get(hit["album_id"])
            if hit["album_id"] in seen:
                row["status"] = "duplicate"
            elif have and have.get("monitored"):
                row["status"] = "already monitored"
            elif have:
                row["status"] = "monitor"
                row["_lidarr_id"] = have["id"]
            else:
                row["status"] = "add"
            seen.add(hit["album_id"])
        plan.append(row)
    save_cache()
    return plan


def write_plan(path, plan):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(plan)


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------

def pick_settings(lidarr, root_folder, quality, metadata):
    roots = lidarr.call("GET", "/rootfolder") or []
    if not roots:
        raise SystemExit("Lidarr has no root folder. Add one in Settings > Media Management.")
    if root_folder:
        root = next((r for r in roots if r["path"].rstrip("/") == root_folder.rstrip("/")), None)
        if not root:
            raise SystemExit(f"Root folder not in Lidarr: {root_folder}. "
                             f"Available: {', '.join(r['path'] for r in roots)}")
    else:
        root = roots[0]

    def profile(kind, name, default_id):
        profiles = lidarr.call("GET", f"/{kind}") or []
        if name:
            p = next((p for p in profiles if p["name"].lower() == name.lower()), None)
            if not p:
                raise SystemExit(f"No such {kind} '{name}'. "
                                 f"Available: {', '.join(p['name'] for p in profiles)}")
            return p
        return next((p for p in profiles if p["id"] == default_id), None) or profiles[0]

    q = profile("qualityprofile", quality, root.get("defaultQualityProfileId"))
    m = profile("metadataprofile", metadata, root.get("defaultMetadataProfileId"))
    log(f"Using root folder {root['path']}, quality '{q['name']}', metadata '{m['name']}'")
    return root["path"], q["id"], m["id"]


def apply_plan(lidarr, plan, root_path, quality_id, metadata_id, search):
    added = monitored = failed = 0
    for row in plan:
        if row["status"] != "add":
            continue
        body = {
            "foreignAlbumId": row["musicbrainz_album_id"],
            "monitored": True,
            "addOptions": {"searchForNewAlbum": search},
            "artist": {
                "foreignArtistId": row["_artist_id"],
                "qualityProfileId": quality_id,
                "metadataProfileId": metadata_id,
                "rootFolderPath": root_path,
                "monitored": True,
                # Only applies if the artist is new to Lidarr: monitor just this
                # album, not the whole discography.
                "addOptions": {"monitor": "future", "monitored": True,
                               "albumsToMonitor": [row["musicbrainz_album_id"]],
                               "searchForMissingAlbums": False},
            },
        }
        try:
            lidarr.call("POST", "/album", body=body)
            row["status"] = "added"
            added += 1
            log(f"  added: {row['lidarr_artist']} - {row['lidarr_album']}")
        except LidarrError as e:
            row["status"] = f"failed: {e}"
            failed += 1
            log(f"  FAILED: {row['artist']} - {row['album']}: {e}")

    ids = [row["_lidarr_id"] for row in plan if row["status"] == "monitor"]
    if ids:
        lidarr.call("PUT", "/album/monitor", body={"albumIds": ids, "monitored": True})
        if search:
            lidarr.call("POST", "/command", body={"name": "AlbumSearch", "albumIds": ids})
        for row in plan:
            if row["status"] == "monitor":
                row["status"] = "monitored"
        monitored = len(ids)
    return added, monitored, failed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("LIDARR_URL", "http://localhost:8686"),
                    help="Lidarr address (or set LIDARR_URL; default http://localhost:8686)")
    ap.add_argument("--library", type=Path, default=Path("export/library.json"))
    ap.add_argument("--apply", action="store_true",
                    help="actually add and monitor the albums in Lidarr")
    ap.add_argument("--search", action="store_true",
                    help="with --apply: also start a download search for each album")
    ap.add_argument("--limit", type=int, help="only handle the first N albums (for a trial run)")
    ap.add_argument("--min-tracks", type=int, default=1, metavar="N",
                    help="only request an album when at least N different songs from it "
                         "appear in the scanned lists (saved albums always count)")
    ap.add_argument("--playlist", action="append", metavar="NAME",
                    help="scan only this playlist instead of all of them (repeatable)")
    ap.add_argument("--no-liked", action="store_true", help="ignore Liked Songs")
    ap.add_argument("--no-playlists", action="store_true", help="ignore playlists")
    ap.add_argument("--no-albums", action="store_true", help="ignore saved albums")
    ap.add_argument("--root-folder", help="Lidarr root folder path (default: the first one)")
    ap.add_argument("--quality-profile", help="quality profile name (default: root folder's)")
    ap.add_argument("--metadata-profile", help="metadata profile name (default: root folder's)")
    args = ap.parse_args()

    api_key = os.environ.get("LIDARR_API_KEY")
    if not api_key:
        raise SystemExit("Set LIDARR_API_KEY (Lidarr > Settings > General > API Key).")
    lidarr = Lidarr(args.url, api_key)

    library = json.loads(args.library.read_text(encoding="utf-8"))
    wanted = wanted_albums(library, args.min_tracks, liked=not args.no_liked,
                           playlists=not args.no_playlists, albums=not args.no_albums,
                           playlist_names=args.playlist)
    if args.limit:
        wanted = wanted[:args.limit]
    log(f"{len(wanted)} albums to request"
        + (f" (those with {args.min_tracks}+ songs)" if args.min_tracks > 1 else "")
        + ". Looking them up in Lidarr...")

    out_dir = args.library.parent
    cache_file = out_dir / "lidarr_lookup_cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    plan = build_plan(lidarr, wanted, cache,
                      lambda: cache_file.write_text(json.dumps(cache, indent=1)))

    if args.apply and any(r["status"] in ("add", "monitor") for r in plan):
        settings = pick_settings(lidarr, args.root_folder, args.quality_profile,
                                 args.metadata_profile)
        apply_plan(lidarr, plan, *settings, args.search)

    plan_file = out_dir / "lidarr_plan.csv"
    write_plan(plan_file, plan)
    counts = {}
    for r in plan:
        status = r["status"].split(":")[0]
        counts[status] = counts.get(status, 0) + 1
    log("\n" + "\n".join(f"  {n:5d}  {s}" for s, n in sorted(counts.items())))
    log(f"\nPlan written to {plan_file}")
    if not args.apply:
        log("Nothing was changed in Lidarr. Review the plan, then re-run with --apply.")


if __name__ == "__main__":
    main()

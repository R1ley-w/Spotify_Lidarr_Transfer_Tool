#!/usr/bin/env python3
"""Export a Spotify library (playlists, liked songs, saved albums, followed
artists) to JSON + CSV so it can be re-created on a self-hosted music server.

Standard library only. See README.md for setup.
"""

import argparse
import base64
import csv
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

API_BASE = os.environ.get("SPOTIFY_API_BASE", "https://api.spotify.com/v1")
ACCOUNTS_BASE = os.environ.get("SPOTIFY_ACCOUNTS_BASE", "https://accounts.spotify.com")
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read user-follow-read"
TOKEN_FILE = Path(".spotify_token.json")
PAGE_SIZE = 50

CSV_COLUMNS = [
    "position", "type", "title", "artists", "album", "album_artists",
    "release_date", "disc_number", "track_number", "duration_ms", "isrc",
    "is_local", "added_at", "spotify_uri",
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Auth (Authorization Code with PKCE; no client secret needed)
# --------------------------------------------------------------------------

def _post_token(data):
    req = urllib.request.Request(
        f"{ACCOUNTS_BASE}/api/token",
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Token request failed ({e.code}): {e.read().decode(errors='replace')}")


def _save_token(client_id, tok, previous=None):
    cache = {
        "client_id": client_id,
        "access_token": tok["access_token"],
        # Spotify may or may not rotate the refresh token; keep the old one if not.
        "refresh_token": tok.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": time.time() + tok.get("expires_in", 3600),
    }
    TOKEN_FILE.write_text(json.dumps(cache, indent=2))
    TOKEN_FILE.chmod(0o600)
    return cache


def _login(client_id, port, open_browser):
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = f"{ACCOUNTS_BASE}/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>Spotify export: you can close this tab.</h3>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 300
    log(f"Open this URL to authorise the export:\n\n  {url}\n")
    if open_browser:
        webbrowser.open(url)
    while not result:
        server.handle_request()
        if not result:
            raise SystemExit("Timed out waiting for the Spotify login.")
    server.server_close()

    if result.get("state") != state:
        raise SystemExit("Login failed: state mismatch.")
    if "code" not in result:
        raise SystemExit(f"Login failed: {result.get('error', 'no code returned')}")

    tok = _post_token({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    return _save_token(client_id, tok)


class Session:
    def __init__(self, client_id, port, open_browser):
        cache = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else None
        client_id = client_id or (cache or {}).get("client_id")
        if not client_id:
            raise SystemExit(
                "No client ID. Pass --client-id or set SPOTIFY_CLIENT_ID (see README.md).")
        if cache and cache.get("client_id") != client_id:
            cache = None
        self.client_id, self.port, self.open_browser = client_id, port, open_browser
        self.cache = cache or _login(client_id, port, open_browser)

    def _refresh(self):
        if self.cache.get("refresh_token"):
            try:
                tok = _post_token({
                    "grant_type": "refresh_token",
                    "refresh_token": self.cache["refresh_token"],
                    "client_id": self.client_id,
                })
                self.cache = _save_token(self.client_id, tok, self.cache)
                return
            except SystemExit as e:
                log(f"Refresh failed, logging in again. ({e})")
        self.cache = _login(self.client_id, self.port, self.open_browser)

    def get(self, url, params=None):
        """GET a Spotify API URL (absolute, or a path under API_BASE)."""
        if not url.startswith("http"):
            url = API_BASE + url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        refreshed = False
        for attempt in range(8):
            if self.cache["expires_at"] - time.time() < 60:
                self._refresh()
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.cache['access_token']}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code == 401 and not refreshed:
                    refreshed = True
                    self._refresh()
                elif e.code == 429:
                    wait = int(e.headers.get("Retry-After") or 5)
                    if wait > 600:
                        raise SystemExit(
                            f"Spotify rate limit: asked to wait {wait}s. Try again later.\n{body}")
                    log(f"  rate limited, waiting {wait}s...")
                    time.sleep(wait + 1)
                elif e.code >= 500:
                    time.sleep(2 ** attempt)
                else:
                    raise ApiError(e.code, body, url)
            except urllib.error.URLError as e:
                log(f"  network error ({e.reason}), retrying...")
                time.sleep(2 ** attempt)
        raise SystemExit(f"Giving up on {url} after repeated failures.")

    def pages(self, url, params=None, key=None):
        """Yield every item of a paginated endpoint. `key` unwraps e.g. {"artists": {...}}."""
        page = self.get(url, {"limit": PAGE_SIZE, **(params or {})})
        while True:
            if key:
                page = page[key]
            yield from page.get("items") or []
            if not page.get("next"):
                return
            page = self.get(page["next"])


class ApiError(Exception):
    def __init__(self, code, body, url):
        super().__init__(f"HTTP {code} for {url}: {body}")
        self.code = code


# --------------------------------------------------------------------------
# Normalising Spotify objects into the export format
# --------------------------------------------------------------------------

def _names(artists):
    return [a.get("name") for a in artists or [] if a.get("name")]


def norm_track(obj, added_at=None, is_local=False):
    """Flatten a Spotify track (or podcast episode) object."""
    if not obj:
        return None
    kind = obj.get("type", "track")
    if kind == "episode":
        show = obj.get("show") or {}
        return {
            "type": "episode",
            "title": obj.get("name"),
            "artists": [show["name"]] if show.get("name") else [],
            "album": show.get("name"),
            "album_artists": [],
            "release_date": obj.get("release_date"),
            "disc_number": None,
            "track_number": None,
            "duration_ms": obj.get("duration_ms"),
            "isrc": None,
            "is_local": False,
            "added_at": added_at,
            "spotify_uri": obj.get("uri"),
        }
    album = obj.get("album") or {}
    return {
        "type": "track",
        "title": obj.get("name"),
        "artists": _names(obj.get("artists")),
        "album": album.get("name"),
        "album_artists": _names(album.get("artists")),
        "release_date": album.get("release_date"),
        "disc_number": obj.get("disc_number"),
        "track_number": obj.get("track_number"),
        "duration_ms": obj.get("duration_ms"),
        "isrc": (obj.get("external_ids") or {}).get("isrc"),
        "is_local": bool(is_local or obj.get("is_local")),
        "added_at": added_at,
        "spotify_uri": obj.get("uri"),
    }


def norm_album(entry):
    album = entry.get("album") or {}
    return {
        "name": album.get("name"),
        "artists": _names(album.get("artists")),
        "album_type": album.get("album_type"),
        "release_date": album.get("release_date"),
        "total_tracks": album.get("total_tracks"),
        "upc": (album.get("external_ids") or {}).get("upc"),
        "added_at": entry.get("added_at"),
        "spotify_uri": album.get("uri"),
    }


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_liked(s):
    log("Liked songs...")
    tracks = [norm_track(e.get("track"), e.get("added_at")) for e in s.pages("/me/tracks")]
    tracks = [t for t in tracks if t]
    log(f"  {len(tracks)} tracks")
    return tracks


def fetch_albums(s):
    log("Saved albums...")
    albums = [norm_album(e) for e in s.pages("/me/albums")]
    log(f"  {len(albums)} albums")
    return albums


def fetch_artists(s):
    log("Followed artists...")
    artists = [
        {"name": a.get("name"), "genres": a.get("genres") or [], "spotify_uri": a.get("uri")}
        for a in s.pages("/me/following", {"type": "artist"}, key="artists")
    ]
    log(f"  {len(artists)} artists")
    return artists


def fetch_playlists(s):
    log("Playlists...")
    playlists = []
    for p in s.pages("/me/playlists"):
        if not p:
            continue
        owner = p.get("owner") or {}
        pl = {
            "name": p.get("name"),
            "description": p.get("description") or "",
            "owner": owner.get("display_name") or owner.get("id"),
            "owner_id": owner.get("id"),
            "public": p.get("public"),
            "collaborative": p.get("collaborative"),
            "spotify_id": p.get("id"),
            "spotify_uri": p.get("uri"),
            "tracks_available": True,
            "tracks": [],
        }
        try:
            for e in s.pages(f"/playlists/{p['id']}/items"):
                # "item" is the current key; "track" is the pre-2026 name.
                t = norm_track(e.get("item") or e.get("track"),
                               e.get("added_at"), e.get("is_local"))
                if t:
                    pl["tracks"].append(t)
            log(f"  {pl['name']}: {len(pl['tracks'])} tracks")
        except ApiError as e:
            if e.code not in (403, 404):
                raise
            # Spotify only exposes the contents of playlists you own or collaborate on.
            pl["tracks_available"] = False
            log(f"  {pl['name']}: contents not accessible (owned by {pl['owner']}), skipped")
        playlists.append(pl)
    return playlists


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return name[:120] or "untitled"


def write_csv(path, tracks):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for i, t in enumerate(tracks, 1):
            row = {**t, "position": i}
            row["artists"] = "; ".join(t["artists"])
            row["album_artists"] = "; ".join(t["album_artists"])
            w.writerow(row)


def write_export(out, library):
    out.mkdir(parents=True, exist_ok=True)
    (out / "library.json").write_text(
        json.dumps(library, indent=2, ensure_ascii=False), encoding="utf-8")
    write_csv(out / "liked_songs.csv", library["liked_songs"])

    pl_dir = out / "playlists"
    pl_dir.mkdir(exist_ok=True)
    used = set()
    for pl in library["playlists"]:
        if not pl["tracks_available"]:
            continue
        base = safe_name(pl["name"])
        if base.lower() in used:
            base = f"{base} ({pl['spotify_id']})"
        used.add(base.lower())
        write_csv(pl_dir / f"{base}.csv", pl["tracks"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client-id", default=os.environ.get("SPOTIFY_CLIENT_ID"),
                    help="Spotify app client ID (or set SPOTIFY_CLIENT_ID)")
    ap.add_argument("--out", default="export", type=Path, help="output folder (default: export)")
    ap.add_argument("--port", default=8888, type=int,
                    help="loopback port for the login redirect (default: 8888)")
    ap.add_argument("--no-browser", action="store_true",
                    help="print the login URL instead of opening a browser")
    args = ap.parse_args()

    s = Session(args.client_id, args.port, not args.no_browser)
    me = s.get("/me")
    log(f"Logged in as {me.get('display_name') or me.get('id')}")

    library = {
        "format": "spotify-library-export",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": {"id": me.get("id"), "display_name": me.get("display_name")},
        "liked_songs": fetch_liked(s),
        "saved_albums": fetch_albums(s),
        "followed_artists": fetch_artists(s),
        "playlists": fetch_playlists(s),
    }
    write_export(args.out, library)
    log(f"\nDone. Wrote {args.out}/library.json, liked_songs.csv and playlists/*.csv")


if __name__ == "__main__":
    main()

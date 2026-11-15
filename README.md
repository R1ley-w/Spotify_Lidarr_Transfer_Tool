# Spotify to self-hosted music

These scripts copy a Spotify library to a self-hosted setup: Lidarr and
Prowlarr to get the music, Navidrome to play it. They read your playlists,
liked songs and saved albums, ask Lidarr for the albums, and rebuild the
playlists as `.m3u` files.

The scripts handle metadata only (titles, artists, albums, ISRCs, playlist
order). They never download audio themselves; Lidarr does that.

## Workflow

| step | script | what it does |
|---|---|---|
| 1 | `spotify_export.py` | writes your Spotify library to `export/library.json` and CSV files |
| 2 | `to_lidarr.py` | adds the albums in the export to Lidarr as wanted |
| 3 | (Lidarr and Prowlarr) | Lidarr finds, downloads and files the albums |
| 4 | `make_m3u.py` | matches the downloaded files to your playlists and writes `.m3u` files for Navidrome |

Prowlarr needs no setup for this. It supplies indexers to Lidarr, and Lidarr
searches them by itself. If you already have the music, skip steps 2 and 3.

## Set up Spotify access

You need Python 3.9 or later and a Spotify Premium account, because Spotify
only lets Premium accounts create developer apps. You do this part once.

1. Go to <https://developer.spotify.com/dashboard> and create an app.
2. Set the redirect URI to exactly `http://127.0.0.1:8888/callback`. Spotify
   rejects `localhost`.
3. Tick "Web API", save, and copy the Client ID. You do not need the client
   secret.

## Export your library

```bash
python3 spotify_export.py --client-id YOUR_CLIENT_ID
```

A browser tab opens and asks you to approve read-only access. The script
caches the login in `.spotify_token.json`, so later runs need no arguments:

```bash
python3 spotify_export.py
```

Options: `--out DIR` (default `export`), `--port N` (must match the redirect
URI), `--no-browser` (print the login URL instead of opening it).

Spotify only returns the contents of playlists you own or collaborate on. A
playlist you only follow is listed with `"tracks_available": false` and no
tracks. To export one, copy it into a playlist of your own in Spotify first.

## What the export contains

```
export/
  library.json          everything, in one file
  liked_songs.csv       one row per track
  playlists/<name>.csv  one file per playlist, in playlist order
```

`library.json`:

```json
{
  "format": "spotify-library-export",
  "version": 1,
  "exported_at": "...",
  "user": {"id": "...", "display_name": "..."},
  "liked_songs": [Track],
  "saved_albums": [{"name", "artists", "album_type", "release_date",
                    "total_tracks", "upc", "added_at", "spotify_uri"}],
  "followed_artists": [{"name", "genres", "spotify_uri"}],
  "playlists": [{"name", "description", "owner", "owner_id", "public",
                 "collaborative", "spotify_id", "spotify_uri",
                 "tracks_available", "tracks": [Track]}]
}
```

A `Track` has the fields below. The CSV files use the same fields as columns
and add `position`. Lists are joined with `; `.

| field | notes |
|---|---|
| `type` | `track` or `episode` (podcast episodes in playlists) |
| `title`, `artists`, `album`, `album_artists` | |
| `release_date`, `disc_number`, `track_number`, `duration_ms` | |
| `isrc` | the most reliable key for matching against other catalogues |
| `is_local` | true for local files added to a Spotify playlist |
| `added_at`, `spotify_uri` | |

## Send albums to Lidarr

Lidarr requests whole albums and has no way to request a single track. So
`to_lidarr.py` collects every distinct album that appears in your liked songs,
playlists and saved albums, and looks each one up through Lidarr. By default
one liked song is enough to request its album; the filter described further
down raises that bar.

Find the API key in Lidarr under Settings, General. Set it in your shell so it
stays out of your command history:

```bash
export LIDARR_API_KEY=your-key
```

Start with a dry run. It changes nothing in Lidarr and writes
`export/lidarr_plan.csv`:

```bash
python3 to_lidarr.py --url http://localhost:8686
```

The `status` column of the plan says what would happen to each album:

| status | meaning |
|---|---|
| `add` | found, not in Lidarr yet; will be added and monitored |
| `monitor` | in Lidarr but unmonitored; will be monitored |
| `already monitored` | nothing to do |
| `not found` | no album by that artist with that title in Lidarr's metadata |
| `lookup failed` | Lidarr's metadata server errored; re-run to retry |
| `duplicate` | two Spotify albums resolved to the same Lidarr album |

Check the rows where `match` is `loose`. Those matched only after ignoring
something like `(Deluxe)` in the title. When the plan looks right, apply it:

```bash
python3 to_lidarr.py --url http://localhost:8686 --apply --search
```

`--search` makes Lidarr search your indexers for each album straight away.
Without it Lidarr only monitors the albums and grabs them as releases appear.

### Only request albums you like more than one song from

Most of a big Liked Songs list is one song per album, and each of those would
pull in a whole album. `album_filter.py` stops that. It requests an album only
when at least N different songs from it appear in the lists being scanned.

You can preview the decision without touching Lidarr. This command scans Liked
Songs, requires 2 songs per album, and writes `export/album_requests.csv` with
a `request` or `skip` decision for every album:

```bash
python3 album_filter.py
```

Use `--min-tracks N` for a different threshold, `--playlist NAME` to scan a
playlist as well (repeatable), and `--no-liked` to leave Liked Songs out.

To send only the qualifying albums to Lidarr, give `to_lidarr.py` the same
threshold. This command does it for Liked Songs alone:

```bash
python3 to_lidarr.py --min-tracks 2 --no-playlists --no-albums
```

How songs are counted:

- A song counts once, however many times or in however many lists it appears.
- Songs are counted across everything scanned. One song in Liked Songs plus a
  different song from the same album in a scanned playlist makes two.
- Saved albums always qualify, since saving an album asks for all of it. Add
  `--no-albums` to leave them out.
- Lidarr never downloads a skipped album, so its songs end up in
  `unmatched.csv` when you build the playlists.

### Options for `to_lidarr.py`

- `--min-tracks N`: the threshold above. The default is 1, which requests
  every album.
- `--playlist NAME`: scan only this playlist (repeatable). Without it, every
  playlist is scanned.
- `--limit N`: handle only the first N albums. Use it for a first trial.
- `--no-liked`, `--no-playlists`, `--no-albums`: leave a source out.
- `--root-folder PATH`, `--quality-profile NAME`, `--metadata-profile NAME`:
  settings for artists that are new to Lidarr. By default the script uses the
  first root folder and that folder's default profiles.

### Things to know

- When an artist is new to Lidarr, the script monitors only the albums you
  requested and leaves the rest of the discography unmonitored. If a requested
  album shows as unmonitored after Lidarr finishes refreshing the artist, run
  the `--apply` command again and the script will monitor it.
- The script caches lookups in `export/lidarr_lookup_cache.json`, so re-runs
  are fast. Delete the file to look everything up again.
- Lidarr finds singles and EPs only if your metadata profile includes them.
- The script does not send followed artists, because adding an artist to
  Lidarr requests everything they have released.

## Make playlists for Navidrome

Navidrome imports playlists as `.m3u` files that point at audio files already
in your music folder. `make_m3u.py` reads the tags of your files, matches them
to the export, and writes one `.m3u` per playlist plus `Liked Songs.m3u`.

The script needs the `mutagen` package, which `uv` installs for you:

```bash
uv run make_m3u.py /path/to/music --out /path/to/music/Playlists
```

Without `uv`, run `python3 -m pip install mutagen` once and use `python3`
instead of `uv run`.

The playlists use paths relative to the `--out` folder. If you write them
inside the music folder, as in the command above, they keep working wherever
the server mounts that folder, and Navidrome picks them up on its next scan.
To keep the playlists somewhere else, pass `--prefix /music` (the music
folder's path as the server sees it) and the script writes absolute paths.

Other options: `--library FILE` (default `export/library.json`), `--no-liked`.

The script tries three ways to match a track, in this order:

1. The file's ISRC tag equals the Spotify ISRC.
2. The title is the same, an artist is shared, and the durations are within
   10 seconds.
3. The same as 2, ignoring additions to the title such as `(feat. X)` or
   `- Remastered 2011`. The script lists these matches at the end of the run
   so you can check them.

When several files fit, the script picks the one on the same album, then the
one closest in length. It leaves unmatched tracks out of the playlist and
lists them in `unmatched.csv` in the output folder. That file is also a list
of what your library is missing.

## Run the tests

```bash
python3 test_spotify_export.py
```

```bash
python3 test_make_m3u.py
```

```bash
python3 test_to_lidarr.py
```

```bash
python3 test_album_filter.py
```

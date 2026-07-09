# Setlist-er-ator

[![tests](https://github.com/thejames/setlisterator/actions/workflows/tests.yml/badge.svg)](https://github.com/thejames/setlisterator/actions/workflows/tests.yml)

Build music playlists on **Plex or YouTube Music** — seeded from a
[setlist.fm](https://www.setlist.fm/) show or assembled from scratch by
searching — with a nicer builder than either service's own UI. For Plex it also
reports which songs are missing from your library so you know what to buy.
Works as a command-line tool (`setlist_to_plex.py`, Plex-only) or a small local
[web app](#web-interface) (`web.py`, the full builder).

## Quickstart (macOS)

These steps assume macOS with [Homebrew](https://brew.sh). (Other platforms
aren't documented here.)

1. **Install Python and git** (skip whatever you already have):

   ```bash
   brew install python git
   ```

2. **Get the code and install dependencies:**

   ```bash
   git clone https://github.com/thejames/setlisterator.git
   cd setlisterator
   python3 -m venv .venv
   ./.venv/bin/pip install -r requirements.txt
   ```

3. **Grab your credentials:**
   - **setlist.fm API key** — request one (free) at <https://api.setlist.fm/docs/>.
   - **Plex token** — follow Plex's
     [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

4. **Configure** — copy the example and fill in the four values:

   ```bash
   cp .env.example .env
   # edit .env: SETLISTFM_API_KEY, PLEX_BASEURL, PLEX_TOKEN
   # (and PLEX_MUSIC_LIBRARY if your music section isn't named "Music")
   ```

5. **Run the web app:**

   ```bash
   ./.venv/bin/python web.py
   ```

   Open <http://127.0.0.1:5001>, pick a destination (Plex, or YouTube Music if
   configured), optionally paste a setlist.fm URL to seed from, and hit **Open
   builder**. Search your library/catalog to add tracks, drag to reorder, then
   **Save**. The **History** link in the navbar lists shows you've already made.

   Prefer the terminal? Use the CLI instead:

   ```bash
   ./.venv/bin/python setlist_to_plex.py "https://www.setlist.fm/…"
   ```

### Install as commands (optional)

An editable install exposes two console commands and keeps the web app's
templates working in place:

```bash
./.venv/bin/pip install -e .
setlisterator "https://www.setlist.fm/…"   # the CLI
setlisterator-web                           # the web app (http://127.0.0.1:5001)
```

More detail on configuration, flags, and matching behavior is below.

## Stack

- **Python 3** — CLI + core matching pipeline (`setlist_to_plex.py`)
- **[plexapi](https://github.com/pkkid/python-plexapi)** — Plex library search
  and playlist creation
- **[ytmusicapi](https://github.com/sigma67/ytmusicapi)** — YouTube Music search
  and playlist creation (unofficial; optional destination)
- **[requests](https://requests.readthedocs.io/)** — setlist.fm REST API
- **[python-dotenv](https://github.com/theskumar/python-dotenv)** — `.env` config
- **[Flask](https://flask.palletsprojects.com/)** + Jinja templates — the local
  web UI (`web.py`, `templates/`); no JS build step
- **pytest** — tests (`test_setlist_to_plex.py`, `test_web.py`), Plex/network-free

Persistence is a single JSON history file (no database); there's no hosted
component — it talks to Plex on `localhost`.

## Configure

Copy `.env.example` to `.env` and fill it in:

| Variable             | Required | Description                                      |
| -------------------- | -------- | ------------------------------------------------ |
| `SETLISTFM_API_KEY`  | yes      | setlist.fm API key (https://api.setlist.fm/docs/)|
| `PLEX_BASEURL`       | yes      | e.g. `http://localhost:32400`                    |
| `PLEX_TOKEN`         | yes      | your Plex `X-Plex-Token`                          |
| `PLEX_MUSIC_LIBRARY` | no       | library section name (default `Music`)           |
| `YTM_OAUTH_FILE`     | no       | YouTube Music OAuth file (see [below](#youtube-music-optional)); enables the YTM destination |
| `SETLIST_TO_PLEX_HISTORY` | no  | history file path (default below)                |
| `SETLIST_TO_PLEX_DRAFTS` | no   | builder-draft file path (default beside history) |
| `PORT`               | no       | web app port (default `5001`; web UI only)       |

## Usage

```bash
# By setlist ID
./.venv/bin/python setlist_to_plex.py 63de4613

# By full setlist.fm URL (the ID is parsed out)
./.venv/bin/python setlist_to_plex.py "https://www.setlist.fm/setlist/primus/2026/td-amp-ballantyne-charlotte-nc-6b756686.html"

# Override the auto-generated playlist name
./.venv/bin/python setlist_to_plex.py 63de4613 --name "Phish @ MSG"
```

The playlist name defaults to `{artist} - {venue}, {city} ({YYYY-MM-DD})`. If a
playlist with that name already exists, a numeric suffix is appended rather than
failing.

### Flags

| Flag           | Effect                                                       |
| -------------- | ------------------------------------------------------------ |
| `--name NAME`  | override the auto-generated playlist name                    |
| `--history`    | list previously created playlists (newest first) and exit    |
| `--backfill`   | fill in missing-track detail for old history entries and exit |
| `--quiet`      | suppress per-song match logging on stderr                    |
| `--force`      | re-process a setlist even if it was processed before         |
| `--no-history` | don't read or write the processed-setlist history            |

`--history` reads only the local history file, so it works without any Plex or
setlist.fm configuration: `setlist_to_plex.py --history`. `--backfill` re-matches
older shows (those recorded before per-track missing detail existed) so they
show up in the web **Buy list**; it needs the usual config and is idempotent.

## Logging vs. report

The actionable **report** (playlist name, missing, fuzzy) goes to **stdout**.
Per-song match decisions are logged to **stderr** (on by default; `--quiet` to
silence), one line per song with the matched track, its album, tier, and source:

```
Setlist: Primus — TD Amp Ballantyne, Charlotte (2026-06-16) — 12 songs
Library: matching against 214 tracks by Primus
  ✓  1. Tommy the Cat → 'Tommy the Cat' on 'Sailing the Seas of Cheese'  [exact/scoped]
  ✓  7. Hello Skinny → 'Hello Skinny / Constantinople' on 'The Desaturating Seven'  [medley/scoped]
  ✗  5. Jilly's on Smack → no match
```

The web Preview shows the source album inline with each matched track (and in
the per-song picker when there's more than one candidate).

## Processed-setlist history

Each setlist that produces a playlist is recorded in a small JSON file (default
`~/.config/setlist_to_plex/history.json`, honoring `XDG_CONFIG_HOME`; override
with `SETLIST_TO_PLEX_HISTORY`). Keys are setlist IDs, so a bare ID and a full
URL for the same show dedupe correctly. On a repeat run the tool prompts when
interactive, or skips with a hint to pass `--force` when run non-interactively
(cron, pipes) so it never hangs. Only runs that actually create a playlist are
recorded — a show that matched nothing is retried next time. The history lives
outside the repo, so it won't be committed.

## Web interface

A small local web UI (dark theme, server-rendered Jinja + a little vanilla JS —
still no build step) wraps the same matching pipeline:

```bash
./.venv/bin/pip install -r requirements.txt   # pulls in Flask
./.venv/bin/python web.py                      # http://127.0.0.1:5001
```

> Default port is **5001** (macOS uses 5000 for AirPlay Receiver). Override with
> `PORT=8080 ./.venv/bin/python web.py`.

On the landing page, choose a **destination** — **Plex** (your library) or
**YouTube Music** (the streaming catalog; only offered when
[configured](#youtube-music-optional)) — optionally paste a setlist URL or ID to
**seed** from, name it if you like, and hit **Open builder**. Seeding matches
the show against the chosen service and drops the best match for each song into
the builder; leave the setlist blank to start from scratch.

In the **builder** you assemble an ordered playlist: **search** the destination
(your Plex library or the YTM catalog) and **+ Add** tracks, **drag** the ⠿
handle to reorder, **✕** to remove, and edit the name up top. Your work is saved
as a draft on every change, so a refresh or navigating away picks up where you
left off. When it looks right, **Save** materializes it into a real playlist on
that service. For a Plex, setlist-seeded playlist, a **Not in your library**
panel lists the songs that had no match — search above to add another version,
or note them for buying.

The top navbar's **History** link lists every playlist you've made, newest
first, tagged by service. A setlist-seeded row can **Re-open** it in the builder
or link to its setlist.fm source; a **Plex** one can also **Update** the
existing playlist — re-match the show against your *current* library and confirm
the now-available tracks to add. Update is **add-only** (nothing removed), so
when you buy a missing song you can fold it into the playlist you already made.
From-scratch playlists get a light entry (no setlist to re-open). The **Buy
list** link aggregates every show's missing tracks into one deduped list,
grouped by artist, with the album each is from — sourced from setlist.fm's "Songs
on Albums" data with [MusicBrainz](https://musicbrainz.org/) as a fallback,
looked up lazily and cached. Buy list and missing-reporting are **Plex-only**
(YouTube Music's catalog effectively has everything).

The **Attended** link turns your setlist.fm "I was there" history into a
worklist. Enter a setlist.fm username (pre-filled from the optional
`SETLISTFM_USER` in your `.env`) and it lists every show you've marked attended,
newest first. Each row has a **Build** button that opens it in the builder;
shows you've already made are flagged `created ✓` with **Re-open** / **Update**
instead. It reads public attended data, so any public username works.

> **Local only.** The app talks to your local Plex server and holds your Plex
> token (and YouTube Music OAuth), so it binds to `127.0.0.1` and has no
> authentication. Don't expose it to a network.

### YouTube Music (optional)

YouTube Music has no official API, so auth means borrowing a logged-in browser
session. The auth file defaults to `ytm_oauth.json` beside `history.json`
(override with `YTM_OAUTH_FILE`).

**One-click connect (recommended).** In the web app, when YouTube Music isn't
set up yet, the landing page shows **Connect YouTube Music →**. It scans the
browsers you're signed into, lists them **by Google account** (so you pick the
right one across multiple profiles), and on pick reads that session's cookies
directly — no header copying. macOS asks to use your keychain once (click
Allow); Safari needs no prompt. Re-connect the same way whenever the session
expires. (Needs the `browser_cookie3` package, included in `requirements.txt`.)

The two manual methods below still work and auto-detect:

**Browser auth (manual).** Copies request headers from a logged-in YouTube Music
session:

```bash
mkdir -p ~/.config/setlist_to_plex
./.venv/bin/ytmusicapi browser --file ~/.config/setlist_to_plex/ytm_oauth.json
```

It waits for you to paste request headers. To get them: open
<https://music.youtube.com> (signed in) → DevTools → **Network** → filter for
`/browse` → click a **POST** request to `…/youtubei/v1/browse…` → copy its
**request headers** (Firefox: right-click → Copy Value → Copy Request Headers).
Paste into the terminal, then press **Ctrl-D**. Restart the web app; YouTube
Music is now selectable. No `.env` change needed.

**OAuth (alternative — often rejected).** Uses a Google Cloud OAuth client
("TVs and Limited Input devices"; enable the YouTube Data API, add yourself as a
test user). Run `ytmusicapi oauth --client-id … --client-secret … --file
<path>`, then put the **same** `YTM_CLIENT_ID` / `YTM_CLIENT_SECRET` in `.env`
(needed at runtime to refresh the token). Note: YouTube frequently returns
`HTTP 400` for requests made with self-created OAuth clients — if search/save
fail that way, switch to browser auth above.

If no valid auth file is present, YouTube Music is greyed out with a reason and
everything works Plex-only. `ytmusicapi` is unofficial and the CLI stays
Plex-only.

## Deploy (self-host with Docker)

To run it as an always-on service on your network — your Mac, a NAS, a Pi —
anywhere that can reach Plex directly:

```bash
cp .env.example .env       # fill in your keys/token and PLEX_BASEURL
docker compose up -d       # builds the image and starts it
# → http://<that-host>:5001
```

It runs under gunicorn, persists `history.json` (including the album cache) in a
named volume, and reaches your Plex over the LAN — no tunnel needed. Change the
published port by editing the `ports:` mapping in `docker-compose.yml`.

> **Still unauthenticated.** This exposes the app (and your Plex token's
> reach) to anyone on the network, so keep it on a trusted LAN. For remote
> access, put it behind a tunnel (Tailscale, Cloudflare Tunnel) with
> authentication — not included here.

### Install on Unraid

A public multi-arch image is published to GHCR by CI on every push to `main`:
`ghcr.io/thejames/setlisterator:latest` (and `:vX.Y.Z` for tags). Unraid's
*Add Container* **Template** field is a dropdown of templates it already knows —
you can't paste a URL there — so use one of these:

**Option A — use the bundled template** (fields pre-filled). Copy
`unraid/setlisterator.xml` to your Unraid flash drive at
`/boot/config/plugins/dockerMan/templates-user/` (easiest over the network:
`\\TOWER\flash\config\plugins\dockerMan\templates-user\setlisterator.xml`). Then
**Docker → Add Container**, pick **setlisterator** from the *Template* dropdown,
fill in your keys, **Apply**.

**Option B — configure manually** (no file copy). **Docker → Add Container**,
leave *Template* blank, and set:
- *Repository*: `ghcr.io/thejames/setlisterator:latest`
- *Port*: container `5001` → host `5001`
- *Path*: container `/data` → host `/mnt/user/appdata/setlisterator`
- *Variables*: `SETLISTFM_API_KEY`, `PLEX_BASEURL` (e.g.
  `http://10.0.0.10:32400`), `PLEX_TOKEN`, `PLEX_MUSIC_LIBRARY`

Either way, open the WebUI on the mapped port (default `5001`); `history.json`
persists in the appdata path.

## How matching works

Each setlist song is matched to a Plex track in two stages:

1. **Artist-scoped (primary).** The setlist artist is resolved once in your
   library and *all* of their tracks are pulled and compared locally. This
   sidesteps Plex's search tokenizer, which can otherwise miss tracks over
   punctuation or Unicode quirks (e.g. an ASCII hyphen `-` in the setlist vs a
   typographic hyphen `‐` in your library) and over result truncation on common
   titles.
2. **Global search (fallback).** Only if the artist isn't in your library, or a
   song isn't among their tracks (covers of other artists), a global title
   search is used.

Titles are compared with normalization (lowercased, punctuation stripped,
whitespace collapsed) across four tiers:

| Tier  | Quality | Example                                                       |
| ----- | ------- | ------------------------------------------------------------- |
| exact | exact   | `Run Like an Antelope!!!` == `run like an antelope`           |
| loose | fuzzy   | `Wilson, Pt. 2` ≈ `Wilson Part 2`; drops `(...)`, `feat. ...` |
| medley| fuzzy   | `Hello Skinny / Constantinople` matches `Hello Skinny`        |
| prefix| fuzzy   | `Tommy the Cat - Live` matches `Tommy the Cat` (2+ word title)|

Anything past the exact tier is reported under **FUZZY MATCHES** so you can
spot-check for false positives. A medley track that matches more than one
setlist song is added to the playlist only once.

## Output

- Playlist name and number of tracks added.
- **MISSING TRACKS** — songs with no match, as `{position}. {artist} - {title}`.
- **FUZZY MATCHES** — non-exact matches, setlist title vs the Plex track.

Exit codes: `0` success, `2` config error, `3` setlist error, `4` Plex error.

## Tests

```bash
./.venv/bin/pip install pytest
./.venv/bin/python -m pytest
```

# Setlist-er-ator

[![tests](https://github.com/thejames/setlisterator/actions/workflows/tests.yml/badge.svg)](https://github.com/thejames/setlisterator/actions/workflows/tests.yml)

Create a Plex music playlist from a [setlist.fm](https://www.setlist.fm/) show,
and report which songs are missing from your library so you know what to buy.
Works as a command-line tool (`setlist_to_plex.py`) or a small local
[web app](#web-interface) (`web.py`).

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

   Open <http://127.0.0.1:5001>, paste a setlist.fm URL, hit **Preview**, pick
   track versions where you're offered a choice, then **Create playlist**. The
   **History** link in the navbar lists shows you've already made.

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
| `SETLIST_TO_PLEX_HISTORY` | no  | history file path (default below)                |
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
| `--quiet`      | suppress per-song match logging on stderr                    |
| `--force`      | re-process a setlist even if it was processed before         |
| `--no-history` | don't read or write the processed-setlist history            |

`--history` reads only the local history file, so it works without any Plex or
setlist.fm configuration: `setlist_to_plex.py --history`.

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

A small local web UI wraps the same matching pipeline:

```bash
./.venv/bin/pip install -r requirements.txt   # pulls in Flask
./.venv/bin/python web.py                      # http://127.0.0.1:5001
```

> Default port is **5001** (macOS uses 5000 for AirPlay Receiver). Override with
> `PORT=8080 ./.venv/bin/python web.py`.

Paste a setlist URL or ID and hit **Preview** — it matches the show against
your library and shows the **full setlist in order** *without* creating
anything; songs not in your library are flagged inline (and also listed
separately below). When a song matches more than one library track (e.g. the
same song on a studio album, a live record, and a compilation) its row shows a
**dropdown** so you can pick the version — by album — you want; the best match
is preselected. Review it (and tweak the playlist name if you like), then click
**Create playlist** to commit. Create rebuilds the chosen tracks by their Plex
rating keys, so nothing is re-matched, and the run is recorded in the same
[history](#processed-setlist-history) the CLI uses.

The top navbar's **History** link opens a page listing every show you've
created, newest first. Each row can **Re-open** the setlist in Preview, link out
to its setlist.fm source, or **Update** the existing playlist: it re-matches the
show against your *current* library and shows the tracks that are now available
but not yet in the playlist, for you to confirm. Update is **add-only** — it
tops up the existing playlist (no new copy, nothing removed), so when you buy a
song that was missing, you can fold it into the playlist you already made. The
**Buy list** link aggregates every show's missing tracks into one deduped list.

> **Local only.** The app talks to your local Plex server and holds your Plex
> token, so it binds to `127.0.0.1` and has no authentication. Don't expose it
> to a network.

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

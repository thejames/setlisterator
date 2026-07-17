# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Turns a [setlist.fm](https://www.setlist.fm/) show into a Plex music playlist, and reports which songs are missing from your Plex library so you know what to buy. Ships as a CLI (`setlist_to_plex.py`) and a small local Flask web app (`web.py`) that share the same matching pipeline. No database, no hosted component — it talks to a Plex server (usually on `localhost`) and persists a single JSON history file outside the repo.

## Commands

Everything runs out of the local venv (`./.venv/bin/...`).

```bash
./.venv/bin/pip install -r requirements.txt      # runtime deps (incl. Flask)
./.venv/bin/pip install -e .                      # editable install → `setlisterator` / `setlisterator-web` console scripts
./.venv/bin/python -m pytest                      # full test suite (no network/Plex needed)
./.venv/bin/python -m pytest test_web.py          # one file
./.venv/bin/python -m pytest test_setlist_to_plex.py::test_parse_bare_id   # one test
./.venv/bin/python setlist_to_plex.py 63de4613    # CLI: by setlist ID or full URL
./.venv/bin/python web.py                         # web app → http://127.0.0.1:5001
```

Web app defaults to port **5001** (macOS uses 5000 for AirPlay); override with `PORT`. Config comes from `.env` (copy `.env.example`): `SETLISTFM_API_KEY`, `PLEX_BASEURL`, `PLEX_TOKEN` are required; `PLEX_MUSIC_LIBRARY` defaults to `Music`.

## Architecture

**Flat two-module layout** (`pyproject.toml` → `py-modules = ["setlist_to_plex", "web"]`), no packages:

- **`setlist_to_plex.py`** — the core library *and* the CLI: setlist.fm fetching, the matching engine, Plex connection, playlist creation, JSON history persistence, plus a `MusicService` seam (`PlexService`/`PLEX_SERVICE`) that `gather_matches` targets.
- **`web.py`** — the live Flask frontend. It `import setlist_to_plex as core` and calls those functions directly (preview → create → update); no business logic of its own, only routes, form parsing, and rendering (`templates/*.html` Jinja + `static/app.js`, no JS build step).

**The pipeline is read → match → create, split so the web UI can insert a human review step:**

1. `gather_matches(config, setlist_id, ...)` — orchestrator. Fetches the setlist, resolves the artist, matches every song, returns a dict (`matched`/`missing`/`fuzzy`/`songs` + `candidates` per song). **Read-only — creates nothing.** Both the CLI and the web `/preview` call this.
2. `create_playlist(config, name, rating_keys, ...)` — the only thing that mutates Plex. Takes already-chosen Plex `rating_key`s, calls `plex.createPlaylist`, records history. Nothing is re-matched here.
3. `add_to_playlist(...)` — the "Update" path; add-only top-up of an existing playlist (`playlist.addItems`).

The load-bearing hand-off between stages is the Plex **`rating_key`** (an integer). It's the universal track identifier threaded through the match IR, the history JSON, and both frontends' form fields (`web._picked_rating_keys`). The web flow is: `/preview` renders candidates → user picks versions → `/create` rebuilds tracks *by rating key* with no re-matching.

**The matcher is the most substantial and valuable code** (roughly the middle third of `setlist_to_plex.py`). Two-tier strategy:
1. **Artist-scoped (primary)** — resolve the setlist artist once, pull *all* their Plex tracks, compare locally. Deliberately sidesteps Plex's search tokenizer, which misses tracks over punctuation/Unicode quirks and result truncation.
2. **Global search (fallback)** — only when the artist isn't in the library or a song isn't theirs (covers).

Titles compare via `normalize_simple` / `normalize_aggressive` across four ranked tiers — `exact`, `loose` (fuzzy), `medley` (slash-split segments), `prefix` — in `_title_rank` / `_ranked_matches`. Anything past `exact` is surfaced as a **fuzzy match** for spot-checking. Results are `Match = namedtuple("track quality tier source")`; `source` is `"scoped"` or `"global"`.

**A `MusicService` seam exists, and Plex is its only implementation.** `gather_matches(config, setlist_id, ..., service=PLEX_SERVICE)` takes a service adapter whose `connect()` returns the `(client, section)` pair the matcher duck-types (reading `grandparentTitle`, `originalTitle`, etc.); `PlexService` (in `setlist_to_plex.py`) is that adapter, and every caller uses the Plex default. The seam is a thin extension point, not a live abstraction — the app is Plex-only. (A YouTube Music backend was tried and removed; see the memory note on why it isn't coming back.) The Plex `rating_key` (an integer) is the identifier baked into the IR, history, and forms.

**Config** is a plain dict from `load_config()` threaded through every function (`config["plex_baseurl"]`, etc.). Plex auth is a static token (`PlexServer(baseurl, token)`) — no OAuth. setlist.fm is a header API key.

**History** is a single JSON file keyed by setlist ID (default `~/.config/setlist_to_plex/history.json`, honoring `XDG_CONFIG_HOME`; override with `SETLIST_TO_PLEX_HISTORY`). It lives outside the repo. Only runs that actually create a playlist are recorded, so a show that matched nothing is retried next time. It also caches per-track album data (from setlist.fm's "Songs on Albums", MusicBrainz fallback) used by the web **Buy list**.

## Conventions

- **stdout vs stderr (CLI):** the actionable report (playlist name, missing, fuzzy) goes to **stdout**; per-song match decisions log to **stderr** (silence with `--quiet`). CLI exit codes: `0` ok, `2` config error, `3` setlist error, `4` Plex error.
- **Tests are network-free by design.** `test_setlist_to_plex.py` covers pure logic and the matcher; `test_web.py` monkeypatches `core.load_config` / `gather_matches` / `create_playlist` to exercise routing and rendering only. Keep new tests offline the same way. There's no lint step.
- The web app is **local-only and unauthenticated** — it holds your Plex token and binds to `127.0.0.1`. Don't add anything that assumes it's safely network-exposed.
- Version is CalVer (`YYYY.M.PATCH` in `pyproject.toml`); commits follow conventional-commit style.

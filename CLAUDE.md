# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Builds music playlists on **Plex or YouTube Music**, either seeded from a [setlist.fm](https://www.setlist.fm/) show or assembled from scratch by searching. For Plex it also reports which songs are missing from your library so you know what to buy. Ships as a CLI (`setlist_to_plex.py`, Plex-only) and a small local Flask web app (`web.py`, the full builder). No database, no hosted component — it talks to a Plex server (usually on `localhost`) and/or YouTube Music, and persists JSON state (history + in-progress drafts) outside the repo.

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

Web app defaults to port **5001** (macOS uses 5000 for AirPlay); override with `PORT`. Config comes from `.env` (copy `.env.example`): `SETLISTFM_API_KEY`, `PLEX_BASEURL`, `PLEX_TOKEN` are required; `PLEX_MUSIC_LIBRARY` defaults to `Music`. YouTube Music is optional. Easiest setup is the web app's **Connect YouTube Music** flow (`/ytm/connect`), which reads a logged-in browser's cookies via `browser_cookie3` and writes the auth file — no env vars. Manual alternatives: `ytmusicapi browser` (header paste) or `ytmusicapi oauth` (needs `YTM_CLIENT_ID`/`YTM_CLIENT_SECRET`, and YouTube often rejects self-made OAuth clients with HTTP 400). Auth file defaults to `~/.config/setlist_to_plex/ytm_oauth.json` (`YTM_OAUTH_FILE`); `connect_ytmusic` auto-detects browser-vs-oauth. When unset, YTM is greyed out and everything works Plex-only.

## Architecture

**Flat module layout** (`pyproject.toml` → `py-modules`), no packages. Dependency direction is one-way: `web` → `builder` → `{setlist_to_plex, ytm_service}` → (`ytm_service` → `setlist_to_plex`). `setlist_to_plex` never imports the others.

- **`setlist_to_plex.py`** — core library + CLI: setlist.fm fetching, the matching engine, Plex connection/playlist creation, and JSON history/draft persistence.
- **`ytm_service.py`** — the YouTube Music backend (optional `ytmusicapi` import).
- **`builder.py`** — playlist-builder business logic (seed/add/remove/reorder/search/materialize), so the web layer stays thin.
- **`web.py`** — thin Flask frontend: routes, form parsing, rendering (`templates/*.html` Jinja + `static/*.js`, no JS build step). No business logic of its own.

**The MusicService seam.** `gather_matches(config, setlist_id, ..., service=PLEX_SERVICE)` takes a service adapter whose `connect(config)` returns a `(client, section)` pair. The matcher only ever duck-types that `section` (`searchArtists`/`searchTracks`) and the tracks it returns (`.title`/`.grandparentTitle`/`.parentTitle`/`.ratingKey`) — so `PlexService` (in `setlist_to_plex.py`) hands it live plexapi objects, and `YTMService` (in `ytm_service.py`) hands it thin shim objects (`YTMSection`/`_YTMTrack`) presenting the same surface. The entire two-tier matcher runs against either backend unchanged. Track identity stays a bare id (Plex `ratingKey` int, YTM `videoId` str) namespaced only at the draft/history envelope via a `service` tag — one draft targets one service, so per-track prefixes aren't needed.

**Two mutation paths, deliberately forked** (not abstracted — low shared value): `create_playlist(config, name, rating_keys, ...)` (Plex, `plex.createPlaylist`) and `create_playlist_ytm(config, name, video_ids, ...)` (`ytmusicapi.create_playlist`). `builder.materialize` dispatches on `draft["service"]`. `add_to_playlist` (add-only Plex "Update") stays Plex-only; editing existing playlists is not yet implemented (a reserved `target_playlist_id` draft field marks the seam).

**The matcher is the most valuable code** (middle third of `setlist_to_plex.py`). Two-tier: **artist-scoped** (resolve the artist once, pull all their tracks, compare locally — sidesteps the backend's search tokenizer) then **global search** fallback. Titles compare via `normalize_simple`/`normalize_aggressive` across four ranked tiers (`exact`/`loose`/`medley`/`prefix`) in `_title_rank`/`_ranked_matches`; anything past `exact` is a **fuzzy match**. Results are `Match = namedtuple("track quality tier source")`. Note YTM's artist-scoped tier is inherently shallower (ytmusicapi has no full "all tracks by artist" endpoint, only a top-songs shelf), so it falls to global search more often.

**The web builder flow.** Landing page picks a destination + optional setlist seed → `/builder/seed` creates a **draft** (server-side JSON) and redirects to `/builder/<id>`. There, htmx fragments drive search/add/remove/reorder (SortableJS for drag); each mutation re-saves the draft so a refresh resumes. `/builder/<id>/save` calls `materialize` then deletes the draft. Fragments render `_tracklist.html`/`_search_results.html`; the action-bar track count updates via an htmx out-of-band swap.

**Config** is a plain dict from `load_config()` (Plex/setlist.fm) plus `ytm_oauth_path` merged in by the web layer (`_web_config`). Plex auth is a static token; YTM auth is a file-based OAuth blob loaded by `ytmusicapi`.

**Persistence** (both under `~/.config/setlist_to_plex/`, honoring `XDG_CONFIG_HOME`, sharing the atomic `_load_json_store`/`_save_json_store` helpers):
- **`history.json`** — keyed by setlist ID; only created playlists are recorded. Each entry carries a `service` (`plex`/`ytm`, defaulting to `plex` for old entries) and `source` (`setlist`/`builder`). From-scratch playlists record a light `builder-<uuid>`-keyed entry. Also caches per-track album data (setlist.fm "Songs on Albums", MusicBrainz fallback) for the Plex-only **Buy list**.
- **`drafts.json`** (override `SETLIST_TO_PLEX_DRAFTS`) — in-progress builder drafts keyed by uuid; deleted on Save.

## Conventions

- **stdout vs stderr (CLI):** the actionable report (playlist name, missing, fuzzy) goes to **stdout**; per-song match decisions log to **stderr** (silence with `--quiet`). CLI exit codes: `0` ok, `2` config error, `3` setlist error, `4` Plex error.
- **Tests are network-free by design.** `test_setlist_to_plex.py` covers pure logic and the service seam (a `FakeMusicService` double); `test_ytm_service.py` uses a `FakeYTMusic` client (runs even without `ytmusicapi` installed, since the import is soft); `test_builder.py` / `test_web.py` monkeypatch `gather_matches` / `builder.*` / the draft store. Keep new tests offline the same way. There's no lint step.
- **Adapters call module-level functions by name, not `self`.** `PlexService.connect` invokes `connect_plex`/`get_music_section` unqualified so existing monkeypatched tests still take effect — preserve this when extending.
- The web app is **local-only and unauthenticated** — it holds your Plex token and binds to `127.0.0.1`. Don't add anything that assumes it's safely network-exposed.
- Version is CalVer (`YYYY.M.PATCH` in `pyproject.toml`); commits follow conventional-commit style.

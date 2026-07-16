# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Builds **Plex** music playlists, either seeded from a [setlist.fm](https://www.setlist.fm/) show or assembled from scratch by searching, and reports which songs are missing from your library so you know what to buy. Ships as a CLI (`setlist_to_plex.py`) and a small local Flask web app (`web.py`, the full builder). No database, no hosted component — it talks to a Plex server (usually on `localhost`) and persists JSON state (history + in-progress drafts) outside the repo.

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

**Flat module layout** (`pyproject.toml` → `py-modules`), no packages. Dependency direction is one-way: `web` → `builder` → `setlist_to_plex`. `setlist_to_plex` never imports the others.

- **`setlist_to_plex.py`** — core library + CLI: setlist.fm fetching, the matching engine, Plex connection/playlist creation, and JSON history/draft persistence.
- **`builder.py`** — playlist-builder business logic (seed/add/remove/reorder/search/materialize), so the web layer stays thin.
- **`web.py`** — thin Flask frontend: routes, form parsing, rendering (`templates/*.html` Jinja + `static/*.js`, no JS build step). No business logic of its own.

**Plex-only, deliberately.** A YouTube Music backend existed behind a pluggable `MusicService` seam and was removed in 2026-07: its auth was a browser-cookie snapshot that died in <7 days and could never be re-established inside the headless Unraid container, logged-out failures were silent, and ytmusicapi (1.12.1, latest) can't read owned playlists so edit/delete were unimplementable. The buy list — the point of the app — has no streaming analogue anyway. **Don't re-add a second backend here**; a YTM builder would be a separate app. `gather_matches` calls `connect_plex_section(config)` for its `(client, section)` pair; track identity is a bare Plex `ratingKey`, with no `service` tag on the draft/history envelope.

**Editing an existing playlist** (from a History row): `builder.draft_from_playlist` opens the live playlist into an edit-mode draft (`target_playlist_id` set; rows carry `item_id` = the Plex `playlistItemID`). On save, `builder.apply_edits` → `setlist_to_plex.apply_playlist_edits` smart-diffs the desired rows against the live playlist (remove/add/reorder via `moveItem`, rename via `editTitle`); `delete_playlist` removes it and drops the History entry. The old add-only "Update" flow was retired — Edit replaces it.

**The matcher is the most valuable code** (middle third of `setlist_to_plex.py`). Two-tier: **artist-scoped** (resolve the artist once, pull all their tracks, compare locally — sidesteps the backend's search tokenizer) then **global search** fallback. Titles compare via `normalize_simple`/`normalize_aggressive` across four ranked tiers (`exact`/`loose`/`medley`/`prefix`) in `_title_rank`/`_ranked_matches`; anything past `exact` is a **fuzzy match**. Results are `Match = namedtuple("track quality tier source")`. Note `builder._row_from_match` currently keeps only id/title/artist/album and **discards `tier`/`source`/`quality`/`candidates`** — the builder UI shows no match-quality signal.

**The web builder flow.** Landing page takes an optional setlist seed + name → `/builder/seed` creates a **draft** (server-side JSON) and redirects to `/builder/<id>`. There, htmx fragments drive search/add/remove/reorder (SortableJS for drag); each mutation re-saves the draft so a refresh resumes. `/builder/<id>/save` calls `materialize` then deletes the draft. Fragments render `_tracklist.html`/`_search_results.html`; the action-bar track count updates via an htmx out-of-band swap.

**Config** is a plain dict from `load_config()` (Plex/setlist.fm). Plex auth is a static token.

**Persistence** (both under `~/.config/setlist_to_plex/`, honoring `XDG_CONFIG_HOME`, sharing the atomic `_load_json_store`/`_save_json_store` helpers):
- **`history.json`** — keyed by setlist ID; only created playlists are recorded. Each entry carries a `source` (`setlist`/`builder`). Entries written before 2026-07 may still carry a vestigial `service` key; nothing reads it. From-scratch playlists record a light `builder-<uuid>`-keyed entry. Also caches per-track album data (setlist.fm "Songs on Albums", MusicBrainz fallback) for the Plex-only **Buy list**.
- **`drafts.json`** (override `SETLIST_TO_PLEX_DRAFTS`) — in-progress builder drafts keyed by uuid; deleted on Save.

## Conventions

- **stdout vs stderr (CLI):** the actionable report (playlist name, missing, fuzzy) goes to **stdout**; per-song match decisions log to **stderr** (silence with `--quiet`). CLI exit codes: `0` ok, `2` config error, `3` setlist error, `4` Plex error.
- **Tests are network-free by design.** `test_setlist_to_plex.py` covers pure logic; its `_wire_gather` helper monkeypatches `connect_plex`/`get_music_section` **by module-level name** to run the matcher offline — `connect_plex_section` calls them unqualified so this keeps working, so preserve that when extending. `test_builder.py` / `test_web.py` monkeypatch `gather_matches` / `builder.*` / the draft store. Keep new tests offline the same way. There's no lint step.
- The web app is **local-only and unauthenticated** — it holds your Plex token and binds to `127.0.0.1`. Don't add anything that assumes it's safely network-exposed.
- Version is CalVer (`YYYY.M.PATCH` in `pyproject.toml`); commits follow conventional-commit style.

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
2. `create_playlist(config, name, rating_keys, ..., poster_path=None)` — the only thing that mutates Plex on the create path. Takes already-chosen Plex `rating_key`s, calls `plex.createPlaylist`, records history, returns a `CreateResult(name, poster)`. Nothing is re-matched here.
3. `add_to_playlist(...)` — the "Update" path; add-only top-up of an existing playlist (`playlist.addItems`). Refreshes the Plex summary, since a bought track means the old one is stale.

The **playlist summary** written to Plex is *derived, never merged*: every write path rebuilds it from scratch via `_write_summary(playlist, history_meta)`, which reads the playlist's live membership and hands it to the pure `_playlist_summary(history_meta, member_keys)`. Each setlist song lands in exactly one state — **added** (its matched track is in the playlist), **declined** (matched, but not in the playlist), **missing** (nothing matched) — see the glossary in `CONTEXT.md`. Because state is derived at each write rather than remembered, a track removed in the editor or directly in Plex reads correctly next time.

What makes that possible without a matcher call is the per-song **map** stored on the history entry (`songs`: one row per setlist position with its title, setlist.fm release, and matched `rating_key` or null). `song_map(result)` builds it from a `gather_matches` result so the CLI and the web produce the same shape — note its keys are **strings**, not the `rating_key` ints used elsewhere, so the web's form round-trip can't change their type. Only paths that re-match supply a fresh map, and `add_to_playlist` reconciles it via `_merge_song_maps` *before* anything reads it: a stored key wins over a re-match's default, because the stored one is the version actually in the playlist. Entries predating the map get **no** summary rather than a degraded one, so a good summary is never blanked; the next Update backfills the map. Summary writes are best-effort like the poster: a failure warns and never undoes the save.

An **optional playlist poster** (the "folder image") can be uploaded on all three write paths — setlist create, Build, and the editor. `create_playlist` and `set_playlist` both take an optional `poster_path` and defer to the shared `set_playlist_poster(playlist, poster_path)` helper (`uploadPoster` + best-effort `uploadSquareArt`). It's **best-effort**: an image problem never blocks or undoes the save, it just surfaces a warning — see `docs/adr/0002-playlist-poster-best-effort.md`. The web layer saves the browser upload to a temp file (`web._take_poster_upload`) and deliberately avoids Flask's `MAX_CONTENT_LENGTH` (a 413 would lose the preview session).

The load-bearing hand-off between stages is the Plex **`rating_key`** (an integer). It's the universal track identifier threaded through the match IR, the history JSON, and both frontends' form fields (`web._picked_rating_keys`). The web flow is: `/preview` renders candidates → user picks versions → `/create` rebuilds tracks *by rating key* with no re-matching. Switching the preferred album on the preview page hits `/rematch` (JSON, same `gather_matches`) and the client re-orders only *untouched* rows in place, so hand-edits survive — see `docs/adr/0001-client-rematch-endpoint.md`.

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

## Agent skills

### Issue tracker

Local markdown — issues and specs live as files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles, recorded as the `Status:` line in each issue file (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` + `docs/adr/` at the repo root (created lazily by `/domain-modeling`). See `docs/agents/domain.md`.

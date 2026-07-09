#!/usr/bin/env python3
"""Local web interface for setlist_to_plex.

Run:
    ./.venv/bin/python web.py        # serves http://127.0.0.1:5001
    PORT=8080 ./.venv/bin/python web.py   # or pick your own port

Default port is 5001 (5000 is taken by AirPlay Receiver on macOS); override
with the PORT environment variable.

This app talks to your LOCAL Plex server and carries your Plex token, so it
binds to 127.0.0.1 only and has no authentication. Do not expose it to a
network. It wraps the core pipeline plus the playlist builder:

    builder.seed_from_setlist() -> a draft seeded from a setlist.fm show
    builder.search()/add/remove/reorder -> assemble the draft interactively
    builder.materialize()       -> commit the draft to Plex or YouTube Music
"""

import json
import os

from flask import Flask, redirect, render_template, request, url_for

import builder as bld
import setlist_to_plex as core
import ytm_service as ytm

app = Flask(__name__)


def _error(title, message, status=200):
    return render_template("error.html", title=title, message=message), status


def _web_config():
    """Core config plus the YouTube Music OAuth path + client creds (raises
    ConfigError from load_config)."""
    client_id, client_secret = ytm.ytm_client_creds()
    return {**core.load_config(),
            "ytm_oauth_path": ytm.ytm_oauth_path(),
            "ytm_client_id": client_id,
            "ytm_client_secret": client_secret}


def _load_draft(draft_id):
    return core.load_drafts().get(draft_id)


def _persist(draft):
    drafts = core.load_drafts()
    drafts[draft["id"]] = draft
    core.save_drafts(drafts)


def _delete_draft(draft_id):
    drafts = core.load_drafts()
    if drafts.pop(draft_id, None) is not None:
        core.save_drafts(drafts)


def _tracklist(draft):
    """Render the tracklist fragment (with an out-of-band track count)."""
    return render_template("_tracklist.html", draft=draft, oob=True)


@app.get("/")
def index():
    try:
        core.load_config()
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    return render_template("index.html", ytm=ytm.load_ytm_config(),
                           ytm_can_connect=ytm.browser_connect_available())


@app.get("/history")
def history():
    """List previously created playlists from the JSON history file."""
    # No config needed — this only reads the local history file.
    entries = list(core.load_history(core.history_path()).values())
    entries.sort(key=lambda e: e.get("processed_at", ""), reverse=True)
    return render_template("history.html", entries=entries)


@app.get("/buylist")
def buylist():
    """Aggregate every show's missing tracks into one deduped buy-list."""
    # No config needed — this only reads the local history file. Lazily enrich
    # each missing track with its likely album (MusicBrainz), caching the result
    # back into history so it's only looked up once.
    path = core.history_path()
    history = core.load_history(path)
    if core.enrich_missing_albums(history):
        try:
            core.save_history(path, history)
        except OSError:
            pass

    by_key = {}
    for entry in history.values():
        for track in entry.get("missing_tracks", []):
            artist = track.get("artist", "")
            title = track.get("title", "")
            key = core.normalize_aggressive(f"{artist} {title}")
            if not key:
                continue
            row = by_key.setdefault(
                key, {"artist": artist, "title": title, "album": "", "shows": 0})
            row["shows"] += 1
            if not row["album"] and track.get("album"):
                row["album"] = track["album"]
    # Group the deduped tracks by artist, artists A→Z, titles A→Z within.
    by_artist = {}
    for row in by_key.values():
        by_artist.setdefault(row["artist"], []).append(row)
    groups = [
        {"artist": artist or "Unknown",
         "tracks": sorted(by_artist[artist], key=lambda r: r["title"].lower())}
        for artist in sorted(by_artist, key=lambda a: a.lower())
    ]
    return render_template("buylist.html", groups=groups, total=len(by_key))


@app.get("/attended")
def attended():
    """Show the username form, prefilled from SETLISTFM_USER if set."""
    try:
        core.load_config()
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    return render_template("attended.html",
                           username=os.environ.get("SETLISTFM_USER", ""),
                           ytm=ytm.load_ytm_config())


@app.post("/attended")
def attended_load():
    """List a setlist.fm user's attended shows, marking ones already created."""
    username = (request.form.get("username") or "").strip()
    if not username:
        return render_template("attended.html", username="",
                               ytm=ytm.load_ytm_config(),
                               error="Enter a setlist.fm username.")
    try:
        config = core.load_config()
        shows = core.fetch_attended(username, config["api_key"])
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except LookupError as exc:           # unknown / private user
        return render_template("attended.html", username=username,
                               ytm=ytm.load_ytm_config(), error=str(exc))
    except (PermissionError, ConnectionError) as exc:
        return _error("setlist.fm problem", str(exc))
    except Exception as exc:             # any other API hiccup
        return _error("setlist.fm problem", f"Could not load attended shows: {exc}")

    # Attach each show's prior history entry (if any) so the row can offer
    # Re-open/Update instead of Preview, like the History page does.
    seen = core.load_history(core.history_path())
    for show in shows:
        show["prior"] = seen.get(show.get("id"))
    return render_template("attended.html", username=username, shows=shows,
                           ytm=ytm.load_ytm_config())


# ---------------------------------------------------------------------------
# Connect YouTube Music (one-click, from a logged-in browser)
# ---------------------------------------------------------------------------

@app.get("/ytm/connect")
def ytm_connect():
    """Show browser sources with a YouTube session, labeled by account."""
    if not ytm.browser_connect_available():
        return _error("Can't auto-connect",
                      "Install browser_cookie3 (pip install browser_cookie3) to "
                      "connect YouTube Music from your browser.")
    sources = ytm.list_ytm_sources()
    for s in sources:                       # annotate with the account name
        s["account"] = ytm.ytm_source_account(s["id"])
    return render_template("ytm_connect.html", sources=sources)


@app.post("/ytm/connect")
def ytm_connect_save():
    """Write the auth file from the chosen browser source."""
    source_id = (request.form.get("source") or "").strip()
    if not source_id:
        return _error("No source chosen", "Pick a browser to connect from.", 400)
    try:
        ytm.connect_ytm_source(source_id)
    except ytm.YTMError as exc:
        return _error("Couldn't connect YouTube Music", str(exc))
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Playlist builder
# ---------------------------------------------------------------------------

@app.post("/builder/seed")
def builder_seed():
    """Create a draft — seeded from a setlist, or empty — and open it."""
    service = "ytm" if request.form.get("service") == "ytm" else "plex"
    setlist_arg = (request.form.get("setlist") or "").strip()
    name = (request.form.get("name") or "").strip() or None
    prefer_album = request.form.get("prefer_album")

    if service == "ytm" and not ytm.load_ytm_config()["available"]:
        return _error("YouTube Music unavailable",
                      ytm.load_ytm_config()["reason"])
    try:
        config = _web_config()
        if setlist_arg:
            draft = bld.seed_from_setlist(config, service, setlist_arg,
                                          name, prefer_album)
        else:
            draft = bld.empty_draft(service, name or "")
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except ValueError as exc:
        return _error("Couldn't read that setlist", str(exc), 400)
    except core.SetlistError as exc:
        return _error("Setlist problem", str(exc))
    except (core.PlexError, ytm.YTMError) as exc:
        return _error("Music service problem", str(exc))

    _persist(draft)
    return redirect(url_for("builder_open", draft_id=draft["id"]))


@app.get("/builder/<draft_id>")
def builder_open(draft_id):
    """Render the builder for a draft."""
    draft = _load_draft(draft_id)
    if draft is None:
        return _error("Draft not found",
                      "That draft is gone (already saved, or discarded). "
                      "Start a new one.")
    return render_template("builder.html", draft=draft,
                           service_label=_SERVICE_LABEL[draft["service"]])


@app.get("/builder/<draft_id>/search")
def builder_search(draft_id):
    """Search the draft's service catalog; return result-row fragments."""
    draft = _load_draft(draft_id)
    if draft is None:
        return "", 404
    q = (request.args.get("q") or "").strip()
    if not q:
        return ""   # clear the results area
    try:
        results = bld.search(_web_config(), draft["service"], q)
    except core.ConfigError as exc:
        return f'<p class="hint">{exc}</p>', 200
    except (PermissionError, ConnectionError, LookupError,
            core.PlexError, ytm.YTMError) as exc:
        return f'<p class="hint">Search failed: {exc}</p>', 200
    except Exception as exc:  # any other backend hiccup
        return f'<p class="hint">Search failed: {exc}</p>', 200
    return render_template("_search_results.html", results=results, draft=draft)


@app.post("/builder/<draft_id>/add")
def builder_add(draft_id):
    """Append a chosen search result to the draft."""
    draft = _load_draft(draft_id)
    if draft is None:
        return "", 404
    bld.add_track(draft, {
        "track_id": request.form.get("track_id", ""),
        "title": request.form.get("title", ""),
        "artist": request.form.get("artist", ""),
        "album": request.form.get("album", ""),
    })
    _persist(draft)
    return _tracklist(draft)


@app.post("/builder/<draft_id>/remove")
def builder_remove(draft_id):
    """Remove a track by its 0-based index."""
    draft = _load_draft(draft_id)
    if draft is None:
        return "", 404
    try:
        index = int(request.form.get("index", ""))
    except ValueError:
        index = -1
    bld.remove_track(draft, index)
    _persist(draft)
    return _tracklist(draft)


@app.post("/builder/<draft_id>/reorder")
def builder_reorder(draft_id):
    """Reorder tracks to the comma-separated permutation of indices."""
    draft = _load_draft(draft_id)
    if draft is None:
        return "", 404
    raw = request.form.get("order", "")
    try:
        order = [int(i) for i in raw.split(",") if i != ""]
    except ValueError:
        order = []
    bld.reorder_tracks(draft, order)
    _persist(draft)
    return _tracklist(draft)


@app.post("/builder/<draft_id>/save")
def builder_save(draft_id):
    """Materialize the draft into a playlist on its service, then delete it."""
    draft = _load_draft(draft_id)
    if draft is None:
        return _error("Draft not found", "That draft is gone. Start a new one.")
    name = (request.form.get("name") or "").strip()
    if name and name != draft["name"]:
        draft["name"] = name
        _persist(draft)   # keep the rename even if materialize below fails
    if not draft["tracks"]:
        return _error("Nothing to save", "Add at least one track first.", 400)
    try:
        final_name = bld.materialize(_web_config(), draft)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except (core.PlexError, ytm.YTMError) as exc:
        return _error("Music service problem", str(exc))

    _delete_draft(draft_id)
    missing = (draft.get("seed") or {}).get("missing_tracks", [])
    return render_template("created.html", name=final_name,
                           added=len(draft["tracks"]), missing=missing,
                           service_label=_SERVICE_LABEL[draft["service"]])


@app.post("/builder/<draft_id>/discard")
def builder_discard(draft_id):
    """Throw a draft away."""
    _delete_draft(draft_id)
    return redirect(url_for("index"))


_SERVICE_LABEL = {"plex": "Plex", "ytm": "YouTube Music"}


# ---------------------------------------------------------------------------
# Update an existing (Plex) playlist — add-only top-up, unchanged flow
# ---------------------------------------------------------------------------

@app.post("/update-preview")
def update_preview():
    """Re-match a past show and show which now-available tracks are NOT yet in
    its existing playlist (the add-only diff), for confirmation."""
    setlist_arg = (request.form.get("setlist") or "").strip()
    playlist_key = (request.form.get("playlist_rating_key") or "").strip()
    pl_name = (request.form.get("name") or "").strip()
    if not setlist_arg:
        return _error("Missing input", "No setlist to update from.", 400)

    try:
        config = core.load_config()
        setlist_id = core.parse_setlist_id(setlist_arg)
        result = core.gather_matches(config, setlist_id, None)
        plex = core.connect_plex(config["plex_baseurl"], config["plex_token"])
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except ValueError as exc:
        return _error("Couldn't read that setlist", str(exc), 400)
    except core.SetlistError as exc:
        return _error("Setlist problem", str(exc))
    except (PermissionError, ConnectionError, core.PlexError) as exc:
        return _error("Plex problem", str(exc))

    playlist = core.find_playlist(plex, rating_key=playlist_key or None,
                                  name=pl_name or None)
    if playlist is None:
        return _error("Playlist not found",
                      "That playlist no longer exists in Plex. Use Re-open to "
                      "create a fresh one.")

    existing = set()
    try:
        for item in playlist.items():
            key = getattr(item, "ratingKey", None)
            if key is not None:
                existing.add(str(key))
    except Exception:
        pass

    # New = matched songs with no candidate already in the playlist (any version).
    new_songs = []
    for song in result["matched"]:
        cand_keys = {str(c.get("rating_key")) for c in song.get("candidates", [])}
        if cand_keys & existing:
            continue
        new_songs.append(song)

    return render_template(
        "update.html", result=result, playlist=playlist, new_songs=new_songs,
        playlist_rating_key=getattr(playlist, "ratingKey", "") or "",
        missing_json=json.dumps(result["missing"]))


@app.post("/update")
def update():
    """Add the chosen new tracks to the existing playlist (add-only)."""
    name = (request.form.get("name") or "").strip()
    playlist_key = (request.form.get("playlist_rating_key") or "").strip()
    setlist_id = (request.form.get("setlist_id") or "").strip()
    rating_keys = _picked_rating_keys()
    if not rating_keys:
        return _error("Nothing selected", "No tracks chosen to add.", 400)

    try:
        config = core.load_config()
        title, added = core.add_to_playlist(
            config, playlist_key or None, name, rating_keys,
            _history_meta_from_form(setlist_id))
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))

    return render_template("updated.html", name=title, added=added)


def _picked_rating_keys():
    """Collect included rows' pick_<position> values, in setlist order."""
    included = {int(p) for p in request.form.getlist("include") if p.isdigit()}
    keys = []
    for pos in sorted(included):
        key = request.form.get(f"pick_{pos}")
        if key:
            keys.append(key)
    return keys


def _history_meta_from_form(setlist_id):
    """Build history_meta (incl. missing_tracks) from the hidden form fields."""
    try:
        missing = json.loads(request.form.get("missing_json") or "[]")
    except ValueError:
        missing = []
    try:
        song_count = int(request.form.get("song_count") or 0)
    except ValueError:
        song_count = 0
    return {
        "id": setlist_id,
        "url": request.form.get("url", ""),
        "artist": request.form.get("artist", ""),
        "date": request.form.get("date", ""),
        "song_count": song_count,
        "missing": len(missing),
        # missing rows are [position, artist, title, album?]; carry position+album.
        "missing_tracks": [
            {"position": row[0], "artist": row[1], "title": row[2],
             "album": (row[3] if len(row) > 3 else "")}
            for row in missing if len(row) >= 3],
    }


def _port():
    """Web server port: PORT env/.env, else 5001 (5000 is AirPlay on macOS)."""
    return int(os.environ.get("PORT", "5001"))


def main():
    """Entry point for the `setlisterator-web` console script."""
    core.load_dotenv()   # pick up PORT (and the rest) from .env at startup
    app.run(host="127.0.0.1", port=_port(), debug=False)


if __name__ == "__main__":
    main()

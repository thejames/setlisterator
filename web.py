#!/usr/bin/env python3
"""Local web interface for setlist_to_plex.

Run:
    ./.venv/bin/python web.py        # serves http://127.0.0.1:5001
    PORT=8080 ./.venv/bin/python web.py   # or pick your own port

Default port is 5001 (5000 is taken by AirPlay Receiver on macOS); override
with the PORT environment variable.

This app talks to your LOCAL Plex server and carries your Plex token, so it
binds to 127.0.0.1 only and has no authentication. Do not expose it to a
network. It reuses the core pipeline from setlist_to_plex.py:

    gather_matches()  -> preview (read-only; no playlist is created)
    create_playlist() -> commit the previewed tracks to a Plex playlist
"""

import json
import os
import uuid

from flask import Flask, jsonify, redirect, render_template, request, url_for

import setlist_to_plex as core

app = Flask(__name__)


def _error(title, message, status=200):
    return render_template("error.html", title=title, message=message), status


def _stats(result):
    """Counts for the preview stat chips (exclusive: sum == total)."""
    matched = result["matched"]
    multi = sum(1 for m in matched if len(m.get("candidates", [])) > 1)
    single = [m for m in matched if len(m.get("candidates", [])) <= 1]
    return {
        "total": len(result["songs"]),
        "exact": sum(1 for m in single if m.get("quality") == "exact"),
        "fuzzy": sum(1 for m in single if m.get("quality") == "fuzzy"),
        "multi": multi,
        "missing": len(result["missing"]),
    }


@app.get("/")
def index():
    try:
        core.load_config()
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    return render_template("index.html")


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
                           username=os.environ.get("SETLISTFM_USER", ""))


@app.post("/attended")
def attended_load():
    """List a setlist.fm user's attended shows, marking ones already created."""
    username = (request.form.get("username") or "").strip()
    if not username:
        return render_template("attended.html", username="",
                               error="Enter a setlist.fm username.")
    try:
        config = core.load_config()
        shows = core.fetch_attended(username, config["api_key"])
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except LookupError as exc:           # unknown / private user
        return render_template("attended.html", username=username, error=str(exc))
    except (PermissionError, ConnectionError) as exc:
        return _error("setlist.fm problem", str(exc))
    except Exception as exc:             # any other API hiccup
        return _error("setlist.fm problem", f"Could not load attended shows: {exc}")

    # Attach each show's prior history entry (if any) so the row can offer
    # Re-open/Update instead of Preview, like the History page does.
    seen = core.load_history(core.history_path())
    for show in shows:
        show["prior"] = seen.get(show.get("id"))
    return render_template("attended.html", username=username, shows=shows)


@app.get("/search")
def search():
    """Search the Plex music library by title; returns JSON for the override UI."""
    q = (request.args.get("q") or "").strip()
    include_albums = bool(request.args.get("albums"))
    if not q:
        return jsonify(results=[])
    try:
        config = core.load_config()
    except core.ConfigError as exc:
        return jsonify(error=str(exc)), 400
    try:
        plex = core.connect_plex(config["plex_baseurl"], config["plex_token"])
        section = core.get_music_section(plex, config["music_library"])
        tracks = section.searchTracks(title=q, maxresults=25)
        albums = section.searchAlbums(title=q, maxresults=10) if include_albums else []
    except (PermissionError, ConnectionError, LookupError) as exc:
        return jsonify(error=str(exc)), 502
    except Exception as exc:  # any other Plex hiccup
        return jsonify(error=f"Search failed: {exc}"), 502

    # Albums first (they're the higher-level match), then individual tracks.
    results = [{
        "type": "album",
        "rating_key": getattr(a, "ratingKey", None),
        "title": a.title,
        "artist": getattr(a, "parentTitle", "") or "",
        "count": getattr(a, "leafCount", None),
    } for a in albums]
    results += [{
        "type": "track",
        "rating_key": getattr(t, "ratingKey", None),
        "title": t.title,
        "artist": core._track_artist_name(t),
        "album": core._track_album(t),
    } for t in tracks]
    return jsonify(results=results)


@app.get("/album/<rating_key>")
def album_tracks(rating_key):
    """An album's tracks in order (JSON) — backs 'add whole album' on the picker."""
    try:
        config = core.load_config()
        tracks = core.get_album_tracks(config, rating_key)
    except core.ConfigError as exc:
        return jsonify(error=str(exc)), 400
    except core.PlexError as exc:
        return jsonify(error=str(exc)), 502
    return jsonify(tracks=tracks)


@app.post("/preview")
def preview():
    """Match the setlist and show the result without creating anything."""
    setlist_arg = (request.form.get("setlist") or "").strip()
    name = (request.form.get("name") or "").strip() or None
    # Absent (first preview) -> None -> auto-detect a cohesive album; present
    # (incl. "" for "No preference") -> use it verbatim.
    prefer_album = request.form.get("prefer_album")
    if not setlist_arg:
        return _error("Missing input", "Enter a setlist.fm URL or ID.", 400)

    try:
        config = core.load_config()
        setlist_id = core.parse_setlist_id(setlist_arg)
        result = core.gather_matches(config, setlist_id, name, prefer_album)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except ValueError as exc:
        return _error("Couldn't read that setlist", str(exc), 400)
    except core.SetlistError as exc:
        return _error("Setlist problem", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))

    prior = core.load_history(core.history_path()).get(result["setlist_id"])
    return render_template(
        "preview.html", result=result, prior=prior, stats=_stats(result),
        missing_json=json.dumps(result["missing"]),
        fuzzy_json=json.dumps(result["fuzzy"]))


@app.post("/create")
def create():
    """Build the Plex playlist from the previewed (matched) rating keys."""
    name = (request.form.get("name") or "").strip()
    setlist_id = (request.form.get("setlist_id") or "").strip()
    rating_keys = _picked_rating_keys()
    if not name:
        return _error("Missing name", "A playlist name is required.", 400)
    if not rating_keys:
        return _error("Nothing to create", "No matched tracks to add.", 400)

    history_meta = _history_meta_from_form(setlist_id)
    try:
        config = core.load_config()
        final_name = core.create_playlist(config, name, rating_keys, history_meta)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))

    # Dedupe to match what create_playlist actually adds (e.g. a medley track
    # chosen for two songs lands in the playlist once).
    added = len(dict.fromkeys(rating_keys))
    return render_template("created.html", name=final_name, added=added,
                           missing=history_meta["missing_tracks"])


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


@app.get("/playlist/delete")
def playlist_delete_confirm():
    """Confirmation page before permanently deleting a playlist."""
    playlist_id = (request.args.get("id") or "").strip()
    if not playlist_id:
        return _error("Nothing to delete", "No playlist was specified.", 400)
    back = _delete_back(request.args.get("back"))
    return render_template("confirm_delete.html", playlist_id=playlist_id,
                           name=request.args.get("name", ""), back=back,
                           view=_back_view(back, request.args.get("view")))


@app.post("/playlist/delete")
def playlist_delete():
    """Delete the Plex playlist (and drop its history entry), then return to the
    page the delete was launched from (History by default, Playlists otherwise),
    restoring the Playlists view filter if one was active."""
    playlist_id = (request.form.get("id") or "").strip()
    back = _delete_back(request.form.get("back"))
    if not playlist_id:
        return _error("Nothing to delete", "No playlist was specified.", 400)
    try:
        config = core.load_config()
        core.delete_playlist(config, playlist_id)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Couldn't delete playlist", str(exc))
    return redirect(url_for(back, view=_back_view(back, request.form.get("view"))))


def _delete_back(value):
    """Whitelist the post-delete redirect target (avoids url_for on junk)."""
    return value if value in ("history", "playlists") else "history"


def _playlists_view(value):
    """Whitelist the Playlists page filter (?view=): concerts (setlist-made),
    built (Build page), other (not ours), or all."""
    return value if value in ("concerts", "built", "other") else "all"


def _back_view(back, value):
    """Playlists filter to restore after a delete round-trip; None (i.e. no
    query param) unless returning to Playlists with a narrowed view."""
    view = _playlists_view(value)
    return view if back == "playlists" and view != "all" else None


@app.get("/build")
def build_page():
    """Manual playlist builder: search the library and pick tracks by hand."""
    return render_template("build.html")


@app.post("/build")
def build_create():
    """Create a Plex playlist from hand-picked rating keys.

    A setlist-free path (no matching). Recorded to History under a namespaced
    ``manual:<uuid>`` id with source="manual" so it's badged as app-created on
    the Playlists page; the manual source keeps the setlist-shaped Plex summary
    off it.
    """
    name = (request.form.get("name") or "").strip()
    rating_keys = [k for k in request.form.getlist("rating_keys") if k.strip()]
    if not name:
        return _error("Missing name", "A playlist name is required.", 400)
    if not rating_keys:
        return _error("Nothing to create", "Add at least one track.", 400)
    history_meta = {"id": "manual:" + uuid.uuid4().hex, "source": "manual",
                    "artist": "", "date": "", "url": ""}
    try:
        config = core.load_config()
        final_name = core.create_playlist(config, name, rating_keys, history_meta)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))

    added = len(dict.fromkeys(rating_keys))
    return render_template("created.html", name=final_name, added=added, missing=[])


@app.get("/playlists")
def playlists():
    """List the Plex audio playlists, badging the ones this app created.

    ``?view=concerts|built|other`` narrows the list to setlist-derived,
    Build-page, or non-app playlists; absent (or junk) shows everything.
    Counts for every view are passed so the filter chips can show what each
    hides.
    """
    view = _playlists_view(request.args.get("view"))
    try:
        config = core.load_config()
        rows = core.list_playlists(config)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))
    counts = {"all": len(rows),
              "concerts": sum(1 for r in rows if r["source"] == "setlist"),
              "built": sum(1 for r in rows if r["source"] == "manual"),
              "other": sum(1 for r in rows if not r["app_created"])}
    if view == "concerts":
        rows = [r for r in rows if r["source"] == "setlist"]
    elif view == "built":
        rows = [r for r in rows if r["source"] == "manual"]
    elif view == "other":
        rows = [r for r in rows if not r["app_created"]]
    return render_template("playlists.html", playlists=rows, view=view,
                           counts=counts)


@app.get("/playlists/<rating_key>/edit")
def playlist_edit(rating_key):
    """Open the in-place editor for an existing playlist (tracks preloaded)."""
    try:
        config = core.load_config()
        pl = core.get_playlist_tracks(config, rating_key)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))
    initial = [t for t in pl["tracks"] if t["rating_key"] is not None]
    return render_template("playlist_edit.html", playlist=pl, initial=initial)


@app.post("/playlists/<rating_key>/edit")
def playlist_save(rating_key):
    """Apply the editor's changes (rename + exact ordered membership) to Plex."""
    name = (request.form.get("name") or "").strip()
    rating_keys = [k for k in request.form.getlist("rating_keys") if k.strip()]
    if not name:
        return _error("Missing name", "A playlist name is required.", 400)
    if not rating_keys:
        return _error("Nothing to save", "A playlist needs at least one track.", 400)
    try:
        config = core.load_config()
        core.set_playlist(config, rating_key, name, rating_keys)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))
    return redirect(url_for("playlists"))


def _port():
    """Web server port: PORT env/.env, else 5001 (5000 is AirPlay on macOS)."""
    return int(os.environ.get("PORT", "5001"))


def main():
    """Entry point for the `setlisterator-web` console script."""
    core.load_dotenv()   # pick up PORT (and the rest) from .env at startup
    app.run(host="127.0.0.1", port=_port(), debug=False)


if __name__ == "__main__":
    main()

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

from flask import Flask, jsonify, render_template, request

import setlist_to_plex as core

app = Flask(__name__)


def _error(title, message, status=200):
    return render_template("error.html", title=title, message=message), status


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
    # No config needed — this only reads the local history file.
    by_key = {}
    for entry in core.load_history(core.history_path()).values():
        for track in entry.get("missing_tracks", []):
            artist = track.get("artist", "")
            title = track.get("title", "")
            key = core.normalize_aggressive(f"{artist} {title}")
            if not key:
                continue
            row = by_key.setdefault(
                key, {"artist": artist, "title": title, "shows": 0})
            row["shows"] += 1
    items = sorted(by_key.values(),
                   key=lambda r: (-r["shows"], r["artist"].lower(),
                                  r["title"].lower()))
    return render_template("buylist.html", items=items)


@app.get("/search")
def search():
    """Search the Plex music library by title; returns JSON for the override UI."""
    q = (request.args.get("q") or "").strip()
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
    except (PermissionError, ConnectionError, LookupError) as exc:
        return jsonify(error=str(exc)), 502
    except Exception as exc:  # any other Plex hiccup
        return jsonify(error=f"Search failed: {exc}"), 502

    results = [{
        "rating_key": getattr(t, "ratingKey", None),
        "title": t.title,
        "artist": core._track_artist_name(t),
        "album": core._track_album(t),
    } for t in tracks]
    return jsonify(results=results)


@app.post("/preview")
def preview():
    """Match the setlist and show the result without creating anything."""
    setlist_arg = (request.form.get("setlist") or "").strip()
    name = (request.form.get("name") or "").strip() or None
    if not setlist_arg:
        return _error("Missing input", "Enter a setlist.fm URL or ID.", 400)

    try:
        config = core.load_config()
        setlist_id = core.parse_setlist_id(setlist_arg)
        result = core.gather_matches(config, setlist_id, name)
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
        "preview.html", result=result, prior=prior,
        missing_json=json.dumps(result["missing"]),
        fuzzy_json=json.dumps(result["fuzzy"]))


@app.post("/create")
def create():
    """Build the Plex playlist from the previewed (matched) rating keys."""
    name = (request.form.get("name") or "").strip()
    setlist_id = (request.form.get("setlist_id") or "").strip()
    # Each included row contributes pick_<position>; collect them in setlist
    # (position) order. Unchecked rows are simply absent from "include".
    included = {int(p) for p in request.form.getlist("include") if p.isdigit()}
    rating_keys = []
    for pos in sorted(included):
        key = request.form.get(f"pick_{pos}")
        if key:
            rating_keys.append(key)
    if not name:
        return _error("Missing name", "A playlist name is required.", 400)
    if not rating_keys:
        return _error("Nothing to create", "No matched tracks to add.", 400)

    try:
        missing = json.loads(request.form.get("missing_json") or "[]")
    except ValueError:
        missing = []

    history_meta = {
        "id": setlist_id,
        "url": request.form.get("url", ""),
        "artist": request.form.get("artist", ""),
        "date": request.form.get("date", ""),
        "missing": len(missing),
        # missing rows are [position, artist, title]; store the names.
        "missing_tracks": [{"artist": row[1], "title": row[2]}
                           for row in missing if len(row) >= 3],
    }
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
    return render_template("created.html", name=final_name,
                           added=added, missing=missing)


def _port():
    """Web server port: PORT env/.env, else 5001 (5000 is AirPlay on macOS)."""
    return int(os.environ.get("PORT", "5001"))


def main():
    """Entry point for the `setlisterator-web` console script."""
    core.load_dotenv()   # pick up PORT (and the rest) from .env at startup
    app.run(host="127.0.0.1", port=_port(), debug=False)


if __name__ == "__main__":
    main()

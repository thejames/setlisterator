#!/usr/bin/env python3
"""Local web interface for setlist_to_plex.

Run:
    ./.venv/bin/python web.py        # serves http://127.0.0.1:5000
    # or:  flask --app web run

This app talks to your LOCAL Plex server and carries your Plex token, so it
binds to 127.0.0.1 only and has no authentication. Do not expose it to a
network. It reuses the core pipeline from setlist_to_plex.py:

    gather_matches()  -> preview (read-only; no playlist is created)
    create_playlist() -> commit the previewed tracks to a Plex playlist
"""

import json

from flask import Flask, render_template, request

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
    # One "pick" value per matched song, in setlist order (the chosen track).
    rating_keys = [k for k in request.form.getlist("pick") if k]
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

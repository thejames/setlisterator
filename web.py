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
import tempfile
import uuid

import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, stream_with_context, url_for)

import setlist_to_plex as core

app = Flask(__name__)


def _error(title, message, status=200):
    return render_template("error.html", title=title, message=message), status


@app.context_processor
def _inject_poster_policy():
    """Expose core's poster policy to every template (single source), so the
    file input's accept + size hint and the JS pre-check all derive from it."""
    return {"poster_policy": {"accept": core.POSTER_ACCEPT,
                              "max_bytes": core.POSTER_MAX_BYTES,
                              "max_label": core.POSTER_MAX_LABEL}}


@app.context_processor
def _inject_plex_baseurl():
    """Expose the Plex base URL to templates so the client can probe whether it
    can reach Plex itself (ADR-0004). Not a secret — the token is not included.
    Deliberately swallows a config error: a broken .env should fail on the page
    the user asked for, not on every render."""
    try:
        config = core.load_config()
        return {"plex_baseurl": config["plex_baseurl"],
                "audition_transcode_default": config["audition_always_transcode"]}
    except Exception:
        return {"plex_baseurl": "",
                "audition_transcode_default": core.AUDITION_TRANSCODE_DEFAULT}


# Poster upload: an optional browser image saved to a temp file for the core
# uploader. Best-effort (ADR 0002) — an unusable file never blocks a save; we
# skip it and warn. Deliberately NOT capped via Flask's MAX_CONTENT_LENGTH,
# which would 413 the whole POST and lose the form (the preview session). The
# accept/reject policy (types, size) lives in core.check_poster.
def _take_poster_upload(req):
    """Pull an optional ``poster`` file from a request and stash it to a temp
    file for core.set_playlist_poster.

    Returns ``(poster_path, prewarn)``: ``poster_path`` is a temp file the
    caller must delete when a valid image was uploaded, else None; ``prewarn``
    is a short reason when a file was supplied but rejected (unsupported type or
    too large), else None. No file at all -> ``(None, None)``.
    """
    file = req.files.get("poster")
    if file is None or not file.filename:
        return None, None
    data = file.read()
    if not data:
        return None, None
    ext, reason = core.check_poster(file.mimetype or "", len(data))
    if reason:
        return None, reason
    fd, path = tempfile.mkstemp(prefix="setlist_poster_", suffix=ext)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path, None


# Shared actionable clause for every "your image didn't take" message, so the
# create page and the editor notice can't drift apart. Only the lead verb
# differs by path ("created" vs "saved").
_POSTER_FAIL_TAIL = "the image couldn’t be set — you can add it in Plex."


def _poster_warned(prewarn, poster_status):
    """Whether to warn the user that their image didn't take: either it was
    rejected before upload, or the poster upload itself failed."""
    return prewarn is not None or poster_status is False


def _poster_warning_msg(prewarn, poster_status, lead):
    """The create-path warning message, or None when the image took fine.
    ``lead`` is the path-specific opener (e.g. "The playlist was created")."""
    if _poster_warned(prewarn, poster_status):
        return f"{lead}, but {_POSTER_FAIL_TAIL}"
    return None


def _cleanup_poster(poster_path):
    """Remove a temp poster file, ignoring a missing/locked file."""
    if poster_path:
        try:
            os.remove(poster_path)
        except OSError:
            pass


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


@app.get("/missing")
def missing():
    """Aggregate every show's missing tracks into one deduped list.

    "Missing" is about your *library*, not your ownership — a gap here may be
    a record you own on vinyl. See the glossary; the list deliberately doesn't
    tell you to buy anything."""
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
    return render_template("missing.html", groups=groups, total=len(by_key))


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
        "rating": getattr(t, "userRating", None),   # Plex stars, 0–10 or None
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


# --- auditioning (ADR-0004) --------------------------------------------------
# Two routes: one hands the client what it needs to play a track, the other is
# the fallback pipe for when the browser can't reach Plex itself. Best-effort
# throughout — a failure here is reported and never touches a selection.

_AUDITION_CHUNK = 64 * 1024

# Headers worth forwarding from Plex. Content-Range/Accept-Ranges are what make
# the scrubber seekable, so they must survive the hop.
_AUDITION_PASSTHRU = ("content-type", "content-length",
                      "accept-ranges", "content-range")


def _audition_args(config):
    """The two per-request knobs: quality preference and seek position.

    `transcode` is the UI setting, sent by the client because it is stored per
    browser; absent, the server's configured default applies. `offset` is only
    meaningful for a transcode, which has no byte ranges to seek with.
    """
    raw = request.args.get("transcode")
    force = config["audition_always_transcode"] if raw is None \
        else core.truthy(raw)
    try:
        offset = max(0, int(float(request.args.get("offset") or 0)))
    except ValueError:
        offset = 0
    return force, offset


@app.get("/audition/<rating_key>")
def audition(rating_key):
    """What the client needs to audition one track (JSON).

    Returns core's source data plus `stream_path`, the proxy URL — composed
    here because routes belong to the web layer, not to core. The client picks
    between `direct_url` and `stream_path` using its own reachability probe.
    """
    try:
        config = core.load_config()
        force, offset = _audition_args(config)
        src = core.audition_source(config, rating_key, force, offset)
    except core.ConfigError as exc:
        return jsonify(error=str(exc)), 400
    except core.PlexError as exc:
        return jsonify(error=str(exc)), 502
    # Carry the resolved preference into the proxy URL. Without it the stream
    # route would re-resolve from the server default and could serve a
    # transcode while this response advertised `mode: "direct"` — the client
    # would then seek it by byte range, which a transcode ignores, and play
    # start-of-track audio at the position asked for (ADR-0004).
    src["stream_path"] = url_for("audition_stream",
                                 rating_key=src["rating_key"],
                                 transcode="1" if force else "0")
    return jsonify(**src)


@app.get("/audition/<rating_key>/stream")
def audition_stream(rating_key):
    """Pipe a track's audio from Plex to the browser.

    For clients that can't reach Plex directly. The Range header is forwarded
    and Plex's response status and range headers are passed straight back, so
    seeking still works on direct (non-transcoded) audio.

    Errors are bare statuses, not HTML: an <audio> element is the only consumer
    and it can do nothing with an error page.
    """
    try:
        config = core.load_config()
        force, offset = _audition_args(config)
        src = core.audition_source(config, rating_key, force, offset)
    except core.ConfigError:
        return "", 400
    except core.PlexError:
        return "", 502

    headers = {}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    try:
        upstream = requests.get(src["direct_url"], headers=headers,
                                stream=True, timeout=20)
    except requests.exceptions.RequestException:
        return "", 502

    # The connection is held open for as long as the browser is playing, and
    # this app ships on one gunicorn worker with four threads — so the pipe has
    # to be closed, not merely dropped, whenever playback stops or the client
    # goes away. Hence the finally: GeneratorExit on disconnect lands there too.
    def _pump():
        try:
            for chunk in upstream.iter_content(_AUDITION_CHUNK):
                yield chunk
        except requests.exceptions.RequestException:
            # Plex went away mid-stream. The status line is long gone, so there
            # is no error to report — stop cleanly rather than raise out of a
            # half-sent response. The audition dies; nothing else notices.
            pass
        finally:
            upstream.close()

    passthru = {k: v for k, v in upstream.headers.items()
                if k.lower() in _AUDITION_PASSTHRU}
    return Response(stream_with_context(_pump()),
                    status=upstream.status_code, headers=passthru)


def _parse_and_match(req):
    """Shared read+match preamble for /preview and /rematch.

    Parses the form, runs `gather_matches`, and classifies any failure. Returns
    `(result, err)` where `err` is None on success or a `(kind, message)` pair —
    the caller renders whichever shape (HTML error page vs JSON) its route needs.
    `prefer_album` is passed through verbatim, and absent (first preview) means
    None — which core treats the same as the "" the "Earliest album" option
    submits: no preference, so each song defaults to its earliest album.
    """
    setlist_arg = (req.form.get("setlist") or "").strip()
    name = (req.form.get("name") or "").strip() or None
    prefer_album = req.form.get("prefer_album")
    if not setlist_arg:
        return None, ("input", "Enter a setlist.fm URL or ID.")
    try:
        config = core.load_config()
        setlist_id = core.parse_setlist_id(setlist_arg)
        return core.gather_matches(config, setlist_id, name, prefer_album), None
    except core.ConfigError as exc:
        return None, ("config", str(exc))
    except ValueError as exc:
        return None, ("value", str(exc))
    except core.SetlistError as exc:
        return None, ("setlist", str(exc))
    except core.PlexError as exc:
        return None, ("plex", str(exc))


@app.post("/preview")
def preview():
    """Match the setlist and show the result without creating anything."""
    result, err = _parse_and_match(request)
    if err:
        kind, msg = err
        titles = {"input": ("Missing input", 400),
                  "config": ("Configuration needed", 200),
                  "value": ("Couldn't read that setlist", 400),
                  "setlist": ("Setlist problem", 200),
                  "plex": ("Plex problem", 200)}
        title, status = titles[kind]
        return _error(title, msg, status)

    prior = core.load_history(core.history_path()).get(result["setlist_id"])
    return render_template(
        "preview.html", result=result, prior=prior, stats=_stats(result),
        missing_json=json.dumps(result["missing"]),
        songs_json=_songs_json(result),
        fuzzy_json=json.dumps(result["fuzzy"]))


@app.post("/rematch")
def rematch():
    """Re-match a setlist for a newly chosen preferred album, as JSON.

    Backs the preview page's in-place "Prefer album" switch: it returns the
    fresh per-song candidates so the client can re-order only the *untouched*
    rows, leaving the user's touched selections sticky (see
    docs/adr/0001-client-rematch-endpoint.md). This runs the same full match as
    /preview — the candidate *set* per song is invariant across album choices;
    only the ordering (hence the default pick) changes.
    """
    result, err = _parse_and_match(request)
    if err:
        kind, msg = err
        status = 400 if kind in ("input", "config", "value") else 502
        return jsonify(error=msg), status
    return jsonify(songs=result["songs"],
                   preferred_album=result.get("preferred_album", ""))


def _create_and_render(name, rating_keys, history_meta, missing):
    """Shared create path for /create and /build: take the optional poster
    upload (temp file + guaranteed cleanup), create the playlist, and render the
    result page — warning if the image didn't take. Returns a Flask response.
    """
    poster_path, prewarn = _take_poster_upload(request)
    try:
        config = core.load_config()
        result = core.create_playlist(config, name, rating_keys, history_meta,
                                      poster_path)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))
    finally:
        _cleanup_poster(poster_path)

    # Dedupe to match what create_playlist actually adds (e.g. a medley track
    # chosen for two songs lands in the playlist once).
    added = len(dict.fromkeys(rating_keys))
    return render_template(
        "created.html", name=result.name, added=added, missing=missing,
        poster_warning=_poster_warning_msg(prewarn, result.poster,
                                           "The playlist was created"))


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
    return _create_and_render(name, rating_keys, history_meta,
                              history_meta["missing_tracks"])


def _picked_rating_keys():
    """Collect included rows' pick_<position> values, in setlist order."""
    included = {int(p) for p in request.form.getlist("include") if p.isdigit()}
    keys = []
    for pos in sorted(included):
        key = request.form.get(f"pick_{pos}")
        if key:
            keys.append(key)
    return keys


def _songs_json(result):
    """core.song_map as compact rows, for the hidden field that carries it."""
    return json.dumps([[s["position"], s["title"], s["album"], s["rating_key"]]
                       for s in core.song_map(result)])


def _songs_from_form():
    """Read back the per-song map, applying the user's selections.

    A song the user re-pointed (version dropdown or row search) submits a
    ``pick_<position>`` that supersedes the key carried in ``songs_json``. Rows
    the current page didn't render keep theirs — the Update page only lists
    songs not yet in the playlist, so most rows come back untouched.
    """
    try:
        rows = json.loads(request.form.get("songs_json") or "[]")
    except ValueError:
        return []
    songs = []
    for row in rows:
        if len(row) < 2:
            continue
        position, title = row[0], row[1]
        picked = (request.form.get(f"pick_{position}") or "").strip()
        key = picked or (row[3] if len(row) > 3 else None)
        songs.append({
            "position": position,
            "title": title,
            "album": row[2] if len(row) > 2 else "",
            "rating_key": str(key) if key not in (None, "") else None,
        })
    return songs


def _history_meta_from_form(setlist_id):
    """Build history_meta (incl. missing_tracks) from the hidden form fields."""
    try:
        missing = json.loads(request.form.get("missing_json") or "[]")
    except ValueError:
        missing = []
    return {
        "id": setlist_id,
        "url": request.form.get("url", ""),
        "artist": request.form.get("artist", ""),
        "venue": request.form.get("venue", ""),
        "city": request.form.get("city", ""),
        "date": request.form.get("date", ""),
        "songs": _songs_from_form(),
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
        missing_json=json.dumps(result["missing"]),
        songs_json=_songs_json(result))


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
    return _create_and_render(name, rating_keys, history_meta, missing=[])


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
    # One-shot notice after an editor save whose poster upload didn't take.
    notice = (f"Playlist saved, but {_POSTER_FAIL_TAIL}"
              if request.args.get("notice") == "poster" else None)
    return render_template("playlists.html", playlists=rows, view=view,
                           counts=counts, notice=notice)


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
    poster_path, prewarn = _take_poster_upload(request)
    try:
        config = core.load_config()
        result = core.set_playlist(config, rating_key, name, rating_keys,
                                   poster_path)
    except core.ConfigError as exc:
        return _error("Configuration needed", str(exc))
    except core.PlexError as exc:
        return _error("Plex problem", str(exc))
    finally:
        _cleanup_poster(poster_path)

    # The editor redirects (no result page), so a poster hiccup surfaces as a
    # one-shot notice banner on the Playlists page rather than an inline block.
    if _poster_warned(prewarn, result.poster):
        return redirect(url_for("playlists", notice="poster"))
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

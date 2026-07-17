"""Playlist-builder business logic, shared by the web app.

A draft (see setlist_to_plex.new_draft) is an ordered, service-tagged list of
tracks that the user assembles — seeded from a setlist.fm show or started empty
— then materializes into a real playlist on the chosen service. This module owns
the transforms (seed / add / remove / reorder / search / materialize) so the web
layer stays a thin set of routes.
"""

from datetime import datetime

import setlist_to_plex as core
import ytm_service as ytm


def service_for(service_name):
    """The MusicService adapter for a draft's service tag."""
    return ytm.YTM_SERVICE if service_name == "ytm" else core.PLEX_SERVICE


def _touch(draft):
    draft["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _track_row(track_id, title="", artist="", album=""):
    """One draft track row, with a service-neutral string id."""
    return {"track_id": str(track_id), "title": title,
            "artist": artist, "album": album}


def _row_from_match(match):
    """Map a gather_matches `matched` entry to a draft track row."""
    return _track_row(match["rating_key"], match.get("track_title", ""),
                      match.get("track_artist", ""), match.get("album", ""))


def seed_from_setlist(config, service_name, setlist_arg, name=None,
                      prefer_album=None):
    """Build a draft seeded from a setlist.fm show (best match per song).

    Raises the same ValueError / SetlistError / PlexError as gather_matches.
    """
    setlist_id = core.parse_setlist_id(setlist_arg)
    result = core.gather_matches(config, setlist_id, name, prefer_album,
                                 service=service_for(service_name))
    seed = {
        "setlist_id": result["setlist_id"],
        "url": result["show"].get("url", ""),
        "artist": result["show"].get("artist", ""),
        "date": result["show"].get("date", ""),
        "song_count": len(result["songs"]),
        # missing rows are [position, artist, title, album?]; keep position+album.
        "missing_tracks": [
            {"position": row[0], "artist": row[1], "title": row[2],
             "album": (row[3] if len(row) > 3 else "")}
            for row in result["missing"] if len(row) >= 3],
    }
    draft = core.new_draft(service_name, name=result["playlist_name"], seed=seed)
    draft["tracks"] = [_row_from_match(m) for m in result["matched"]]
    return draft


def empty_draft(service_name, name=""):
    """A fresh from-scratch draft for the given service."""
    return core.new_draft(service_name, name=name.strip() or "New playlist")


def add_track(draft, track):
    """Append a chosen search result (a track dict) to the draft."""
    draft["tracks"].append(_track_row(
        track.get("track_id", ""), track.get("title", ""),
        track.get("artist", ""), track.get("album", "")))
    _touch(draft)


def remove_track(draft, index):
    """Remove the track at ``index`` (0-based); no-op if out of range."""
    if 0 <= index < len(draft["tracks"]):
        draft["tracks"].pop(index)
        _touch(draft)


def reorder_tracks(draft, order):
    """Reorder tracks to the given permutation of current indices.

    Ignores a malformed ``order`` (not a permutation of 0..n-1) so a stale or
    truncated client payload can't drop or duplicate tracks.
    """
    tracks = draft["tracks"]
    if sorted(order) != list(range(len(tracks))):
        return
    draft["tracks"] = [tracks[i] for i in order]
    _touch(draft)


def search(config, service_name, query, limit=25):
    """Search the service's catalog; return a list of track dicts.

    Uses the same section.searchTracks the matcher does, so one code path serves
    Plex (library) and YouTube Music (catalog) alike.
    """
    _client, section = service_for(service_name).connect(config)
    tracks = section.searchTracks(title=query, maxresults=limit)
    return [_track_row(getattr(t, "ratingKey", "") or "", t.title,
                       core._track_artist_name(t), core._track_album(t))
            for t in tracks]


def _history_meta(draft):
    """History metadata for a materialized draft.

    A setlist-seeded draft records the show (so Re-open/Buy-list keep working);
    a from-scratch draft records a light entry keyed by the draft id.
    """
    seed = draft.get("seed")
    if seed:
        return {
            "id": seed.get("setlist_id"),
            "url": seed.get("url", ""),
            "artist": seed.get("artist", ""),
            "date": seed.get("date", ""),
            "song_count": seed.get("song_count", 0),
            "missing": len(seed.get("missing_tracks", [])),
            "missing_tracks": seed.get("missing_tracks", []),
            "source": "setlist",
        }
    return {
        "id": f"builder-{draft['id']}",
        "url": "",
        "artist": "",
        "date": "",
        "song_count": len(draft["tracks"]),
        "missing": 0,
        "missing_tracks": [],
        "source": "builder",
    }


def materialize(config, draft):
    """Create the playlist on the draft's service; return the final name.

    Raises PlexError (Plex) or YTMError (YouTube Music) on failure.
    """
    track_ids = [t["track_id"] for t in draft["tracks"]]
    history_meta = _history_meta(draft)
    if draft["service"] == "ytm":
        return ytm.create_playlist_ytm(config, draft["name"], track_ids,
                                       history_meta)
    return core.create_playlist(config, draft["name"], track_ids, history_meta)

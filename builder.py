"""Playlist-builder business logic, shared by the web app.

A draft (see setlist_to_plex.new_draft) is an ordered list of tracks that the
user assembles — seeded from a setlist.fm show or started empty — then
materializes into a real Plex playlist. This module owns the transforms (seed /
add / remove / reorder / search / materialize) so the web layer stays a thin set
of routes.
"""

from datetime import datetime

import setlist_to_plex as core


def _touch(draft):
    draft["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _track_row(track_id, title="", artist="", album=""):
    """One draft track row, with a string id."""
    return {"track_id": str(track_id), "title": title,
            "artist": artist, "album": album}


def _row_from_match(match):
    """Map a gather_matches `matched` entry to a draft track row.

    Carries the match quality (``tier``/``source``/``quality``) so the preview
    can flag fuzzy matches. Manually-added rows (``_track_row``) omit these —
    a user-picked track needs no quality signal — and everything downstream
    (materialize, edits) only reads ``track_id``, so the extra keys are inert.
    """
    row = _track_row(match["rating_key"], match.get("track_title", ""),
                     match.get("track_artist", ""), match.get("album", ""))
    row["tier"] = match.get("tier", "")
    row["source"] = match.get("source", "")
    row["quality"] = match.get("quality", 0)
    row["position"] = match.get("position")   # setlist slot, for show-order preview
    return row


def seed_from_setlist(config, setlist_arg, name=None, prefer_album=None):
    """Build a draft seeded from a setlist.fm show (best match per song).

    Raises the same ValueError / SetlistError / PlexError as gather_matches.
    """
    setlist_id = core.parse_setlist_id(setlist_arg)
    result = core.gather_matches(config, setlist_id, name, prefer_album)
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
    draft = core.new_draft(name=result["playlist_name"], seed=seed)
    draft["tracks"] = [_row_from_match(m) for m in result["matched"]]
    return draft


def empty_draft(name=""):
    """A fresh from-scratch draft."""
    return core.new_draft(name=name.strip() or "New playlist")


def preview_stats(draft):
    """Match-quality counts for the preview screen.

    A row is *fuzzy* when it carries a tier other than ``exact`` (loose/medley/
    prefix); manually-added rows have no tier and count as neither.
    """
    tracks = draft["tracks"]
    return {
        "matched": len(tracks),
        "exact": sum(1 for t in tracks if t.get("tier") == "exact"),
        "fuzzy": sum(1 for t in tracks
                     if t.get("tier") not in ("exact", "", None)),
        "missing": len((draft.get("seed") or {}).get("missing_tracks", [])),
    }


def preview_rows(draft):
    """The setlist in play order — matched tracks and missing gaps interleaved.

    One row per setlist song, ordered by its position in the show, so a missing
    song shows where it was played rather than lumped after all the matches.
    Matched rows carry ``tier`` for a quality pill; missing rows set
    ``missing`` True. Rows without a position (shouldn't happen) sort last.
    """
    seed = draft.get("seed") or {}
    rows = [
        {"missing": False, "position": t.get("position"),
         "title": t.get("title"), "artist": t.get("artist"),
         "album": t.get("album"), "tier": t.get("tier", ""),
         "manual": t.get("manual", False)}
        for t in draft["tracks"]
    ] + [
        {"missing": True, "position": m.get("position"),
         "title": m.get("title"), "artist": m.get("artist"),
         "album": m.get("album", "")}
        for m in seed.get("missing_tracks", [])
    ]
    rows.sort(key=lambda r: (r["position"] is None, r["position"] or 0))
    return rows


def set_slot(draft, position, track):
    """Point a setlist slot at a chosen library track — fill a gap or replace a match.

    Replaces the row already at ``position`` if there is one, otherwise adds a
    new row there and clears the matching missing entry. Marked ``manual`` so the
    preview shows an "Added" pill rather than a match-quality tier.
    """
    row = _track_row(track.get("track_id", ""), track.get("title", ""),
                     track.get("artist", ""), track.get("album", ""))
    row["position"] = position
    row["manual"] = True
    for i, t in enumerate(draft["tracks"]):
        if t.get("position") == position:
            draft["tracks"][i] = row
            break
    else:
        draft["tracks"].append(row)
    _drop_missing(draft, position)
    _touch(draft)


def skip_slot(draft, position):
    """Drop a missing setlist song from the draft (and the buy-list record)."""
    _drop_missing(draft, position)
    _touch(draft)


def _drop_missing(draft, position):
    seed = draft.get("seed") or {}
    seed["missing_tracks"] = [m for m in seed.get("missing_tracks", [])
                              if m.get("position") != position]


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


def search(config, query, limit=25):
    """Search the Plex library; return a list of track dicts.

    Uses the same section.searchTracks the matcher does.
    """
    _client, section = core.connect_plex_section(config)
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
    """Create the playlist in Plex; return the final name.

    Raises PlexError on failure.
    """
    track_ids = [t["track_id"] for t in draft["tracks"]]
    history_meta = _history_meta(draft)
    return core.create_playlist(config, draft["name"], track_ids, history_meta)


# ---------------------------------------------------------------------------
# Editing an existing playlist: load it into a draft, then apply the diff on
# save.
# ---------------------------------------------------------------------------

def draft_from_playlist(config, playlist_id):
    """Build an edit-mode draft seeded from an existing playlist."""
    opened = core.open_playlist(config, playlist_id)
    draft = core.new_draft(name=opened["name"])
    draft["target_playlist_id"] = str(playlist_id)
    draft["tracks"] = [dict(t) for t in opened["tracks"]]
    return draft


def apply_edits(config, draft):
    """Apply an edit-mode draft's changes to its target playlist.

    Returns ``(final_name, stats)``. Raises if the draft isn't in edit mode.
    """
    playlist_id = draft.get("target_playlist_id")
    if not playlist_id:
        raise ValueError("draft is not editing an existing playlist")
    return core.apply_playlist_edits(config, playlist_id, draft["name"],
                                     draft["tracks"])


def delete_playlist(config, playlist_id):
    """Delete a playlist from Plex; returns its title."""
    return core.delete_playlist(config, playlist_id)

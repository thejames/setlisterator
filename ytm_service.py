"""YouTube Music backend for the playlist builder.

Presents YouTube Music through the same seam Plex uses: ``YTMService.connect``
hands ``gather_matches`` a ``(client, section)`` pair, where the section
duck-types plexapi's ``LibrarySection`` (``searchArtists``/``searchTracks``) and
the tracks it returns duck-type plexapi tracks (``.title``/``.grandparentTitle``/
``.parentTitle``/``.originalTitle``/``.ratingKey``). Because the matcher only
ever touches that surface, the entire two-tier scoped→global ranker in
``setlist_to_plex`` runs against YouTube Music unchanged.

``ytmusicapi`` is an optional, unofficial dependency; the import is soft so a
Plex-only install is unaffected and YouTube Music simply reports itself
unavailable.
"""

import os
from collections import namedtuple
from pathlib import Path

import setlist_to_plex as core

try:
    from ytmusicapi import YTMusic
except ImportError:                       # optional dependency
    YTMusic = None


class YTMError(Exception):
    """A YouTube Music connection, auth, or playlist-mutation failure."""


def ytm_oauth_path():
    """Path to the ytmusicapi OAuth file (override with YTM_OAUTH_FILE).

    Defaults beside history.json, sharing its config-dir/XDG conventions.
    """
    override = os.environ.get("YTM_OAUTH_FILE")
    if override:
        return Path(override)
    return core.history_path().with_name("ytm_oauth.json")


def load_ytm_config():
    """Describe YouTube Music availability without raising.

    Returns ``{available, oauth_path, reason}``. The web app offers YTM as a
    destination only when ``available`` is True, and shows ``reason`` otherwise.
    """
    path = ytm_oauth_path()
    if YTMusic is None:
        return {"available": False, "oauth_path": path,
                "reason": "ytmusicapi is not installed (pip install ytmusicapi)."}
    if not path.exists():
        return {"available": False, "oauth_path": path,
                "reason": f"No YouTube Music OAuth file at {path}. Run "
                          "`ytmusicapi oauth` and save the result there."}
    return {"available": True, "oauth_path": path, "reason": ""}


def connect_ytmusic(config):
    """Authenticate a YTMusic client from ``config['ytm_oauth_path']``."""
    if YTMusic is None:
        raise YTMError("ytmusicapi is not installed (pip install ytmusicapi).")
    try:
        return YTMusic(str(config["ytm_oauth_path"]))
    except Exception as exc:
        raise YTMError(
            f"Could not authenticate to YouTube Music: {exc}") from exc


# Duck-types a plexapi track for the matcher: the only attributes it reads are
# title (ranking), grandparentTitle/originalTitle (artist), parentTitle (album),
# and ratingKey (dedupe). ratingKey carries the YTM videoId.
_YTMTrack = namedtuple(
    "_YTMTrack", "title grandparentTitle parentTitle originalTitle ratingKey")


def _ytm_track_view(song):
    """Adapt one ytmusicapi song dict into a matcher-shaped track."""
    artists = song.get("artists") or []
    album = song.get("album") or {}
    return _YTMTrack(
        title=song.get("title", ""),
        grandparentTitle=(artists[0].get("name", "") if artists else ""),
        parentTitle=(album.get("name", "") if isinstance(album, dict) else ""),
        originalTitle=None,
        ratingKey=song.get("videoId"))


class YTMArtist:
    """Duck-types plexapi's Artist: ``.title`` plus ``.tracks()``.

    ``tracks()`` returns the artist's "Songs" shelf from ytmusicapi — a curated
    top-songs list, not the full catalog (no such endpoint exists), so deep cuts
    fall through to the matcher's global-search tier more often than on Plex.
    """

    def __init__(self, client, browse_id, title):
        self._client = client
        self._browse_id = browse_id
        self.title = title

    def tracks(self):
        try:
            data = self._client.get_artist(self._browse_id) or {}
        except Exception:
            return []
        songs = ((data.get("songs") or {}).get("results")) or []
        return [_ytm_track_view(s) for s in songs if s.get("videoId")]


class YTMSection:
    """Duck-types plexapi's LibrarySection: searchArtists + searchTracks."""

    def __init__(self, client):
        self._client = client

    def searchArtists(self, title=None, maxresults=10):
        try:
            hits = self._client.search(
                title, filter="artists", limit=maxresults) or []
        except Exception:
            return []
        return [YTMArtist(self._client, h["browseId"], h.get("artist", ""))
                for h in hits if h.get("browseId")]

    def searchTracks(self, title=None, maxresults=50):
        try:
            songs = self._client.search(
                title, filter="songs", limit=maxresults) or []
        except Exception:
            songs = []
        return [_ytm_track_view(s) for s in songs if s.get("videoId")]


class YTMService:
    """MusicService adapter for YouTube Music (mirrors PlexService)."""

    name = "ytm"

    def connect(self, config):
        client = connect_ytmusic(config)
        return client, YTMSection(client)


YTM_SERVICE = YTMService()


def create_playlist_ytm(config, name, video_ids, history_meta=None):
    """Create a YouTube Music playlist from an ordered list of video ids.

    Dedupes while preserving order, records builder history tagged "ytm", and
    returns the created playlist's name. Raises YTMError on failure.
    """
    client = connect_ytmusic(config)
    ids = list(dict.fromkeys(vid for vid in video_ids if vid))
    if not ids:
        raise YTMError(
            "None of the tracks could be added; playlist not created.")
    try:
        playlist_id = client.create_playlist(
            name, "Created by Setlist-er-ator.", video_ids=ids)
    except Exception as exc:
        raise YTMError(
            f"Failed to create YouTube Music playlist: {exc}") from exc
    core._record_history(name, playlist_id, len(ids), history_meta,
                         service="ytm")
    return name

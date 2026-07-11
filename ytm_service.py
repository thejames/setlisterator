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

import json
import os
import re
from collections import namedtuple
from pathlib import Path

import setlist_to_plex as core

# Header keys to drop from a browser-auth file. Copying request headers from
# Chrome pulls in HTTP/2 pseudo-headers, the request line, and a "decoded"
# annotation (which ytmusicapi mis-parses into bogus keys), plus request-only
# encoding headers that make YouTube return an empty body. Firefox is cleaner,
# but we sanitize either way so a Chrome paste still works.
_DROP_HEADERS = {"content-encoding", "accept-encoding", "content-length",
                 "priority", "x-client-data", "decoded",
                 "x-browser-channel", "x-browser-copyright",
                 "x-browser-validation", "x-browser-year"}
_HEADER_NAME = re.compile(r"[a-z0-9-]+")

try:
    import ytmusicapi as _ytmusicapi
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.oauth import OAuthCredentials
    from ytmusicapi.helpers import get_authorization
except ImportError:                       # optional dependency
    _ytmusicapi = None
    YTMusic = None
    OAuthCredentials = None
    get_authorization = None

try:
    import browser_cookie3 as _bc3       # optional: one-click connect from a browser
except ImportError:
    _bc3 = None

_YTM_ORIGIN = "https://music.youtube.com"


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


def ytm_client_creds():
    """(client_id, client_secret) for the Google OAuth client, from the env.

    Only needed for OAuth-style auth files, where ytmusicapi uses them at
    runtime (not just at `ytmusicapi oauth` time) to refresh the token — the
    OAuth file itself holds only tokens. Browser-style auth files don't use them.
    """
    return (os.environ.get("YTM_CLIENT_ID", ""),
            os.environ.get("YTM_CLIENT_SECRET", ""))


def _clean_browser_headers(headers):
    """Drop junk keys a browser paste can introduce, keeping real headers.

    Removes anything that isn't a valid header name (paths, hosts, ``:pseudo``
    headers) and the request-only/encoding headers in ``_DROP_HEADERS`` that
    otherwise yield an empty response.
    """
    out = {}
    for key, value in headers.items():
        name = key.strip().lower()
        if not _HEADER_NAME.fullmatch(name) or name in _DROP_HEADERS:
            continue
        out[key] = value
    return out


def _is_oauth_file(path):
    """True if the auth file is an OAuth token file (vs browser headers).

    OAuth token files carry a ``refresh_token``; browser-auth files are a bag of
    request headers (``cookie``/``authorization``/…). Malformed/missing → False.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return "refresh_token" in json.load(fh)
    except (OSError, ValueError):
        return False


def load_ytm_config():
    """Describe YouTube Music availability without raising.

    Returns ``{available, oauth_path, reason}``. The web app offers YTM as a
    destination only when ``available`` is True, and shows ``reason`` otherwise.
    Accepts either a browser-auth file or an OAuth token file (the latter also
    needs the client id/secret).
    """
    path = ytm_oauth_path()
    if YTMusic is None:
        return {"available": False, "oauth_path": path,
                "reason": "ytmusicapi is not installed (pip install ytmusicapi)."}
    if not path.exists():
        return {"available": False, "oauth_path": path,
                "reason": f"No YouTube Music auth file at {path}. Set one up with "
                          "`ytmusicapi browser` (recommended) or `ytmusicapi oauth`."}
    if _is_oauth_file(path) and not all(ytm_client_creds()):
        return {"available": False, "oauth_path": path,
                "reason": "OAuth auth file needs YTM_CLIENT_ID and "
                          "YTM_CLIENT_SECRET set to refresh its token."}
    return {"available": True, "oauth_path": path, "reason": ""}


def connect_ytmusic(config):
    """Authenticate a YTMusic client from the configured auth file.

    Auto-detects browser-auth (headers) vs OAuth (token) files; OAuth additionally
    needs the client id/secret to build refresh credentials.
    """
    if YTMusic is None:
        raise YTMError("ytmusicapi is not installed (pip install ytmusicapi).")
    auth_file = config.get("ytm_oauth_path")
    if not (auth_file and os.path.exists(str(auth_file))):
        raise YTMError("No YouTube Music auth file — set one up with "
                       "`ytmusicapi browser` or `ytmusicapi oauth`.")
    try:
        if _is_oauth_file(auth_file):
            client_id = config.get("ytm_client_id")
            client_secret = config.get("ytm_client_secret")
            if not (client_id and client_secret):
                raise YTMError("OAuth auth file needs YTM_CLIENT_ID and "
                               "YTM_CLIENT_SECRET to refresh its token.")
            creds = OAuthCredentials(client_id=client_id,
                                     client_secret=client_secret)
            return YTMusic(str(auth_file), oauth_credentials=creds)
        # Browser auth: pass sanitized headers as a dict so a Chrome paste
        # (with junk keys) works without hand-editing the file.
        with open(auth_file, encoding="utf-8") as fh:
            headers = json.load(fh)
        return YTMusic(_clean_browser_headers(headers))
    except YTMError:
        raise
    except Exception as exc:
        raise YTMError(
            f"Could not authenticate to YouTube Music: {exc}") from exc


# ---------------------------------------------------------------------------
# One-click connect: build an auth file from a logged-in browser's cookies,
# so nobody has to copy request headers by hand.
# ---------------------------------------------------------------------------

def browser_connect_available():
    """True if we can offer cookie-based connect (deps present)."""
    return _bc3 is not None and _ytmusicapi is not None


def _cookie_source_defs():
    """(id, label, loader) for each installed browser cookie store (macOS).

    Loaders are lazy and may raise if the browser/profile isn't present; the
    caller guards. Chrome is enumerated per profile (accounts differ by profile).
    """
    if _bc3 is None:
        return []
    defs = [("safari", "Safari", lambda: _bc3.safari(domain_name="youtube.com")),
            ("firefox", "Firefox", lambda: _bc3.firefox(domain_name="youtube.com"))]
    chrome_base = Path.home() / "Library/Application Support/Google/Chrome"
    for db in sorted(chrome_base.glob("*/Cookies")):
        profile = db.parent.name
        defs.append((f"chrome:{profile}", f"Chrome — {profile}",
                     lambda db=db: _bc3.chrome(cookie_file=str(db),
                                               domain_name="youtube.com")))
    for bid, label in (("edge", "Edge"), ("brave", "Brave"), ("arc", "Arc")):
        loader = getattr(_bc3, bid, None)
        if loader:
            defs.append((bid, label,
                         lambda loader=loader: loader(domain_name="youtube.com")))
    return defs


def _source_cookies(source_id):
    """Cookie name→value dict for a source id, or raise YTMError."""
    for sid, _label, loader in _cookie_source_defs():
        if sid == source_id:
            try:
                return {c.name: c.value for c in loader()}
            except Exception as exc:
                raise YTMError(f"Could not read cookies from {source_id}: "
                               f"{exc}") from exc
    raise YTMError(f"Unknown browser source '{source_id}'.")


def _auth_raw_from_cookies(cookies):
    """Build a clean ytmusicapi header block from a cookie dict.

    The __Secure-3PAPISID cookie is the durable credential — ytmusicapi
    recomputes the SAPISIDHASH from it per request, so the file lasts as long as
    the browser session does.
    """
    sapisid = cookies.get("__Secure-3PAPISID")
    if not sapisid:
        raise YTMError("No YouTube session there — sign in at music.youtube.com.")
    cookie_str = "; ".join(f"{n}={v}" for n, v in cookies.items())
    authorization = get_authorization(sapisid + " " + _YTM_ORIGIN)
    return "\n".join([f"cookie: {cookie_str}",
                      f"authorization: {authorization}",
                      "x-goog-authuser: 0",
                      f"origin: {_YTM_ORIGIN}"])


def list_ytm_sources():
    """Browser sources that hold a YouTube session. Cheap — no network."""
    sources = []
    for sid, label, loader in _cookie_source_defs():
        try:
            cookies = {c.name: c.value for c in loader()}
        except Exception:
            continue
        if cookies.get("__Secure-3PAPISID"):
            sources.append({"id": sid, "label": label})
    return sources


def ytm_source_account(source_id):
    """The Google account name behind a source, or None if it can't be read.

    Costs one YouTube Music API call — used to label the connect picker.
    """
    try:
        raw = _auth_raw_from_cookies(_source_cookies(source_id))
        headers = json.loads(_ytmusicapi.setup(headers_raw=raw))
        return YTMusic(headers).get_account_info().get("accountName")
    except Exception:
        return None


def connect_ytm_source(source_id):
    """Write the auth file from a browser source's cookies. Returns its path."""
    if not browser_connect_available():
        raise YTMError("Browser connect needs the browser_cookie3 package.")
    raw = _auth_raw_from_cookies(_source_cookies(source_id))
    path = ytm_oauth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _ytmusicapi.setup(filepath=str(path), headers_raw=raw)
    return path


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


# Editing existing YouTube Music playlists is deferred: ytmusicapi 1.12.1 can't
# parse owned-playlist responses (twoColumnBrowseResultsRenderer), so we can't
# read a playlist's current tracks / setVideoIds to diff against. These stubs
# keep the builder's edit seam uniform; implement them once reads work.
_EDIT_UNSUPPORTED = ("Editing YouTube Music playlists isn't supported yet "
                     "(ytmusicapi can't read owned playlists).")


def open_ytm_playlist(config, playlist_id):
    raise YTMError(_EDIT_UNSUPPORTED)


def apply_playlist_edits_ytm(config, playlist_id, name, desired_rows):
    raise YTMError(_EDIT_UNSUPPORTED)


def delete_playlist_ytm(config, playlist_id):
    raise YTMError(_EDIT_UNSUPPORTED)

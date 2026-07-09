"""Tests for the YouTube Music backend.

Fully offline: a FakeYTMusic stands in for the ytmusicapi client, so these run
even when ytmusicapi isn't installed (the soft import leaves ytm_service.YTMusic
as None, which each test overrides via monkeypatch).
"""

import json

import pytest

import setlist_to_plex as core
import ytm_service as y


_SONG = {"videoId": "v1", "title": "Wilson",
         "artists": [{"name": "Phish"}], "album": {"name": "Junta"}}


class FakeYTMusic:
    """Stand-in for ytmusicapi.YTMusic covering the surface the shims use."""

    def __init__(self, *args, **kwargs):
        self.created = None

    def search(self, query, filter=None, limit=0):
        if filter == "artists":
            return [{"browseId": "ART1", "artist": "Phish"}]
        if filter == "songs":
            return [dict(_SONG)]
        return []

    def get_artist(self, browse_id):
        return {"songs": {"results": [dict(_SONG)]}}

    def create_playlist(self, name, description, video_ids=None):
        self.created = (name, description, list(video_ids or []))
        return "PL123"


def _browser_auth(tmp_path):
    """A browser-style auth file (headers; no refresh_token)."""
    p = tmp_path / "ytm_auth.json"
    p.write_text(json.dumps({"cookie": "c", "authorization": "a"}),
                 encoding="utf-8")
    return p


def _oauth_auth(tmp_path):
    """An OAuth-style token file (carries a refresh_token)."""
    p = tmp_path / "ytm_auth.json"
    p.write_text(json.dumps({"access_token": "a", "refresh_token": "r",
                             "token_type": "Bearer", "expires_in": 0}),
                 encoding="utf-8")
    return p


@pytest.fixture
def ytm(monkeypatch):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.setattr(y, "OAuthCredentials", lambda **k: object())
    return FakeYTMusic()


def _config(auth_path, with_creds=True):
    cfg = {"api_key": "k", "ytm_oauth_path": str(auth_path)}
    if with_creds:
        cfg["ytm_client_id"] = "cid"
        cfg["ytm_client_secret"] = "csec"
    return cfg


# ---------------------------------------------------------------------------
# path / detection
# ---------------------------------------------------------------------------

def test_oauth_path_honors_override(monkeypatch):
    monkeypatch.setenv("YTM_OAUTH_FILE", "/tmp/custom-oauth.json")
    assert y.ytm_oauth_path() == core.Path("/tmp/custom-oauth.json")


def test_oauth_path_defaults_beside_history(monkeypatch):
    monkeypatch.delenv("YTM_OAUTH_FILE", raising=False)
    monkeypatch.delenv("SETLIST_TO_PLEX_HISTORY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert y.ytm_oauth_path() == core.Path(
        "/tmp/xdg/setlist_to_plex/ytm_oauth.json")


def test_is_oauth_file_distinguishes(tmp_path):
    assert y._is_oauth_file(_oauth_auth(tmp_path)) is True
    assert y._is_oauth_file(_browser_auth(tmp_path)) is False
    assert y._is_oauth_file(tmp_path / "missing.json") is False


def test_clean_browser_headers_drops_chrome_junk():
    raw = {
        "cookie": "c", "authorization": "a", "user-agent": "ua",
        "x-goog-authuser": "0",
        # junk a Chrome paste introduces:
        "/youtubei/v1/browse?prettyprint=false": "", "decoded": "",
        "music.youtube.com": "", "content-encoding": "gzip",
        "accept-encoding": "gzip, deflate", "x-browser-year": "2024",
    }
    cleaned = y._clean_browser_headers(raw)
    assert set(cleaned) == {"cookie", "authorization", "user-agent",
                            "x-goog-authuser"}


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------

def test_unavailable_when_library_missing(monkeypatch):
    monkeypatch.setattr(y, "YTMusic", None)
    cfg = y.load_ytm_config()
    assert cfg["available"] is False
    assert "not installed" in cfg["reason"]


def test_unavailable_when_no_auth_file(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.setenv("YTM_OAUTH_FILE", str(tmp_path / "absent.json"))
    cfg = y.load_ytm_config()
    assert cfg["available"] is False
    assert "auth file" in cfg["reason"]


def test_browser_file_available_without_creds(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.delenv("YTM_CLIENT_ID", raising=False)
    monkeypatch.delenv("YTM_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("YTM_OAUTH_FILE", str(_browser_auth(tmp_path)))
    cfg = y.load_ytm_config()
    assert cfg["available"] is True                # browser auth needs no creds
    assert cfg["reason"] == ""


def test_oauth_file_needs_creds(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.delenv("YTM_CLIENT_ID", raising=False)
    monkeypatch.delenv("YTM_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("YTM_OAUTH_FILE", str(_oauth_auth(tmp_path)))
    cfg = y.load_ytm_config()
    assert cfg["available"] is False
    assert "YTM_CLIENT_ID" in cfg["reason"]


def test_oauth_file_available_with_creds(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.setenv("YTM_CLIENT_ID", "cid")
    monkeypatch.setenv("YTM_CLIENT_SECRET", "csec")
    monkeypatch.setenv("YTM_OAUTH_FILE", str(_oauth_auth(tmp_path)))
    cfg = y.load_ytm_config()
    assert cfg["available"] is True


# ---------------------------------------------------------------------------
# connect (auto-detects browser vs oauth)
# ---------------------------------------------------------------------------

def test_connect_errors_without_library(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", None)
    with pytest.raises(y.YTMError):
        y.connect_ytmusic(_config(_browser_auth(tmp_path)))


def test_connect_errors_without_auth_file(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    with pytest.raises(y.YTMError):
        y.connect_ytmusic(_config(tmp_path / "missing.json"))


def test_connect_browser_needs_no_creds(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    client = y.connect_ytmusic(_config(_browser_auth(tmp_path), with_creds=False))
    assert isinstance(client, FakeYTMusic)          # plain YTMusic(file), no creds


def test_connect_oauth_needs_creds(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    with pytest.raises(y.YTMError):
        y.connect_ytmusic(_config(_oauth_auth(tmp_path), with_creds=False))


def test_connect_oauth_with_creds(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.setattr(y, "OAuthCredentials", lambda **k: object())
    client = y.connect_ytmusic(_config(_oauth_auth(tmp_path)))
    assert isinstance(client, FakeYTMusic)


# ---------------------------------------------------------------------------
# shims duck-type the matcher's plexapi surface
# ---------------------------------------------------------------------------

def test_track_view_maps_fields():
    t = y._ytm_track_view(_SONG)
    assert t.title == "Wilson"
    assert t.grandparentTitle == "Phish"   # artist for _track_artist_name
    assert t.parentTitle == "Junta"        # album for _track_album
    assert t.ratingKey == "v1"             # videoId as the dedupe key


def test_section_search_tracks_returns_track_views(ytm):
    section = y.YTMSection(ytm)
    tracks = section.searchTracks(title="Wilson", maxresults=5)
    assert [t.title for t in tracks] == ["Wilson"]
    assert core._track_artist_name(tracks[0]) == "Phish"
    assert core._track_album(tracks[0]) == "Junta"


def test_section_search_artists_returns_artists_with_tracks(ytm):
    section = y.YTMSection(ytm)
    artists = section.searchArtists(title="Phish", maxresults=5)
    assert [a.title for a in artists] == ["Phish"]
    assert [t.title for t in artists[0].tracks()] == ["Wilson"]


# ---------------------------------------------------------------------------
# the whole matcher runs against YouTube Music via the seam
# ---------------------------------------------------------------------------

def test_gather_matches_against_ytm(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.setattr(core, "fetch_setlist", lambda sid, key: {})
    monkeypatch.setattr(core, "extract_show", lambda data: {
        "artist": "Phish", "venue": "MSG", "city": "NY",
        "date": "2023-12-31", "url": "", "songs": ["Wilson"]})
    monkeypatch.setattr(core, "fetch_album_map", lambda url: {})

    result = core.gather_matches(_config(_browser_auth(tmp_path)), "abc123",
                                 service=y.YTM_SERVICE)
    assert result["service"] == "ytm"
    assert result["matched"][0]["rating_key"] == "v1"       # videoId flows through
    assert result["matched"][0]["track_title"] == "Wilson"
    assert result["missing"] == []


# ---------------------------------------------------------------------------
# playlist creation
# ---------------------------------------------------------------------------

def test_create_playlist_dedupes_and_records_history(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    hist = tmp_path / "history.json"
    monkeypatch.setattr(core, "history_path", lambda: hist)

    name = y.create_playlist_ytm(
        _config(_browser_auth(tmp_path)), "My Mix", ["v1", "v2", "v1", None],
        history_meta={"id": "abc123", "url": "u", "artist": "Phish",
                      "date": "2023-12-31"})

    assert name == "My Mix"
    saved = core.load_history(hist)
    assert saved["abc123"]["service"] == "ytm"
    assert saved["abc123"]["matched"] == 2                  # v1,v2 — deduped, None dropped
    assert saved["abc123"]["playlist_rating_key"] == "PL123"


def test_create_playlist_errors_on_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    with pytest.raises(y.YTMError):
        y.create_playlist_ytm(_config(_browser_auth(tmp_path)), "Empty", [None, ""])


# ---------------------------------------------------------------------------
# one-click connect from a browser's cookies
# ---------------------------------------------------------------------------

class _FakeCookie:
    def __init__(self, name, value):
        self.name, self.value = name, value


def _jar(*pairs):
    return [_FakeCookie(n, v) for n, v in pairs]


def test_browser_connect_available(monkeypatch):
    monkeypatch.setattr(y, "_bc3", object())
    monkeypatch.setattr(y, "_ytmusicapi", object())
    assert y.browser_connect_available() is True
    monkeypatch.setattr(y, "_bc3", None)
    assert y.browser_connect_available() is False


def test_auth_raw_requires_sapisid():
    with pytest.raises(y.YTMError):
        y._auth_raw_from_cookies({"SID": "x"})          # no __Secure-3PAPISID


def test_auth_raw_builds_header_block():
    raw = y._auth_raw_from_cookies({"__Secure-3PAPISID": "abc", "SID": "s"})
    assert "cookie: __Secure-3PAPISID=abc; SID=s" in raw
    assert "authorization: SAPISIDHASH" in raw          # type-detection header
    assert "x-goog-authuser: 0" in raw


def test_list_ytm_sources_keeps_only_logged_in(monkeypatch):
    defs = [
        ("safari", "Safari", lambda: _jar(("__Secure-3PAPISID", "a"), ("SID", "s"))),
        ("chrome:Default", "Chrome — Default", lambda: _jar(("SID", "x"))),  # no session
        ("firefox", "Firefox", lambda: (_ for _ in ()).throw(RuntimeError("no ff"))),
    ]
    monkeypatch.setattr(y, "_cookie_source_defs", lambda: defs)
    assert [s["id"] for s in y.list_ytm_sources()] == ["safari"]


def test_connect_ytm_source_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "_bc3", object())
    monkeypatch.setattr(y, "_source_cookies",
                        lambda sid: {"__Secure-3PAPISID": "a"})
    path = tmp_path / "ytm_oauth.json"
    monkeypatch.setattr(y, "ytm_oauth_path", lambda: path)

    calls = {}

    class FakeYtmModule:
        def setup(self, filepath=None, headers_raw=None):
            calls["filepath"], calls["raw"] = filepath, headers_raw
            open(filepath, "w").write("{}")
            return "{}"
    monkeypatch.setattr(y, "_ytmusicapi", FakeYtmModule())

    result = y.connect_ytm_source("safari")
    assert result == path
    assert calls["filepath"] == str(path)
    assert "SAPISIDHASH" in calls["raw"]


def test_connect_ytm_source_needs_deps(monkeypatch):
    monkeypatch.setattr(y, "_bc3", None)
    with pytest.raises(y.YTMError):
        y.connect_ytm_source("safari")

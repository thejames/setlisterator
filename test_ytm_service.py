"""Tests for the YouTube Music backend.

Fully offline: a FakeYTMusic stands in for the ytmusicapi client, so these run
even when ytmusicapi isn't installed (the soft import leaves ytm_service.YTMusic
as None, which each test overrides via monkeypatch).
"""

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


@pytest.fixture
def ytm(monkeypatch):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    return FakeYTMusic()


_CONFIG = {"api_key": "k", "ytm_oauth_path": "/tmp/oauth.json"}


# ---------------------------------------------------------------------------
# config / availability
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


def test_unavailable_when_library_missing(monkeypatch):
    monkeypatch.setattr(y, "YTMusic", None)
    cfg = y.load_ytm_config()
    assert cfg["available"] is False
    assert "not installed" in cfg["reason"]


def test_unavailable_when_oauth_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.setenv("YTM_OAUTH_FILE", str(tmp_path / "absent.json"))
    cfg = y.load_ytm_config()
    assert cfg["available"] is False
    assert "OAuth file" in cfg["reason"]


def test_available_when_library_and_file_present(monkeypatch, tmp_path):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    oauth = tmp_path / "oauth.json"
    oauth.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("YTM_OAUTH_FILE", str(oauth))
    cfg = y.load_ytm_config()
    assert cfg["available"] is True
    assert cfg["reason"] == ""


def test_connect_ytmusic_errors_without_library(monkeypatch):
    monkeypatch.setattr(y, "YTMusic", None)
    with pytest.raises(y.YTMError):
        y.connect_ytmusic(_CONFIG)


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

def test_gather_matches_against_ytm(monkeypatch):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    monkeypatch.setattr(core, "fetch_setlist", lambda sid, key: {})
    monkeypatch.setattr(core, "extract_show", lambda data: {
        "artist": "Phish", "venue": "MSG", "city": "NY",
        "date": "2023-12-31", "url": "", "songs": ["Wilson"]})
    monkeypatch.setattr(core, "fetch_album_map", lambda url: {})

    result = core.gather_matches(_CONFIG, "abc123", service=y.YTM_SERVICE)
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
        _CONFIG, "My Mix", ["v1", "v2", "v1", None],
        history_meta={"id": "abc123", "url": "u", "artist": "Phish",
                      "date": "2023-12-31"})

    assert name == "My Mix"
    saved = core.load_history(hist)
    assert saved["abc123"]["service"] == "ytm"
    assert saved["abc123"]["matched"] == 2                  # v1,v2 — deduped, None dropped
    assert saved["abc123"]["playlist_rating_key"] == "PL123"


def test_create_playlist_errors_on_empty(monkeypatch):
    monkeypatch.setattr(y, "YTMusic", FakeYTMusic)
    with pytest.raises(y.YTMError):
        y.create_playlist_ytm(_CONFIG, "Empty", [None, ""])

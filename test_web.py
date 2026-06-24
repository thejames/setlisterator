"""Tests for the Flask web interface.

The core pipeline (load_config / gather_matches / create_playlist) is
monkeypatched so these exercise routing and rendering only — no network, no
Plex, no setlist.fm.
"""

import pytest

import setlist_to_plex as core
import web
from web import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(core, "load_config", lambda: {
        "api_key": "k", "plex_baseurl": "http://x", "plex_token": "t",
        "music_library": "Music"})
    app.config.update(TESTING=True)
    return app.test_client()


def _preview_result():
    # single candidate -> rendered as a hidden "pick" input
    tommy = {"position": 1, "title": "Tommy the Cat",
             "track_title": "Tommy the Cat", "track_artist": "Primus",
             "album": "Sailing the Seas of Cheese", "rating_key": 10,
             "tier": "exact", "source": "scoped", "quality": "exact",
             "candidates": [
                 {"rating_key": 10, "track_title": "Tommy the Cat",
                  "track_artist": "Primus",
                  "album": "Sailing the Seas of Cheese",
                  "tier": "exact", "source": "scoped", "quality": "exact"}]}
    # two candidates -> rendered as a <select name="pick">
    jerry = {"position": 2, "title": "Jerry Was a Race Car Driver",
             "track_title": "Jerry Was a Race Car Driver", "track_artist": "Primus",
             "album": "Sailing the Seas of Cheese", "rating_key": 20,
             "tier": "exact", "source": "scoped", "quality": "exact",
             "candidates": [
                 {"rating_key": 20, "track_title": "Jerry Was a Race Car Driver",
                  "track_artist": "Primus", "album": "Sailing the Seas of Cheese",
                  "tier": "exact", "source": "scoped", "quality": "exact"},
                 {"rating_key": 21, "track_title": "Jerry Was a Race Car Driver",
                  "track_artist": "Primus", "album": "Suck on This (Live)",
                  "tier": "exact", "source": "scoped", "quality": "exact"}]}
    return {
        "setlist_id": "abc123",
        "show": {"artist": "Primus", "venue": "TD Amp", "city": "Charlotte",
                 "date": "2026-06-16", "url": "https://setlist.fm/x.html"},
        "playlist_name": "Primus - TD Amp, Charlotte (2026-06-16)",
        "matched": [tommy, jerry],
        "missing": [(3, "Primus", "Jilly's on Smack")],
        "fuzzy": [(2, "Primus - Hello Skinny",
                   "Primus - Hello Skinny / Constantinople")],
        # full setlist in order: both matched songs plus the missing one inline
        "songs": [
            {**tommy, "matched": True},
            {**jerry, "matched": True},
            {"position": 3, "title": "Jilly's on Smack", "matched": False,
             "artist": "Primus"},
        ],
    }


def test_index_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"setlist.fm URL or ID" in resp.data


def test_port_default_and_override(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert web._port() == 5001
    monkeypatch.setenv("PORT", "8080")
    assert web._port() == 8080


def test_navbar_present(client):
    body = client.get("/").data.decode()
    assert "Setlist-er-ator" in body            # brand
    assert 'href="/history"' in body            # History link
    assert 'href="/buylist"' in body            # Buy list link


def test_buylist_aggregates_and_dedupes(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {
        "a": {"missing_tracks": [
            {"artist": "Primus", "title": "Jilly's on Smack"},
            {"artist": "Primus", "title": "The Ol' Grizz"}]},
        "b": {"missing_tracks": [
            {"artist": "Primus", "title": "Jilly's on Smack"}]},  # dup
    })
    body = client.get("/buylist").data.decode()
    # deduped to two unique tracks; the repeated one shows in 2 shows
    assert body.count("Jilly&#39;s on Smack") == 1
    assert "2 shows" in body
    assert "The Ol&#39; Grizz" in body


def test_buylist_empty(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {})
    body = client.get("/buylist").data.decode()
    assert "Nothing to buy" in body


def test_history_lists_entries_newest_first(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {
        "old": {"id": "old", "artist": "Phish", "date": "2023-12-31",
                "playlist_name": "Phish — MSG", "processed_at": "2026-06-20",
                "matched": 21, "missing": 0,
                "url": "https://setlist.fm/phish.html"},
        "new": {"id": "new", "artist": "Primus", "date": "2026-06-16",
                "playlist_name": "Primus — TD Amp", "processed_at": "2026-06-23",
                "matched": 8, "missing": 4,
                "url": "https://setlist.fm/primus.html"},
    })
    body = client.get("/history").data.decode()
    assert "Phish — MSG" in body and "Primus — TD Amp" in body
    # newest processed_at first
    assert body.index("Primus — TD Amp") < body.index("Phish — MSG")
    # Re-open posts the stored URL to the existing preview route
    assert 'name="setlist" value="https://setlist.fm/primus.html"' in body
    assert "4 missing" in body


def test_history_empty(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {})
    body = client.get("/history").data.decode()
    assert "No history yet" in body


def test_index_config_error(client, monkeypatch):
    def raise_cfg():
        raise core.ConfigError("Missing required environment variable(s): X")
    monkeypatch.setattr(core, "load_config", raise_cfg)
    resp = client.get("/")
    assert b"Missing required environment variable" in resp.data


def test_preview_renders_matches(client, monkeypatch):
    monkeypatch.setattr(core, "gather_matches",
                        lambda cfg, sid, name=None: _preview_result())
    monkeypatch.setattr(core, "load_history", lambda path: {})
    resp = client.post("/preview", data={"setlist": "abc123"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Tommy the Cat" in body                 # matched
    assert "Sailing the Seas of Cheese" in body     # album surfaced
    assert "Jilly&#39;s on Smack" in body           # missing (HTML-escaped)
    # per-position fields: single-candidate -> hidden input; multi -> <select>
    assert '<input type="hidden" name="pick_1" value="10">' in body
    assert '<select name="pick_2">' in body
    assert 'value="21"' in body                     # the alternate album option
    assert "Suck on This (Live)" in body            # alternate album shown
    # each matched row has an include checkbox, checked by default
    assert 'name="include" value="1" checked' in body
    assert 'name="include" value="2" checked' in body
    # the missing song is shown inline in the full-setlist table...
    assert "not in your library" in body
    assert 'class="missing"' in body
    assert "go buy these" not in body               # label removed


def test_preview_requires_input(client):
    resp = client.post("/preview", data={"setlist": ""})
    assert resp.status_code == 400


def test_preview_missing_row_has_search_ui(client, monkeypatch):
    monkeypatch.setattr(core, "gather_matches",
                        lambda cfg, sid, name=None: _preview_result())
    monkeypatch.setattr(core, "load_history", lambda path: {})
    body = client.post("/preview", data={"setlist": "abc"}).data.decode()
    # the missing row gets a search box + disabled pick/include the JS fills
    assert 'class="q"' in body
    assert 'name="pick_3"' in body
    assert 'name="include" value="3" class="inc" disabled hidden' in body
    assert "app.js" in body                      # script is wired in


# --- /search (manual override JSON endpoint) -------------------------------

class _Track:
    def __init__(self, title, artist, album, key):
        self.title = title
        self.grandparentTitle = artist
        self.parentTitle = album
        self.ratingKey = key
        self.originalTitle = None


class _Section:
    def __init__(self, tracks):
        self._tracks = tracks

    def searchTracks(self, title=None, maxresults=None):
        return list(self._tracks)


def test_search_returns_json(client, monkeypatch):
    section = _Section([_Track("Jilly's on Smack", "Primus", "Pork Soda", 77)])
    monkeypatch.setattr(core, "connect_plex", lambda u, t: object())
    monkeypatch.setattr(core, "get_music_section", lambda plex, lib: section)
    data = client.get("/search?q=jilly").get_json()
    assert data["results"][0] == {
        "rating_key": 77, "title": "Jilly's on Smack",
        "artist": "Primus", "album": "Pork Soda"}


def test_search_empty_query(client):
    assert client.get("/search?q=").get_json() == {"results": []}


def test_search_config_error(client, monkeypatch):
    def boom():
        raise core.ConfigError("Missing required environment variable(s): X")
    monkeypatch.setattr(core, "load_config", boom)
    resp = client.get("/search?q=x")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_search_plex_error(client, monkeypatch):
    def boom(u, t):
        raise ConnectionError("Could not reach Plex")
    monkeypatch.setattr(core, "connect_plex", boom)
    resp = client.get("/search?q=x")
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_preview_disables_create_when_no_matches(client, monkeypatch):
    # A setlist where nothing is in the library: full setlist still shows,
    # but the create button is disabled.
    all_missing = {
        "setlist_id": "abc", "playlist_name": "Nobody — Nowhere",
        "show": {"artist": "Nobody", "venue": "Nowhere", "city": "X",
                 "date": "2026-01-01", "url": ""},
        "matched": [], "missing": [(1, "Nobody", "Some Song")], "fuzzy": [],
        "songs": [{"position": 1, "title": "Some Song", "matched": False,
                   "artist": "Nobody"}],
    }
    monkeypatch.setattr(core, "gather_matches", lambda c, s, n=None: all_missing)
    monkeypatch.setattr(core, "load_history", lambda p: {})
    body = client.post("/preview", data={"setlist": "abc"}).data.decode()
    assert "<button type=\"submit\" disabled>" in body
    assert "Some Song" in body            # still shows the (missing) setlist


def test_preview_setlist_error(client, monkeypatch):
    def boom(cfg, sid, name=None):
        raise core.SetlistError("No setlist found with ID 'abc123'.")
    monkeypatch.setattr(core, "gather_matches", boom)
    resp = client.post("/preview", data={"setlist": "abc123"})
    assert b"Setlist problem" in resp.data
    assert b"No setlist found" in resp.data


def test_create_builds_playlist(client, monkeypatch):
    captured = {}

    def fake_create(cfg, name, rating_keys, history_meta):
        captured["name"] = name
        captured["keys"] = rating_keys
        captured["meta"] = history_meta
        return name + " (2)"   # simulate a name collision suffix

    monkeypatch.setattr(core, "create_playlist", fake_create)
    resp = client.post("/create", data={
        "name": "Primus - TD Amp",
        "setlist_id": "abc123",
        "url": "https://setlist.fm/x.html",
        "artist": "Primus",
        "date": "2026-06-16",
        "include": ["1", "2"],
        "pick_1": "10",
        "pick_2": "21",          # second song: the alternate album was chosen
        "missing_json": '[[3, "Primus", "Jilly\'s on Smack"]]',
        "fuzzy_json": "[]",
    })
    assert resp.status_code == 200
    assert captured["keys"] == ["10", "21"]   # picks honored in position order
    assert captured["meta"]["missing"] == 1
    body = resp.data.decode()
    assert "Primus - TD Amp (2)" in body         # final (suffixed) name shown
    assert "Jilly&#39;s on Smack" in body        # buy-list persisted


def test_create_excludes_unchecked_rows(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(core, "create_playlist",
                        lambda cfg, name, keys, meta: captured.update(keys=keys)
                        or name)
    # Three matched rows, but row 2 is left out of "include".
    resp = client.post("/create", data={
        "name": "Show", "setlist_id": "abc",
        "include": ["1", "3"],
        "pick_1": "10", "pick_2": "20", "pick_3": "30",
        "missing_json": "[]", "fuzzy_json": "[]",
    })
    assert resp.status_code == 200
    assert captured["keys"] == ["10", "30"]   # row 2 excluded, order preserved


def test_create_requires_keys(client):
    resp = client.post("/create", data={"name": "X"})  # nothing included
    assert resp.status_code == 400


def test_create_added_count_is_deduped(client, monkeypatch):
    # Two songs resolving to the same track (e.g. a medley) -> counted once.
    monkeypatch.setattr(core, "create_playlist",
                        lambda cfg, name, keys, meta: name)
    resp = client.post("/create", data={
        "name": "Show", "setlist_id": "abc",
        "include": ["1", "2", "3"],
        "pick_1": "10", "pick_2": "10", "pick_3": "21",   # 10 chosen twice
        "missing_json": "[]", "fuzzy_json": "[]",
    })
    body = resp.data.decode()
    assert "2 tracks added" in body   # deduped: {10, 21}, not 3

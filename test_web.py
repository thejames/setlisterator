"""Tests for the Flask web interface.

The core pipeline (load_config / gather_matches / create_playlist) is
monkeypatched so these exercise routing and rendering only — no network, no
Plex, no setlist.fm.
"""

import pytest

import setlist_to_plex as core
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


def test_navbar_present(client):
    body = client.get("/").data.decode()
    assert "Setlist-er-ator" in body            # brand
    assert 'href="/history"' in body            # History link


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
    # single-candidate song -> hidden pick input; multi-candidate -> a <select>
    assert '<input type="hidden" name="pick" value="10">' in body
    assert '<select name="pick">' in body
    assert 'value="21"' in body                     # the alternate album option
    assert "Suck on This (Live)" in body            # alternate album shown
    # the missing song is shown inline in the full-setlist table...
    assert "not in your library" in body
    assert 'class="missing"' in body
    # ...with no pick input for it (only the two matched songs contribute)
    assert body.count('name="pick"') == 2
    assert "go buy these" not in body               # label removed


def test_preview_requires_input(client):
    resp = client.post("/preview", data={"setlist": ""})
    assert resp.status_code == 400


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
        "pick": ["10", "21"],   # second song: the alternate album was chosen
        "missing_json": '[[3, "Primus", "Jilly\'s on Smack"]]',
        "fuzzy_json": "[]",
    })
    assert resp.status_code == 200
    assert captured["keys"] == ["10", "21"]   # picks honored in order
    assert captured["meta"]["missing"] == 1
    body = resp.data.decode()
    assert "Primus - TD Amp (2)" in body         # final (suffixed) name shown
    assert "Jilly&#39;s on Smack" in body        # buy-list persisted


def test_create_requires_keys(client):
    resp = client.post("/create", data={"name": "X"})  # no "pick" values
    assert resp.status_code == 400

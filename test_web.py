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
    return {
        "setlist_id": "abc123",
        "show": {"artist": "Primus", "venue": "TD Amp", "city": "Charlotte",
                 "date": "2026-06-16", "url": "https://setlist.fm/x.html"},
        "playlist_name": "Primus - TD Amp, Charlotte (2026-06-16)",
        "matched": [
            {"position": 1, "title": "Tommy the Cat",
             "track_title": "Tommy the Cat", "track_artist": "Primus",
             "rating_key": 10, "tier": "exact", "source": "scoped",
             "quality": "exact"},
            {"position": 2, "title": "Hello Skinny",
             "track_title": "Hello Skinny / Constantinople",
             "track_artist": "Primus", "rating_key": 11, "tier": "medley",
             "source": "scoped", "quality": "fuzzy"},
        ],
        "missing": [(3, "Primus", "Jilly's on Smack")],
        "fuzzy": [(2, "Primus - Hello Skinny",
                   "Primus - Hello Skinny / Constantinople")],
    }


def test_index_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"setlist.fm URL or ID" in resp.data


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
    assert "Hello Skinny / Constantinople" in body  # medley fuzzy
    assert "Jilly&#39;s on Smack" in body           # missing (HTML-escaped)
    assert 'value="10,11"' in body                  # rating keys carried forward


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
        "rating_keys": "10,11",
        "missing_json": '[[3, "Primus", "Jilly\'s on Smack"]]',
        "fuzzy_json": "[]",
    })
    assert resp.status_code == 200
    assert captured["keys"] == ["10", "11"]
    assert captured["meta"]["missing"] == 1
    body = resp.data.decode()
    assert "Primus - TD Amp (2)" in body         # final (suffixed) name shown
    assert "Jilly&#39;s on Smack" in body        # buy-list persisted


def test_create_requires_keys(client):
    resp = client.post("/create", data={"name": "X", "rating_keys": ""})
    assert resp.status_code == 400

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
        "missing": [(3, "Primus", "Jilly's on Smack", "Green Naugahyde")],
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
    body = resp.data.decode()
    assert "setlist.fm URL or ID" in body
    assert 'data-loading="Matching the setlist…"' in body  # loading feedback
    assert "app.js" in body                                # script on every page


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
            {"artist": "Primus", "title": "The Ol' Grizz"},
            {"artist": "Goose", "title": "Arrow"}]},
        "b": {"missing_tracks": [
            {"artist": "Primus", "title": "Jilly's on Smack"}]},  # dup
    })
    # no MusicBrainz network, no real history writes
    monkeypatch.setattr(core, "lookup_album", lambda a, t: "Pork Soda")
    monkeypatch.setattr(core, "save_history", lambda p, h: None)
    body = client.get("/buylist").data.decode()
    # deduped to one Jilly's entry; the repeated one counts as 2 shows
    assert body.count("Jilly&#39;s on Smack") == 1
    assert "2 shows" in body
    # grouped by artist, A->Z (Goose before Primus)
    assert "Goose" in body and "Primus" in body
    assert body.index("Goose") < body.index("Primus")
    # likely album surfaced from MusicBrainz
    assert "likely from Pork Soda" in body
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
    assert 'action="/update-preview"' in body   # Update button present


def test_history_empty(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {})
    body = client.get("/history").data.decode()
    assert "No history yet" in body


# --- update flow -----------------------------------------------------------

class _FakePL:
    def __init__(self, title, key, item_keys):
        self.title = title
        self.ratingKey = key
        self._items = [type("T", (), {"ratingKey": k})() for k in item_keys]

    def items(self):
        return list(self._items)


def test_update_preview_shows_only_new(client, monkeypatch):
    monkeypatch.setattr(core, "gather_matches",
                        lambda c, s, n=None: _preview_result())
    monkeypatch.setattr(core, "connect_plex", lambda u, t: object())
    # Tommy (rating_key 10) is already in the playlist; Jerry (20/21) is not.
    pl = _FakePL("Primus - TD Amp", 999, [10])
    monkeypatch.setattr(core, "find_playlist", lambda plex, **k: pl)
    body = client.post("/update-preview", data={
        "setlist": "abc", "playlist_rating_key": "999",
        "name": "Primus - TD Amp"}).data.decode()
    assert "Update" in body
    assert 'name="pick_2"' in body        # Jerry is new -> offered
    assert 'name="pick_1"' not in body    # Tommy already in playlist -> hidden
    assert 'value="999"' in body          # playlist key carried forward


def test_update_preview_nothing_new(client, monkeypatch):
    monkeypatch.setattr(core, "gather_matches",
                        lambda c, s, n=None: _preview_result())
    monkeypatch.setattr(core, "connect_plex", lambda u, t: object())
    pl = _FakePL("Primus - TD Amp", 999, [10, 20])   # both already present
    monkeypatch.setattr(core, "find_playlist", lambda plex, **k: pl)
    body = client.post("/update-preview", data={
        "setlist": "abc", "name": "Primus - TD Amp"}).data.decode()
    assert "Nothing new" in body


def test_update_preview_playlist_gone(client, monkeypatch):
    monkeypatch.setattr(core, "gather_matches",
                        lambda c, s, n=None: _preview_result())
    monkeypatch.setattr(core, "connect_plex", lambda u, t: object())
    monkeypatch.setattr(core, "find_playlist", lambda plex, **k: None)
    resp = client.post("/update-preview", data={"setlist": "abc", "name": "X"})
    assert b"Playlist not found" in resp.data


def test_update_adds_picked_tracks(client, monkeypatch):
    captured = {}

    def fake_add(cfg, key, name, keys, meta):
        captured.update(key=key, name=name, keys=keys, meta=meta)
        return name, len(keys)

    monkeypatch.setattr(core, "add_to_playlist", fake_add)
    resp = client.post("/update", data={
        "name": "Primus - TD Amp", "playlist_rating_key": "999",
        "setlist_id": "abc", "include": ["2"], "pick_2": "21",
        "missing_json": "[]",
    })
    assert resp.status_code == 200
    assert captured["keys"] == ["21"] and captured["key"] == "999"
    body = resp.data.decode()
    assert "Added 1 track" in body and "Primus - TD Amp" in body


def test_update_requires_picks(client):
    resp = client.post("/update", data={"name": "X", "playlist_rating_key": "1"})
    assert resp.status_code == 400


def test_index_config_error(client, monkeypatch):
    def raise_cfg():
        raise core.ConfigError("Missing required environment variable(s): X")
    monkeypatch.setattr(core, "load_config", raise_cfg)
    resp = client.get("/")
    assert b"Missing required environment variable" in resp.data


def test_preview_renders_matches(client, monkeypatch):
    monkeypatch.setattr(core, "gather_matches",
                        lambda cfg, sid, name=None, prefer_album=None: _preview_result())
    monkeypatch.setattr(core, "load_history", lambda path: {})
    resp = client.post("/preview", data={"setlist": "abc123"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Tommy the Cat" in body                 # matched
    assert "Sailing the Seas of Cheese" in body     # album surfaced
    assert "Jilly&#39;s on Smack" in body           # missing (HTML-escaped)
    # per-position fields: single-candidate -> hidden input; multi -> custom
    # dropdown (hidden pick_N + options carrying each candidate's rating key)
    assert '<input type="hidden" name="pick_1" value="10">' in body
    assert 'name="pick_2"' in body
    assert 'class="dropdown"' in body
    assert 'data-key="21"' in body                  # the alternate album option
    assert "Suck on This (Live)" in body            # alternate album shown
    # each matched row has an include checkbox, checked by default
    assert 'name="include" value="1" class="inc" checked' in body
    assert 'name="include" value="2" class="inc" checked' in body
    # the missing song is shown inline in the full-setlist table...
    assert "No match in library" in body
    assert 'class="missing"' in body
    assert "go buy these" not in body               # label removed
    # design: stat chips and the live selected count
    assert 'class="chip' in body
    assert "data-selected" in body
    # spec elements: URL bar + Re-import, custom Add box, count-in-button
    assert 'class="urlbar"' in body and "Re-import" in body
    # URL bar links out to setlist.fm in a new tab
    assert 'class="urlbar-link"' in body and 'target="_blank"' in body
    assert 'class="addbox"' in body
    assert "data-create-count" in body
    # matched rows get a manual-search escape hatch (single AND multi rows)
    assert 'data-rowsearch="1"' in body              # Tommy (single candidate)
    assert 'data-rowsearch="2"' in body              # Jerry (multi candidate)
    assert 'class="rowsearch-btn"' in body           # magnifier toggle by the pill
    # the Exact/Fuzzy pill is a button opening a match-explanation popover
    assert "data-matchinfo" in body                  # Tommy's clickable pill
    assert "Exact title match" in body               # tier -> plain language
    assert "Primus" in body and "own tracks in your library" in body  # source
    assert 'data-rownum="1"' in body and "data-rowtitle" in body


def test_stats_counts_are_exclusive():
    import web as webmod
    result = _preview_result()   # Tommy(exact,1 cand), Jerry(exact,2 cand), 1 missing
    s = webmod._stats(result)
    assert s == {"total": 3, "exact": 1, "fuzzy": 0, "multi": 1, "missing": 1}
    assert s["exact"] + s["fuzzy"] + s["multi"] + s["missing"] == s["total"]


def test_preview_requires_input(client):
    resp = client.post("/preview", data={"setlist": ""})
    assert resp.status_code == 400


def test_preview_missing_row_has_search_ui(client, monkeypatch):
    monkeypatch.setattr(core, "gather_matches",
                        lambda cfg, sid, name=None, prefer_album=None: _preview_result())
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
        "matched": [], "missing": [(1, "Nobody", "Some Song", "")], "fuzzy": [],
        "songs": [{"position": 1, "title": "Some Song", "matched": False,
                   "artist": "Nobody"}],
    }
    monkeypatch.setattr(core, "gather_matches",
                        lambda c, s, n=None, prefer_album=None: all_missing)
    monkeypatch.setattr(core, "load_history", lambda p: {})
    body = client.post("/preview", data={"setlist": "abc"}).data.decode()
    assert "disabled>Nothing in your library to add" in body
    assert "Some Song" in body            # still shows the (missing) setlist


def test_preview_setlist_error(client, monkeypatch):
    def boom(cfg, sid, name=None, prefer_album=None):
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
    assert captured["meta"]["missing_tracks"][0]["position"] == 3   # carried for summary
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


def test_preview_prefer_album_forwarded_and_rendered(client, monkeypatch):
    captured = {}

    def fake_gather(cfg, sid, name=None, prefer_album=None):
        captured["prefer_album"] = prefer_album
        result = _preview_result()
        result["preferred_album"] = "Live@ Sun Dome"
        result["album_options"] = [{"album": "Live@ Sun Dome", "songs": 5},
                                   {"album": "Studio", "songs": 2}]
        return result

    monkeypatch.setattr(core, "gather_matches", fake_gather)
    monkeypatch.setattr(core, "load_history", lambda path: {})
    body = client.post("/preview", data={
        "setlist": "abc", "prefer_album": "Live@ Sun Dome"}).data.decode()
    assert captured["prefer_album"] == "Live@ Sun Dome"      # forwarded to core
    assert 'name="prefer_album"' in body                     # the select is shown
    assert "Live@ Sun Dome (5 songs)" in body                # option with coverage
    assert 'value="Live@ Sun Dome" selected' in body         # pre-selected


def test_preview_first_load_auto_detects_album(client, monkeypatch):
    # No prefer_album in the form -> None reaches core (auto-detect).
    captured = {}

    def fake_gather(cfg, sid, name=None, prefer_album=None):
        captured["prefer_album"] = prefer_album
        return _preview_result()

    monkeypatch.setattr(core, "gather_matches", fake_gather)
    monkeypatch.setattr(core, "load_history", lambda path: {})
    client.post("/preview", data={"setlist": "abc"})
    assert captured["prefer_album"] is None


# --- /attended (browse a user's "I was there" shows) -----------------------

def test_attended_get_prefills_username(client, monkeypatch):
    monkeypatch.setenv("SETLISTFM_USER", "thejames")
    resp = client.get("/attended")
    assert resp.status_code == 200
    assert 'value="thejames"' in resp.data.decode()


def test_attended_post_lists_shows_with_history_crossref(client, monkeypatch):
    shows = [
        {"id": "new1", "url": "http://sl/new1", "artist": "Primus",
         "venue": "TD", "city": "Charlotte", "date": "2026-06-16"},
        {"id": "old1", "url": "http://sl/old1", "artist": "Phish",
         "venue": "MSG", "city": "New York", "date": "2025-12-31"},
    ]
    monkeypatch.setattr(core, "fetch_attended", lambda u, k: shows)
    monkeypatch.setattr(core, "load_history",
                        lambda path: {"old1": {"playlist_name": "Phish - MSG",
                                               "playlist_rating_key": 999}})
    resp = client.post("/attended", data={"username": "bob"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Attended (2)" in body
    assert "Primus" in body and "Phish" in body
    assert "created ✓" in body              # old1 flagged from history
    assert "Phish - MSG" in body            # Update form carries playlist name


def test_attended_post_empty_username(client):
    resp = client.post("/attended", data={"username": ""})
    assert resp.status_code == 200
    assert "Enter a setlist.fm username." in resp.data.decode()


def test_attended_post_unknown_user(client, monkeypatch):
    def boom(username, api_key):
        raise LookupError("No setlist.fm user named 'nobody' (is it public?).")
    monkeypatch.setattr(core, "fetch_attended", boom)
    resp = client.post("/attended", data={"username": "nobody"})
    assert resp.status_code == 200
    assert "is it public" in resp.data.decode()


def test_attended_post_no_shows(client, monkeypatch):
    monkeypatch.setattr(core, "fetch_attended", lambda u, k: [])
    monkeypatch.setattr(core, "load_history", lambda path: {})
    resp = client.post("/attended", data={"username": "bob"})
    assert resp.status_code == 200
    assert "No attended shows found" in resp.data.decode()

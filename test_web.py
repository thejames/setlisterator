"""Tests for the Flask web interface.

The core pipeline (load_config / gather_matches / builder.materialize / search)
is monkeypatched so these exercise routing and rendering only — no network, no
Plex, no setlist.fm, no YouTube Music.
"""

import pytest

import builder as bld
import setlist_to_plex as core
import web
import ytm_service as ytm
from web import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(core, "load_config", lambda: {
        "api_key": "k", "plex_baseurl": "http://x", "plex_token": "t",
        "music_library": "Music"})
    # YouTube Music off by default (no OAuth file); tests opt in explicitly.
    monkeypatch.setenv("YTM_OAUTH_FILE", "/nonexistent/ytm_oauth.json")
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def drafts(monkeypatch):
    """In-memory draft store standing in for drafts.json."""
    store = {}
    monkeypatch.setattr(core, "load_drafts", lambda path=None: dict(store))

    def _save(data, path=None):
        store.clear()
        store.update(data)
    monkeypatch.setattr(core, "save_drafts", _save)
    return store


def _gather_result():
    """A gather_matches result shaped as builder.seed_from_setlist expects."""
    return {
        "setlist_id": "abc123",
        "show": {"artist": "Primus", "venue": "TD Amp", "city": "Charlotte",
                 "date": "2026-06-16", "url": "https://setlist.fm/x.html"},
        "playlist_name": "Primus - TD Amp, Charlotte (2026-06-16)",
        "songs": [{"position": 1}, {"position": 2}, {"position": 3}],
        "matched": [
            {"rating_key": 10, "track_title": "Tommy the Cat",
             "track_artist": "Primus", "album": "Sailing the Seas of Cheese"},
            {"rating_key": 20, "track_title": "Jerry Was a Race Car Driver",
             "track_artist": "Primus", "album": "Sailing the Seas of Cheese"}],
        "missing": [(3, "Primus", "Jilly's on Smack", "Green Naugahyde")],
        "fuzzy": [],
    }


# --- landing / chrome ------------------------------------------------------

def test_index_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Destination" in body                    # service picker
    assert 'action="/builder/seed"' in body         # seeds the builder
    assert "YouTube Music" in body                   # both destinations offered
    assert "app.js" in body


def test_index_ytm_disabled_when_unavailable(client):
    body = client.get("/").data.decode()
    assert "not configured" in body                 # YTM greyed out with reason


def test_port_default_and_override(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert web._port() == 5001
    monkeypatch.setenv("PORT", "8080")
    assert web._port() == 8080


def test_navbar_present(client):
    body = client.get("/").data.decode()
    assert "Setlist-er-ator" in body
    assert 'href="/history"' in body
    assert 'href="/buylist"' in body


def test_index_config_error(client, monkeypatch):
    def raise_cfg():
        raise core.ConfigError("Missing PLEX_TOKEN")
    monkeypatch.setattr(core, "load_config", raise_cfg)
    body = client.get("/").data.decode()
    assert "Missing PLEX_TOKEN" in body


# --- buy list --------------------------------------------------------------

def test_buylist_aggregates_and_dedupes(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {
        "a": {"missing_tracks": [
            {"artist": "Primus", "title": "Jilly's on Smack"},
            {"artist": "Primus", "title": "The Ol' Grizz"},
            {"artist": "Goose", "title": "Arrow"}]},
        "b": {"missing_tracks": [
            {"artist": "Primus", "title": "Jilly's on Smack"}]},  # dup
    })
    monkeypatch.setattr(core, "lookup_album", lambda a, t: "Pork Soda")
    monkeypatch.setattr(core, "save_history", lambda p, h: None)
    body = client.get("/buylist").data.decode()
    assert body.count("Jilly&#39;s on Smack") == 1
    assert "2 shows" in body
    assert "Goose" in body and "Primus" in body
    assert body.index("Goose") < body.index("Primus")
    assert "likely from Pork Soda" in body
    assert "The Ol&#39; Grizz" in body


def test_buylist_empty(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {})
    body = client.get("/buylist").data.decode()
    assert "Nothing to buy" in body


# --- history ---------------------------------------------------------------

def test_history_lists_entries_newest_first(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {
        "old": {"id": "old", "artist": "Phish", "date": "2023-12-31",
                "playlist_name": "Phish — MSG", "processed_at": "2026-06-20",
                "matched": 21, "missing": 0,
                "url": "https://setlist.fm/phish.html"},
        "new": {"id": "new", "artist": "Primus", "date": "2026-06-16",
                "playlist_name": "Primus — TD Amp", "processed_at": "2026-06-23",
                "matched": 8, "missing": 4, "playlist_rating_key": 500,
                "url": "https://setlist.fm/primus.html"},
    })
    body = client.get("/history").data.decode()
    assert "Phish — MSG" in body and "Primus — TD Amp" in body
    assert body.index("Primus — TD Amp") < body.index("Phish — MSG")
    # Re-open now seeds the builder with the stored URL
    assert 'action="/builder/seed"' in body
    assert 'name="setlist" value="https://setlist.fm/primus.html"' in body
    assert "4 missing" in body
    assert 'action="/builder/edit"' in body      # Edit present for Plex row with an id


def test_history_builder_entry_has_no_setlist_actions(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {
        "builder-xyz": {"id": "builder-xyz", "playlist_name": "Road Trip",
                        "processed_at": "2026-07-01", "matched": 12,
                        "service": "ytm", "source": "builder"},
    })
    body = client.get("/history").data.decode()
    assert "Road Trip" in body
    assert "Built from scratch" in body           # Show column labeled, not blank ()
    assert "()" not in body                        # no empty artist/date placeholder
    assert "built in YouTube Music" in body
    assert 'action="/builder/seed"' not in body   # no Re-open for a from-scratch entry


def test_history_empty(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {})
    body = client.get("/history").data.decode()
    assert "No history yet" in body


# --- connect YouTube Music -------------------------------------------------

def test_index_shows_connect_link_when_ytm_unavailable(client, monkeypatch):
    monkeypatch.setattr(ytm, "browser_connect_available", lambda: True)
    body = client.get("/").data.decode()
    assert 'href="/ytm/connect"' in body          # YTM off (fixture) → offer connect


def test_ytm_connect_lists_sources_with_accounts(client, monkeypatch):
    monkeypatch.setattr(ytm, "browser_connect_available", lambda: True)
    monkeypatch.setattr(ytm, "list_ytm_sources",
                        lambda: [{"id": "safari", "label": "Safari"},
                                 {"id": "chrome:Profile 3", "label": "Chrome — Profile 3"}])
    monkeypatch.setattr(ytm, "ytm_source_account",
                        lambda sid: "James Zambon" if sid == "safari" else None)
    body = client.get("/ytm/connect").data.decode()
    assert "James Zambon" in body                 # resolved account name
    assert "Safari" in body and "Chrome — Profile 3" in body
    assert 'value="safari"' in body               # source id to submit


def test_ytm_connect_unavailable_without_deps(client, monkeypatch):
    monkeypatch.setattr(ytm, "browser_connect_available", lambda: False)
    body = client.get("/ytm/connect").data.decode()
    assert "browser_cookie3" in body


def test_ytm_connect_save_writes_and_redirects(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(ytm, "connect_ytm_source",
                        lambda sid: seen.setdefault("id", sid))
    resp = client.post("/ytm/connect", data={"source": "safari"})
    assert resp.status_code == 302
    assert seen["id"] == "safari"


def test_ytm_connect_save_requires_source(client):
    resp = client.post("/ytm/connect", data={})
    assert resp.status_code == 400


# --- builder: seeding ------------------------------------------------------

def test_seed_empty_creates_draft_and_redirects(client, drafts):
    resp = client.post("/builder/seed", data={"service": "plex", "name": "Mix"})
    assert resp.status_code == 302
    assert len(drafts) == 1
    draft = next(iter(drafts.values()))
    assert draft["service"] == "plex"
    assert draft["name"] == "Mix"
    assert draft["tracks"] == []
    assert f"/builder/{draft['id']}" in resp.headers["Location"]


def test_seed_from_setlist_populates_tracks(client, drafts, monkeypatch):
    monkeypatch.setattr(core, "parse_setlist_id", lambda s: "abc123")
    monkeypatch.setattr(core, "gather_matches", lambda *a, **k: _gather_result())
    resp = client.post("/builder/seed",
                       data={"service": "plex", "setlist": "abc123"})
    assert resp.status_code == 302
    draft = next(iter(drafts.values()))
    assert [t["track_id"] for t in draft["tracks"]] == ["10", "20"]
    assert draft["seed"]["missing_tracks"][0]["title"] == "Jilly's on Smack"


def test_seed_ytm_unavailable_errors(client, drafts):
    resp = client.post("/builder/seed", data={"service": "ytm"})
    body = resp.data.decode()
    assert "YouTube Music" in body
    assert len(drafts) == 0                       # nothing persisted


def test_seed_setlist_error(client, drafts, monkeypatch):
    monkeypatch.setattr(core, "parse_setlist_id", lambda s: "abc123")

    def boom(*a, **k):
        raise core.SetlistError("no songs")
    monkeypatch.setattr(core, "gather_matches", boom)
    body = client.post("/builder/seed",
                       data={"service": "plex", "setlist": "abc"}).data.decode()
    assert "no songs" in body


# --- builder: open / mutate ------------------------------------------------

def _seed(store, service="plex", tracks=()):
    draft = core.new_draft(service, name="Mix")
    draft["tracks"] = [dict(t) for t in tracks]
    store[draft["id"]] = draft
    return draft


def test_builder_open_renders(client, drafts):
    d = _seed(drafts, tracks=[{"track_id": "10", "title": "Tommy the Cat",
                               "artist": "Primus", "album": "Seas"}])
    body = client.get(f"/builder/{d['id']}").data.decode()
    assert "Tommy the Cat" in body
    assert "Save to Plex" in body
    assert 'id="tracklist"' in body


def test_builder_open_missing_draft(client, drafts):
    body = client.get("/builder/nope").data.decode()
    assert "Draft not found" in body


def test_builder_search_returns_fragment(client, drafts, monkeypatch):
    d = _seed(drafts)
    monkeypatch.setattr(bld, "search", lambda cfg, svc, q: [
        {"track_id": "99", "title": "Wilson", "artist": "Phish", "album": "Junta"}])
    body = client.get(f"/builder/{d['id']}/search?q=wilson").data.decode()
    assert "Wilson" in body
    assert 'name="track_id" value="99"' in body
    assert "+ Add" in body


def test_builder_search_empty_query_clears(client, drafts):
    d = _seed(drafts)
    resp = client.get(f"/builder/{d['id']}/search?q=")
    assert resp.data.decode().strip() == ""


def test_builder_add_appends_and_updates_count(client, drafts):
    d = _seed(drafts)
    resp = client.post(f"/builder/{d['id']}/add", data={
        "track_id": "42", "title": "Wilson", "artist": "Phish", "album": "Junta"})
    body = resp.data.decode()
    assert "Wilson" in body
    assert 'id="track-count"' in body and ">1<" in body   # OOB count
    assert "track</span>" in body                          # singular word swapped too
    assert drafts[d["id"]]["tracks"][0]["track_id"] == "42"


def test_builder_add_count_word_pluralizes(client, drafts):
    d = _seed(drafts, tracks=[{"track_id": "1"}])          # already one
    body = client.post(f"/builder/{d['id']}/add",
                       data={"track_id": "2"}).data.decode()
    assert ">2<" in body and "tracks</span>" in body       # word swaps to plural


def test_builder_remove(client, drafts):
    d = _seed(drafts, tracks=[{"track_id": "10", "title": "A"},
                              {"track_id": "11", "title": "B"}])
    client.post(f"/builder/{d['id']}/remove", data={"index": "0"})
    assert [t["track_id"] for t in drafts[d["id"]]["tracks"]] == ["11"]


def test_builder_reorder(client, drafts):
    d = _seed(drafts, tracks=[{"track_id": "10"}, {"track_id": "11"},
                              {"track_id": "12"}])
    client.post(f"/builder/{d['id']}/reorder", data={"order": "2,0,1"})
    assert [t["track_id"] for t in drafts[d["id"]]["tracks"]] == ["12", "10", "11"]


def test_builder_reorder_malformed_is_noop(client, drafts):
    d = _seed(drafts, tracks=[{"track_id": "10"}, {"track_id": "11"}])
    client.post(f"/builder/{d['id']}/reorder", data={"order": "0"})
    assert [t["track_id"] for t in drafts[d["id"]]["tracks"]] == ["10", "11"]


# --- builder: save / discard ----------------------------------------------

def test_builder_save_materializes_and_deletes(client, drafts, monkeypatch):
    d = _seed(drafts, tracks=[{"track_id": "10", "title": "A"}])
    seen = {}

    def fake_materialize(config, draft):
        seen["name"] = draft["name"]
        return draft["name"]
    monkeypatch.setattr(bld, "materialize", fake_materialize)

    resp = client.post(f"/builder/{d['id']}/save", data={"name": "Final Mix"})
    body = resp.data.decode()
    assert "Playlist created in Plex" in body
    assert seen["name"] == "Final Mix"            # edited name carried through
    assert d["id"] not in drafts                   # draft consumed


def test_builder_save_ytm_label(client, drafts, monkeypatch):
    d = _seed(drafts, service="ytm", tracks=[{"track_id": "v1"}])
    monkeypatch.setattr(bld, "materialize", lambda c, dr: dr["name"])
    body = client.post(f"/builder/{d['id']}/save", data={"name": "YT"}).data.decode()
    assert "Playlist created in YouTube Music" in body


def test_builder_save_empty_rejected(client, drafts):
    d = _seed(drafts)                              # no tracks
    resp = client.post(f"/builder/{d['id']}/save", data={"name": "Mix"})
    assert resp.status_code == 400
    assert "Add at least one track" in resp.data.decode()
    assert d["id"] in drafts                        # not consumed on failure


def test_builder_save_service_error(client, drafts, monkeypatch):
    d = _seed(drafts, tracks=[{"track_id": "10"}])

    def boom(config, draft):
        raise core.PlexError("Plex went away")
    monkeypatch.setattr(bld, "materialize", boom)
    body = client.post(f"/builder/{d['id']}/save",
                       data={"name": "Renamed Mix"}).data.decode()
    assert "Plex went away" in body
    assert d["id"] in drafts                        # kept so the user can retry
    assert drafts[d["id"]]["name"] == "Renamed Mix"  # rename survived the failure


def test_builder_discard(client, drafts):
    d = _seed(drafts, tracks=[{"track_id": "10"}])
    resp = client.post(f"/builder/{d['id']}/discard")
    assert resp.status_code == 302
    assert d["id"] not in drafts


# --- editing an existing playlist (Edit / Delete) --------------------------

def test_history_editable_plex_row_has_edit_and_delete(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {
        "abc": {"id": "abc", "service": "plex", "source": "setlist",
                "playlist_name": "Primus — TD Amp", "playlist_rating_key": 500,
                "artist": "Primus", "date": "2026-06-16", "matched": 10,
                "processed_at": "2026-07-01"}})
    body = client.get("/history").data.decode()
    assert 'action="/builder/edit"' in body
    assert 'name="playlist_id" value="500"' in body
    assert "/playlist/delete?" in body


def test_history_ytm_row_not_editable(client, monkeypatch):
    monkeypatch.setattr(core, "load_history", lambda path: {
        "z": {"id": "z", "service": "ytm", "source": "setlist",
              "playlist_name": "Zakk", "playlist_rating_key": "PL1",
              "processed_at": "2026-07-01"}})
    body = client.get("/history").data.decode()
    assert 'action="/builder/edit"' not in body      # YTM edit deferred
    assert "/playlist/delete?" not in body


def test_builder_edit_opens_draft(client, drafts, monkeypatch):
    monkeypatch.setattr(bld, "draft_from_playlist", lambda cfg, svc, pid:
                        core.new_draft(svc, name="My Mix") | {
                            "target_playlist_id": pid})
    resp = client.post("/builder/edit",
                       data={"service": "plex", "playlist_id": "500"})
    assert resp.status_code == 302
    draft = next(iter(drafts.values()))
    assert draft["target_playlist_id"] == "500"


def test_builder_edit_playlist_gone(client, drafts, monkeypatch):
    def boom(cfg, svc, pid):
        raise core.PlexError("no longer exists")
    monkeypatch.setattr(bld, "draft_from_playlist", boom)
    body = client.post("/builder/edit",
                       data={"service": "plex", "playlist_id": "500"}).data.decode()
    assert "no longer exists" in body


def test_builder_save_edit_applies_and_deletes(client, drafts, monkeypatch):
    d = _seed(drafts, tracks=[{"track_id": "10", "item_id": "i1"}])
    d["target_playlist_id"] = "500"
    seen = {}

    def fake_apply(config, draft):
        seen["id"] = draft["target_playlist_id"]
        return (draft["name"], {"added": 1, "removed": 2})
    monkeypatch.setattr(bld, "apply_edits", fake_apply)

    body = client.post(f"/builder/{d['id']}/save", data={"name": "Mix"}).data.decode()
    assert "Playlist updated in Plex" in body
    assert "removed" in body                         # stats shown
    assert seen["id"] == "500"
    assert d["id"] not in drafts


def test_delete_confirm_page(client):
    body = client.get("/playlist/delete",
                      query_string={"service": "plex", "id": "500",
                                    "name": "My Mix"}).data.decode()
    assert "Delete this playlist?" in body
    assert "My Mix" in body
    assert 'value="500"' in body


def test_delete_executes_and_redirects(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(bld, "delete_playlist",
                        lambda cfg, svc, pid: seen.update(svc=svc, pid=pid))
    resp = client.post("/playlist/delete", data={"service": "plex", "id": "500"})
    assert resp.status_code == 302
    assert seen == {"svc": "plex", "pid": "500"}


# --- attended --------------------------------------------------------------

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
    assert "created ✓" in body
    assert 'action="/builder/seed"' in body        # Build/Re-open seed the builder
    assert 'action="/builder/edit"' in body        # prior Plex playlist is editable
    assert 'name="playlist_id" value="999"' in body


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

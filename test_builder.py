"""Tests for the playlist-builder business logic (offline)."""

import pytest

import builder as b
import setlist_to_plex as core


_CONFIG = {"api_key": "k", "plex_baseurl": "http://x", "plex_token": "t",
           "music_library": "Music", "ytm_oauth_path": "/tmp/o.json"}


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------

def _gather_result():
    return {
        "setlist_id": "abc123",
        "show": {"artist": "Phish", "date": "2023-12-31",
                 "url": "https://setlist.fm/x"},
        "playlist_name": "Phish - MSG",
        "songs": [{"position": 1}, {"position": 2}, {"position": 3}],
        "matched": [
            {"rating_key": 10, "track_title": "Wilson",
             "track_artist": "Phish", "album": "Junta"},
            {"rating_key": 11, "track_title": "Tweezer",
             "track_artist": "Phish", "album": "A Live One"}],
        "missing": [(3, "Phish", "Some Rarity", "Rift")],
        "fuzzy": [],
    }


def test_seed_from_setlist_maps_matches_and_seed(monkeypatch):
    monkeypatch.setattr(core, "parse_setlist_id", lambda s: "abc123")
    monkeypatch.setattr(core, "gather_matches",
                        lambda *a, **k: _gather_result())
    draft = b.seed_from_setlist(_CONFIG, "plex", "abc123")

    assert draft["service"] == "plex"
    assert draft["name"] == "Phish - MSG"
    assert [t["track_id"] for t in draft["tracks"]] == ["10", "11"]
    assert draft["tracks"][0]["album"] == "Junta"
    assert draft["seed"]["setlist_id"] == "abc123"
    assert draft["seed"]["song_count"] == 3
    assert draft["seed"]["missing_tracks"] == [
        {"position": 3, "artist": "Phish", "title": "Some Rarity",
         "album": "Rift"}]


def test_seed_passes_service_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(core, "parse_setlist_id", lambda s: "abc123")

    def fake_gather(config, sid, name=None, prefer_album=None, service=None):
        seen["service"] = service
        return _gather_result()
    monkeypatch.setattr(core, "gather_matches", fake_gather)

    b.seed_from_setlist(_CONFIG, "ytm", "abc123")
    assert seen["service"].name == "ytm"


def test_empty_draft():
    draft = b.empty_draft("ytm", name="  Road Trip  ")
    assert draft["service"] == "ytm"
    assert draft["name"] == "Road Trip"
    assert draft["tracks"] == []
    assert draft["seed"] is None


def test_empty_draft_default_name():
    assert b.empty_draft("plex")["name"] == "New playlist"


# ---------------------------------------------------------------------------
# mutation
# ---------------------------------------------------------------------------

def test_add_track():
    draft = b.empty_draft("plex")
    b.add_track(draft, {"track_id": 10, "title": "Wilson",
                        "artist": "Phish", "album": "Junta"})
    assert draft["tracks"] == [
        {"track_id": "10", "title": "Wilson", "artist": "Phish",
         "album": "Junta"}]


def test_remove_track():
    draft = b.empty_draft("plex")
    for i in range(3):
        b.add_track(draft, {"track_id": i, "title": f"T{i}"})
    b.remove_track(draft, 1)
    assert [t["track_id"] for t in draft["tracks"]] == ["0", "2"]


def test_remove_track_out_of_range_is_noop():
    draft = b.empty_draft("plex")
    b.add_track(draft, {"track_id": 1})
    b.remove_track(draft, 5)
    assert len(draft["tracks"]) == 1


def test_reorder_tracks():
    draft = b.empty_draft("plex")
    for i in range(3):
        b.add_track(draft, {"track_id": i})
    b.reorder_tracks(draft, [2, 0, 1])
    assert [t["track_id"] for t in draft["tracks"]] == ["2", "0", "1"]


def test_reorder_ignores_malformed_permutation():
    draft = b.empty_draft("plex")
    for i in range(3):
        b.add_track(draft, {"track_id": i})
    b.reorder_tracks(draft, [0, 1])          # too short — drops a track if applied
    assert [t["track_id"] for t in draft["tracks"]] == ["0", "1", "2"]
    b.reorder_tracks(draft, [0, 1, 1])       # duplicate index
    assert [t["track_id"] for t in draft["tracks"]] == ["0", "1", "2"]


# ---------------------------------------------------------------------------
# search dispatch (one path serves both services via the section shim)
# ---------------------------------------------------------------------------

class _FakeTrack:
    def __init__(self, rk, title, artist, album):
        self.ratingKey = rk
        self.title = title
        self.grandparentTitle = artist
        self.parentTitle = album


class _FakeSection:
    def searchTracks(self, title=None, maxresults=25):
        return [_FakeTrack("42", "Wilson", "Phish", "Junta")]


def test_search_maps_tracks(monkeypatch):
    monkeypatch.setattr(b, "service_for", lambda name: _FakeServiceStub())
    rows = b.search(_CONFIG, "plex", "wilson")
    assert rows == [{"track_id": "42", "title": "Wilson",
                     "artist": "Phish", "album": "Junta"}]


class _FakeServiceStub:
    name = "plex"

    def connect(self, config):
        return object(), _FakeSection()


# ---------------------------------------------------------------------------
# materialize / history metadata
# ---------------------------------------------------------------------------

def test_materialize_plex_dispatches_to_create_playlist(monkeypatch):
    calls = {}

    def fake_create(config, name, ids, meta):
        calls["ids"] = ids
        calls["meta"] = meta
        return name
    monkeypatch.setattr(core, "create_playlist", fake_create)

    draft = b.empty_draft("plex", name="Mix")
    b.add_track(draft, {"track_id": 10})
    b.add_track(draft, {"track_id": 11})
    name = b.materialize(_CONFIG, draft)

    assert name == "Mix"
    assert calls["ids"] == ["10", "11"]
    assert calls["meta"]["source"] == "builder"      # light entry for from-scratch
    assert calls["meta"]["id"] == f"builder-{draft['id']}"


def test_materialize_ytm_dispatches_to_ytm(monkeypatch):
    import ytm_service as ytm
    calls = {}

    def fake_create(config, name, ids, meta):
        calls["ids"] = ids
        return name
    monkeypatch.setattr(ytm, "create_playlist_ytm", fake_create)

    draft = b.empty_draft("ytm", name="YT Mix")
    b.add_track(draft, {"track_id": "v1"})
    name = b.materialize(_CONFIG, draft)

    assert name == "YT Mix"
    assert calls["ids"] == ["v1"]


def test_history_meta_setlist_source(monkeypatch):
    monkeypatch.setattr(core, "parse_setlist_id", lambda s: "abc123")
    monkeypatch.setattr(core, "gather_matches", lambda *a, **k: _gather_result())
    draft = b.seed_from_setlist(_CONFIG, "plex", "abc123")
    meta = b._history_meta(draft)
    assert meta["source"] == "setlist"
    assert meta["id"] == "abc123"
    assert meta["missing"] == 1


# ---------------------------------------------------------------------------
# editing an existing playlist (dispatch)
# ---------------------------------------------------------------------------

def test_draft_from_playlist_builds_edit_draft(monkeypatch):
    monkeypatch.setattr(core, "open_playlist", lambda cfg, pid: {
        "name": "My Mix",
        "tracks": [{"track_id": "10", "item_id": "i1", "title": "A",
                    "artist": "Phish", "album": "Junta"}]})
    draft = b.draft_from_playlist(_CONFIG, "plex", "500")
    assert draft["service"] == "plex"
    assert draft["name"] == "My Mix"
    assert draft["target_playlist_id"] == "500"
    assert draft["seed"] is None
    assert draft["tracks"][0]["item_id"] == "i1"


def test_open_for_edit_ytm_raises(monkeypatch):
    import ytm_service as ytm
    with pytest.raises(ytm.YTMError):
        b.open_for_edit(_CONFIG, "ytm", "PL123")


def test_apply_edits_dispatches_to_plex(monkeypatch):
    seen = {}
    monkeypatch.setattr(core, "apply_playlist_edits",
                        lambda cfg, pid, name, rows: seen.update(
                            pid=pid, name=name, rows=rows) or ("New", {"added": 1}))
    draft = core.new_draft("plex", name="New")
    draft["target_playlist_id"] = "500"
    draft["tracks"] = [{"track_id": "10", "item_id": "i1"}, {"track_id": "20"}]
    name, stats = b.apply_edits(_CONFIG, draft)
    assert name == "New" and stats == {"added": 1}
    assert seen["pid"] == "500"
    assert seen["rows"] == draft["tracks"]


def test_apply_edits_without_target_raises():
    draft = core.new_draft("plex", name="x")   # target_playlist_id is None
    with pytest.raises(ValueError):
        b.apply_edits(_CONFIG, draft)


def test_apply_edits_ytm_raises():
    import ytm_service as ytm
    draft = core.new_draft("ytm", name="x")
    draft["target_playlist_id"] = "PL1"
    with pytest.raises(ytm.YTMError):
        b.apply_edits(_CONFIG, draft)


def test_delete_playlist_dispatches(monkeypatch):
    monkeypatch.setattr(core, "delete_playlist", lambda cfg, pid: f"deleted-{pid}")
    assert b.delete_playlist(_CONFIG, "plex", "500") == "deleted-500"


def test_delete_playlist_ytm_raises():
    import ytm_service as ytm
    with pytest.raises(ytm.YTMError):
        b.delete_playlist(_CONFIG, "ytm", "PL1")

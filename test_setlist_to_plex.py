"""Unit tests for the pure logic in setlist_to_plex.

These cover URL/ID parsing, title/artist normalization, setlist flattening,
playlist-name generation, and the unique-name suffixing — i.e. everything that
doesn't require a live Plex server or setlist.fm key. Run with: pytest
"""

import pytest

import setlist_to_plex as m


# ---------------------------------------------------------------------------
# parse_setlist_id
# ---------------------------------------------------------------------------

def test_parse_bare_id():
    assert m.parse_setlist_id("63de4613") == "63de4613"


def test_parse_bare_id_strips_whitespace():
    assert m.parse_setlist_id("  63de4613  ") == "63de4613"


def test_parse_full_url():
    url = ("https://www.setlist.fm/setlist/phish/2023/"
           "madison-square-garden-new-york-ny-63de4613.html")
    assert m.parse_setlist_id(url) == "63de4613"


def test_parse_http_url_without_setlistfm_host():
    # Anything starting with http is treated as a URL and must contain an id.
    url = "http://example.com/whatever-7a3f10bc.html"
    assert m.parse_setlist_id(url) == "7a3f10bc"


def test_parse_url_without_id_raises():
    with pytest.raises(ValueError):
        m.parse_setlist_id("https://www.setlist.fm/setlist/no-id-here.htm")


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

def test_normalize_simple_strips_punctuation_and_case():
    assert m.normalize_simple("Run Like an Antelope!!!") == "run like an antelope"


def test_normalize_simple_collapses_whitespace():
    assert m.normalize_simple("  Down   with   Disease ") == "down with disease"


def test_normalize_simple_empty():
    assert m.normalize_simple("") == ""
    assert m.normalize_simple(None) == ""


def test_aggressive_drops_parenthetical():
    assert m.normalize_aggressive("Tweezer (Reprise)") == "tweezer"


def test_aggressive_drops_brackets():
    assert m.normalize_aggressive("Sand [Remastered 2020]") == "sand"


def test_aggressive_drops_featured_artist():
    assert m.normalize_aggressive("Song feat. Trey") == "song"
    assert m.normalize_aggressive("Song ft Mike") == "song"
    assert m.normalize_aggressive("Song featuring Page") == "song"


def test_aggressive_drops_leading_article():
    assert m.normalize_aggressive("The Lizards") == "lizards"


def test_aggressive_pt_equals_part():
    assert m.normalize_aggressive("Wilson, Pt. 2") == m.normalize_aggressive("Wilson Part 2")


def test_aggressive_and_equals_ampersand():
    assert m.normalize_aggressive("Punch You in the Eye and More") == \
        m.normalize_aggressive("Punch You in the Eye & More")


# ---------------------------------------------------------------------------
# artists_match
# ---------------------------------------------------------------------------

def test_artists_match_case_insensitive():
    assert m.artists_match("Phish", "phish")


def test_artists_match_ignores_leading_the():
    assert m.artists_match("The Beatles", "Beatles")


def test_artists_match_rejects_different():
    assert not m.artists_match("Phish", "Goose")


# ---------------------------------------------------------------------------
# extract_show
# ---------------------------------------------------------------------------

def _sample_setlist():
    return {
        "artist": {"name": "Phish"},
        "venue": {"name": "MSG", "city": {"name": "New York"}},
        "eventDate": "31-12-2023",
        "url": "https://www.setlist.fm/setlist/phish/2023/msg-abc123.html",
        "sets": {"set": [
            {"song": [
                {"name": "Walk-in music", "tape": True},
                {"name": "Wilson"},
                {"name": "  "},
            ]},
            {"encore": 1, "song": [{"name": "Tweezer Reprise"}]},
        ]},
    }


def test_extract_show_fields():
    show = m.extract_show(_sample_setlist())
    assert show["artist"] == "Phish"
    assert show["venue"] == "MSG"
    assert show["city"] == "New York"
    assert show["date"] == "2023-12-31"  # reformatted from DD-MM-YYYY to ISO
    assert show["url"] == "https://www.setlist.fm/setlist/phish/2023/msg-abc123.html"


def test_extract_show_date_passthrough_when_unparseable():
    show = m.extract_show({"eventDate": "sometime in 1995"})
    assert show["date"] == "sometime in 1995"


def test_extract_show_flattens_and_skips_tape_and_empty():
    show = m.extract_show(_sample_setlist())
    assert show["songs"] == ["Wilson", "Tweezer Reprise"]


def test_extract_show_tolerates_bare_set_key():
    data = {
        "artist": {"name": "Goose"},
        "venue": {"name": "Cap", "city": {"name": "Port Chester"}},
        "eventDate": "01-01-2024",
        "set": [{"song": [{"name": "Arrow"}]}],
    }
    show = m.extract_show(data)
    assert show["songs"] == ["Arrow"]


def test_extract_show_handles_missing_fields():
    show = m.extract_show({})
    assert show["artist"] == "Unknown Artist"
    assert show["venue"] == "Unknown Venue"
    assert show["city"] == "Unknown City"
    assert show["date"] == ""
    assert show["url"] == ""
    assert show["songs"] == []


def test_build_playlist_name():
    show = m.extract_show(_sample_setlist())
    assert m.build_playlist_name(show) == "Phish - MSG, New York (2023-12-31)"


# ---------------------------------------------------------------------------
# unique_playlist_name (uses a tiny stub instead of a live server)
# ---------------------------------------------------------------------------

class _FakePlaylist:
    def __init__(self, title):
        self.title = title


class _FakePlex:
    def __init__(self, titles):
        self._titles = titles

    def playlists(self):
        return [_FakePlaylist(t) for t in self._titles]


def test_unique_name_no_collision():
    plex = _FakePlex(["Other"])
    assert m.unique_playlist_name(plex, "My Show") == "My Show"


def test_unique_name_single_collision():
    plex = _FakePlex(["My Show"])
    assert m.unique_playlist_name(plex, "My Show") == "My Show (2)"


def test_unique_name_multiple_collisions():
    plex = _FakePlex(["My Show", "My Show (2)", "My Show (3)"])
    assert m.unique_playlist_name(plex, "My Show") == "My Show (4)"


class _TitlelessTag:
    """Stand-in for the titleless plexapi Tag object that /playlists can
    yield on some servers (see unique_playlist_name)."""
    def __getattr__(self, name):
        if name == "title":
            raise AttributeError("'Tag' object has no attribute 'title'")
        raise AttributeError(name)


def test_unique_name_ignores_titleless_objects():
    # A stray titleless object must not crash collision detection.
    plex = _FakePlex(["My Show"])
    plex._titles = None  # bypass the title-based stub below
    plex.playlists = lambda: [_FakePlaylist("My Show"), _TitlelessTag()]
    assert m.unique_playlist_name(plex, "My Show") == "My Show (2)"


# ---------------------------------------------------------------------------
# match_song ranking (stubbed library section + tracks)
# ---------------------------------------------------------------------------

class _FakeTrack:
    def __init__(self, title, artist, rating_key=None, album="Some Album"):
        self.title = title
        self.grandparentTitle = artist
        self.originalTitle = None
        self.parentTitle = album
        self.ratingKey = rating_key


class _FakeArtist:
    def __init__(self, title, tracks=()):
        self.title = title
        self._tracks = list(tracks)

    def tracks(self):
        return list(self._tracks)


class _FakeSection:
    """Stub library section for the global-search fallback path."""

    def __init__(self, tracks=(), artists=()):
        self._tracks = list(tracks)
        self._artists = list(artists)

    def searchTracks(self, title=None, maxresults=None):
        return list(self._tracks)

    def searchArtists(self, title=None, maxresults=None):
        return list(self._artists)


# --- global-fallback path (no artist_tracks) -------------------------------

def test_match_song_prefers_exact_artist_and_title():
    section = _FakeSection([
        _FakeTrack("Wilson", "Ween"),       # title match, wrong artist
        _FakeTrack("Wilson", "Phish"),      # exact
    ])
    match = m.match_song(section, "Wilson", "Phish", [])
    assert match.quality == "exact"
    assert match.source == "global"
    assert match.track.grandparentTitle == "Phish"


def test_match_song_exact_ignores_parenthetical_punctuation():
    section = _FakeSection([_FakeTrack("Tweezer (Reprise)", "Phish")])
    match = m.match_song(section, "Tweezer Reprise", "Phish", [])
    assert match.quality == "exact"


def test_match_song_fuzzy_on_abbreviation():
    section = _FakeSection([_FakeTrack("Wilson, Pt. 2", "Phish")])
    match = m.match_song(section, "Wilson Part 2", "Phish", [])
    assert match.quality == "fuzzy"
    assert match.tier == "loose"


def test_match_song_fuzzy_when_artist_differs():
    section = _FakeSection([_FakeTrack("Loving Cup", "The Rolling Stones")])
    match = m.match_song(section, "Loving Cup", "Phish", [])
    assert match.quality == "fuzzy"


def test_match_song_returns_none_when_no_match():
    section = _FakeSection([_FakeTrack("Totally Different", "Nobody")])
    assert m.match_song(section, "Wilson", "Phish", []) is None


# --- artist-scoped path (Part A) -------------------------------------------

def test_scoped_match_survives_unicode_hyphen():
    # Real bug #1: setlist sends ASCII '-' (U+002D); library has '‐' (U+2010).
    # Plex search misses it, but local scoped matching normalizes both away.
    library = [_FakeTrack("Those Damned Blue‐Collar Tweekers", "Primus")]
    section = _FakeSection()  # global search returns nothing
    match = m.match_song(
        section, "Those Damned Blue-Collar Tweekers", "Primus", library)
    assert match.quality == "exact"
    assert match.source == "scoped"


def test_scoped_match_handles_medley_segment():
    # Real bug #7: library track is a medley; setlist lists one song of it.
    library = [_FakeTrack("Hello Skinny / Constantinople", "Primus")]
    section = _FakeSection()
    match = m.match_song(section, "Hello Skinny", "Primus", library)
    assert match.quality == "fuzzy"
    assert match.tier == "medley"


def test_scoped_match_handles_trailing_words():
    library = [_FakeTrack("Tommy the Cat - Live in Charlotte", "Primus")]
    section = _FakeSection()
    match = m.match_song(section, "Tommy the Cat", "Primus", library)
    assert match.quality == "fuzzy"
    assert match.tier == "prefix"


def test_prefix_tier_requires_multiword_target():
    # A single-word target must not prefix-match an unrelated longer title.
    library = [_FakeTrack("Bobby Brown Stomp", "Primus")]
    section = _FakeSection()
    assert m.match_song(section, "Bob", "Primus", library) is None


def test_scoped_exact_beats_global():
    # Scoped match wins even when a global search would also hit.
    library = [_FakeTrack("Tommy the Cat", "Primus", rating_key=1)]
    section = _FakeSection([_FakeTrack("Tommy the Cat", "Someone Else")])
    match = m.match_song(section, "Tommy the Cat", "Primus", library)
    assert match.quality == "exact"
    assert match.source == "scoped"
    assert match.track.ratingKey == 1


# --- resolve_artist --------------------------------------------------------

def test_resolve_artist_found():
    section = _FakeSection(artists=[_FakeArtist("Goose"), _FakeArtist("Primus")])
    artist = m.resolve_artist(section, "primus")
    assert artist is not None and artist.title == "Primus"


def test_resolve_artist_not_found():
    section = _FakeSection(artists=[_FakeArtist("Goose")])
    assert m.resolve_artist(section, "Primus") is None


# --- medley splitting ------------------------------------------------------

def test_split_medley_variants():
    assert m._split_medley("A / B") == ["A", "B"]
    assert m._split_medley("A > B ; C") == ["A", "B", "C"]
    assert m._split_medley("Single Song") == ["Single Song"]


# ---------------------------------------------------------------------------
# Processed-setlist history
# ---------------------------------------------------------------------------

def test_history_path_honors_override(monkeypatch):
    monkeypatch.setenv("SETLIST_TO_PLEX_HISTORY", "/tmp/custom-history.json")
    assert m.history_path() == m.Path("/tmp/custom-history.json")


def test_history_path_defaults_to_xdg(monkeypatch):
    monkeypatch.delenv("SETLIST_TO_PLEX_HISTORY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert m.history_path() == m.Path("/tmp/xdg/setlist_to_plex/history.json")


def test_load_history_missing_file_returns_empty(tmp_path):
    assert m.load_history(tmp_path / "nope.json") == {}


def test_load_history_corrupt_file_returns_empty(tmp_path):
    bad = tmp_path / "history.json"
    bad.write_text("{not json", encoding="utf-8")
    assert m.load_history(bad) == {}


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "nested" / "history.json"  # parent created on save
    data = {"abc123": {"playlist_name": "Test", "matched": 5}}
    m.save_history(path, data)
    assert m.load_history(path) == data


def test_should_process_new_id():
    assert m.should_process({}, "abc", force=False, is_tty=False) is True


def test_should_process_force_overrides_history():
    history = {"abc": {"playlist_name": "X"}}
    assert m.should_process(history, "abc", force=True, is_tty=False) is True


def test_should_process_non_tty_skips_known_id():
    history = {"abc": {"playlist_name": "X", "processed_at": "2026-06-22"}}
    assert m.should_process(history, "abc", force=False, is_tty=False) is False


def test_should_process_tty_prompts_yes():
    history = {"abc": {"playlist_name": "X"}}
    answered = m.should_process(history, "abc", force=False, is_tty=True,
                                prompt_fn=lambda _: "y")
    assert answered is True


def test_should_process_tty_prompts_no():
    history = {"abc": {"playlist_name": "X"}}
    answered = m.should_process(history, "abc", force=False, is_tty=True,
                                prompt_fn=lambda _: "")
    assert answered is False


# --- print_history / --history flag ----------------------------------------

def test_print_history_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(m, "history_path", lambda: tmp_path / "none.json")
    m.print_history()
    assert "No history yet" in capsys.readouterr().out


def test_print_history_lists_newest_first(monkeypatch, tmp_path, capsys):
    hist = tmp_path / "history.json"
    m.save_history(hist, {
        "a": {"artist": "Phish", "date": "2023-12-31", "playlist_name": "MSG",
              "processed_at": "2026-06-20", "matched": 21, "missing": 0},
        "b": {"artist": "Primus", "date": "2026-06-16", "playlist_name": "TD Amp",
              "processed_at": "2026-06-23", "matched": 8, "missing": 4},
    })
    monkeypatch.setattr(m, "history_path", lambda: hist)
    m.print_history()
    out = capsys.readouterr().out
    assert "Phish" in out and "Primus" in out
    assert out.index("Primus") < out.index("Phish")   # newest first
    assert "4 missing" in out


def test_main_history_flag_needs_no_config(monkeypatch, tmp_path, capsys):
    # --history works with no setlist and no Plex/setlist.fm config.
    monkeypatch.setattr(m, "history_path", lambda: tmp_path / "none.json")
    assert m.main(["--history"]) == m.EXIT_OK
    assert "No history yet" in capsys.readouterr().out


def test_main_requires_setlist_or_history():
    with pytest.raises(SystemExit):   # argparse error -> exit
        m.main([])


# --- backfill_history / --backfill -----------------------------------------

def test_backfill_history_fills_count_only_entries(monkeypatch, tmp_path):
    hist = tmp_path / "h.json"
    m.save_history(hist, {
        "stale": {"id": "stale", "url": "http://x-aaa.html", "missing": 2,
                  "playlist_name": "Stale"},                 # count only -> fill
        "done": {"id": "done", "missing": 1, "playlist_name": "Done",
                 "missing_tracks": [{"artist": "A", "title": "B"}]},  # has detail
        "complete": {"id": "complete", "missing": 0,
                     "playlist_name": "Complete"},           # nothing missing
    })
    monkeypatch.setattr(m, "history_path", lambda: hist)
    monkeypatch.setattr(m, "gather_matches", lambda cfg, sid, name=None: {
        "missing": [(1, "Stale Artist", "Song One"),
                    (2, "Stale Artist", "Song Two")]})

    assert m.backfill_history(_CONFIG) == 1
    saved = m.load_history(hist)
    assert saved["stale"]["missing_tracks"] == [
        {"artist": "Stale Artist", "title": "Song One", "album": ""},
        {"artist": "Stale Artist", "title": "Song Two", "album": ""}]
    assert saved["stale"]["missing"] == 2
    assert saved["done"]["missing_tracks"] == [{"artist": "A", "title": "B"}]
    assert "missing_tracks" not in saved["complete"]
    assert m.backfill_history(_CONFIG) == 0   # idempotent


def test_backfill_history_skips_on_setlist_error(monkeypatch, tmp_path):
    hist = tmp_path / "h.json"
    m.save_history(hist, {"x": {"id": "x", "url": "http://x-aaa.html",
                                "missing": 1, "playlist_name": "X"}})
    monkeypatch.setattr(m, "history_path", lambda: hist)

    def boom(cfg, sid, name=None):
        raise m.SetlistError("gone")
    monkeypatch.setattr(m, "gather_matches", boom)
    assert m.backfill_history(_CONFIG) == 0          # skipped, not crashed
    assert "missing_tracks" not in m.load_history(hist)["x"]


def test_main_backfill_flag(monkeypatch, capsys):
    monkeypatch.setattr(m, "load_config", lambda: _CONFIG)
    monkeypatch.setattr(m, "backfill_history", lambda config: 3)
    assert m.main(["--backfill"]) == m.EXIT_OK
    assert "Backfilled 3 entries" in capsys.readouterr().out


# --- MusicBrainz album lookup ----------------------------------------------

_MB_CANNED = {"recordings": [
    {"releases": [
        {"release-group": {"primary-type": "Album", "secondary-types": [],
                           "title": "Studio One"}},
        {"release-group": {"primary-type": "Album", "secondary-types": ["Live"],
                           "title": "Live Album"}},
    ]},
    {"releases": [
        {"release-group": {"primary-type": "Album", "secondary-types": [],
                           "title": "Studio One"}},
        {"release-group": {"primary-type": "Album", "secondary-types": [],
                           "title": "Other Studio"}},
    ]},
]}


def test_lookup_album_picks_most_frequent_studio(monkeypatch):
    monkeypatch.setattr(m, "_mb_get", lambda a, t: _MB_CANNED)
    # Studio One appears 2x, Other Studio 1x, the live album is excluded.
    assert m.lookup_album("Band", "Song") == "Studio One"


def test_lookup_album_empty_when_only_live_or_no_data(monkeypatch):
    monkeypatch.setattr(m, "_mb_get", lambda a, t: {"recordings": [{"releases": [
        {"release-group": {"primary-type": "Album", "secondary-types": ["Live"],
                           "title": "L"}}]}]})
    assert m.lookup_album("Band", "Song") == ""
    monkeypatch.setattr(m, "_mb_get", lambda a, t: None)   # offline / error
    assert m.lookup_album("Band", "Song") == ""


def test_lookup_album_requires_artist_and_title():
    assert m.lookup_album("", "X") == ""
    assert m.lookup_album("X", "") == ""


def test_enrich_missing_albums_caches_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(m, "lookup_album", lambda a, t: "Found Album")
    history = {"e": {"missing_tracks": [
        {"artist": "A", "title": "B"},
        {"artist": "A", "title": "C", "album": "Already"},  # cached -> skip
    ]}}
    assert m.enrich_missing_albums(history) is True
    tracks = history["e"]["missing_tracks"]
    assert tracks[0]["album"] == "Found Album"
    assert tracks[1]["album"] == "Already"                  # untouched
    assert m.enrich_missing_albums(history) is False        # all cached now


def test_enrich_missing_albums_respects_limit(monkeypatch):
    monkeypatch.setattr(m, "lookup_album", lambda a, t: "X")
    history = {"e": {"missing_tracks":
                     [{"artist": "A", "title": str(i)} for i in range(5)]}}
    m.enrich_missing_albums(history, limit=2)
    assert sum("album" in t for t in history["e"]["missing_tracks"]) == 2


def test_enrich_skips_when_album_already_set(monkeypatch):
    # setlist.fm already supplied the album -> MusicBrainz isn't consulted.
    calls = []
    monkeypatch.setattr(m, "lookup_album", lambda a, t: calls.append(1) or "MB")
    history = {"e": {"missing_tracks": [{"artist": "A", "title": "B",
                                         "album": "From setlist.fm"}]}}
    assert m.enrich_missing_albums(history) is False
    assert calls == []                                    # no MB lookup


def test_enrich_caches_musicbrainz_no_match(monkeypatch):
    # Empty album -> MB tried once; a no-match is cached (album_checked).
    calls = []
    monkeypatch.setattr(m, "lookup_album", lambda a, t: calls.append(1) or "")
    history = {"e": {"missing_tracks": [{"artist": "A", "title": "B"}]}}
    assert m.enrich_missing_albums(history) is True
    assert history["e"]["missing_tracks"][0]["album_checked"] is True
    assert m.enrich_missing_albums(history) is False
    assert len(calls) == 1


# --- setlist.fm "Songs on Albums" scrape ------------------------------------

_ALBUM_HTML = """
<div class="setlistAlbumStats"><div class="col-xs-12"><h2>Songs on Albums</h2></div>
<ul class="noList listStriped listPadding">
<li><div><i class="fa fa-circle" style="color:#85B146;"></i>
  <a href="javascript:void(0);" id="id11" rel="nofollow">Frizzle Fry</a> <span>2</span></div>
  <div id="id12" style="display:none"><ul>
    <li><a href="../../../stats/songs/p.html?songid=1" title="Statistics for Groundhog's Day performed by Primus">Groundhog's Day</a></li>
    <li><a href="../../../stats/songs/p.html?songid=2" title="Statistics for Harold of the Rocks performed by Primus">Harold of the Rocks</a></li>
  </ul></div></li>
<li><div><i class="fa fa-circle" style="color:#aaa;"></i>
  <a href="javascript:void(0);" rel="nofollow">Covers</a> <span>1</span></div>
  <div style="display:none"><ul>
    <li><a href="../../../stats/songs/p.html?songid=9" title="Statistics for Hello Skinny performed by Primus">Hello Skinny</a></li>
  </ul></div></li>
<li><div><i class="fa fa-circle" style="color:#bbb;"></i>
  <a href="javascript:void(0);" rel="nofollow">Others</a> <span>1</span></div>
  <div style="display:none"><ul>
    <li><a href="../../../stats/songs/p.html?songid=8" title="Statistics for B-Side Jam performed by Primus">B-Side Jam</a></li>
  </ul></div></li>
</ul></div>
"""


def test_parse_album_section_maps_songs_and_skips_buckets():
    mp = m._parse_album_section(_ALBUM_HTML)
    assert mp[m.normalize_aggressive("Groundhog's Day")] == "Frizzle Fry"
    assert mp[m.normalize_aggressive("Harold of the Rocks")] == "Frizzle Fry"
    # "Covers" and "Others" are non-album buckets -> their songs aren't mapped
    assert m.normalize_aggressive("Hello Skinny") not in mp
    assert m.normalize_aggressive("B-Side Jam") not in mp


def test_parse_album_section_absent_returns_empty():
    assert m._parse_album_section("<html>no such section</html>") == {}


def test_fetch_album_map_fail_soft(monkeypatch):
    assert m.fetch_album_map("") == {}                     # no url
    class _R:
        status_code = 200
        text = _ALBUM_HTML
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _R())
    mp = m.fetch_album_map("https://setlist.fm/x.html")
    assert mp[m.normalize_aggressive("Groundhog's Day")] == "Frizzle Fry"

    def boom(*a, **k):
        raise m.requests.exceptions.RequestException("offline")
    monkeypatch.setattr(m.requests, "get", boom)
    assert m.fetch_album_map("https://setlist.fm/x.html") == {}   # fail-soft


def test_gather_attaches_setlistfm_album(monkeypatch):
    library = [_FakeTrack("Wilson", "Phish", rating_key=10),
               _FakeTrack("Tweezer (Reprise)", "Phish", rating_key=11)]
    _wire_gather(monkeypatch, library,
                 album_map={m.normalize_aggressive("Some Rarity"): "Rarities LP"})
    result = m.gather_matches(_CONFIG, "abc123")
    assert result["missing"] == [(3, "Phish", "Some Rarity", "Rarities LP")]


# ---------------------------------------------------------------------------
# Core pipeline: load_config / gather_matches / create_playlist
# ---------------------------------------------------------------------------

_CONFIG = {"api_key": "k", "plex_baseurl": "http://x", "plex_token": "t",
           "music_library": "Music"}


def test_load_config_raises_on_missing(monkeypatch):
    monkeypatch.setattr(m, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("SETLISTFM_API_KEY", raising=False)
    monkeypatch.delenv("PLEX_BASEURL", raising=False)
    monkeypatch.delenv("PLEX_TOKEN", raising=False)
    with pytest.raises(m.ConfigError) as exc:
        m.load_config()
    assert "SETLISTFM_API_KEY" in str(exc.value)


def test_load_config_ok(monkeypatch):
    monkeypatch.setattr(m, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("SETLISTFM_API_KEY", "k")
    monkeypatch.setenv("PLEX_BASEURL", "http://x")
    monkeypatch.setenv("PLEX_TOKEN", "t")
    monkeypatch.delenv("PLEX_MUSIC_LIBRARY", raising=False)
    config = m.load_config()
    assert config["api_key"] == "k" and config["music_library"] == "Music"


def _gather_setlist_data():
    return {
        "artist": {"name": "Phish"},
        "venue": {"name": "MSG", "city": {"name": "New York"}},
        "eventDate": "31-12-2023",
        "url": "https://setlist.fm/x.html",
        "sets": {"set": [{"song": [
            {"name": "Wilson"},
            {"name": "Tweezer Reprise"},   # matches "Tweezer (Reprise)"
            {"name": "Some Rarity"},        # not in library -> missing
        ]}]},
    }


def _wire_gather(monkeypatch, library_tracks, album_map=None):
    monkeypatch.setattr(m, "fetch_setlist", lambda sid, key: _gather_setlist_data())
    monkeypatch.setattr(m, "connect_plex", lambda u, t: object())
    section = _FakeSection(artists=[_FakeArtist("Phish", library_tracks)])
    monkeypatch.setattr(m, "get_music_section", lambda plex, lib: section)
    monkeypatch.setattr(m, "fetch_album_map", lambda url: album_map or {})


def test_gather_matches_builds_structure(monkeypatch):
    library = [
        _FakeTrack("Wilson", "Phish", rating_key=10, album="Junta"),
        _FakeTrack("Tweezer (Reprise)", "Phish", rating_key=11, album="A Live One"),
    ]
    _wire_gather(monkeypatch, library)
    result = m.gather_matches(_CONFIG, "abc123")
    assert result["playlist_name"] == "Phish - MSG, New York (2023-12-31)"
    assert [x["rating_key"] for x in result["matched"]] == [10, 11]
    assert result["matched"][1]["track_title"] == "Tweezer (Reprise)"
    assert result["matched"][0]["album"] == "Junta"
    assert result["matched"][0]["candidates"][0]["rating_key"] == 10
    assert result["missing"] == [(3, "Phish", "Some Rarity", "")]  # album from map
    # songs is the full setlist in order, missing rows flagged matched=False
    assert [(s["position"], s["matched"]) for s in result["songs"]] == [
        (1, True), (2, True), (3, False)]
    assert result["songs"][2]["title"] == "Some Rarity"


def test_gather_matches_attaches_multiple_candidates(monkeypatch):
    # Same song on two albums -> one matched row with two candidates.
    library = [
        _FakeTrack("Wilson", "Phish", rating_key=10, album="Junta"),
        _FakeTrack("Wilson", "Phish", rating_key=99, album="Hampton Comes Alive"),
        _FakeTrack("Tweezer (Reprise)", "Phish", rating_key=11, album="A Live One"),
    ]
    _wire_gather(monkeypatch, library)
    result = m.gather_matches(_CONFIG, "abc123")
    wilson = result["matched"][0]
    assert wilson["title"] == "Wilson"
    assert [c["rating_key"] for c in wilson["candidates"]] == [10, 99]
    assert wilson["rating_key"] == 10   # default mirrors the best candidate


# --- match_candidates ------------------------------------------------------

def test_match_candidates_ranks_and_dedupes():
    library = [
        _FakeTrack("Wilson", "Phish", rating_key=1, album="Junta"),
        _FakeTrack("Wilson (Live)", "Phish", rating_key=2, album="A Live One"),
        _FakeTrack("Wilson", "Phish", rating_key=1, album="Junta"),  # dupe key
    ]
    cands = m.match_candidates(_FakeSection(), "Wilson", "Phish", library)
    keys = [c.track.ratingKey for c in cands]
    assert keys == [1, 2]                      # exact before fuzzy; key 1 once
    assert cands[0].quality == "exact"


def test_match_candidates_respects_limit():
    library = [_FakeTrack("Wilson", "Phish", rating_key=i) for i in range(10)]
    cands = m.match_candidates(_FakeSection(), "Wilson", "Phish", library,
                               limit=3)
    assert len(cands) == 3


def test_match_candidates_empty_when_no_match():
    library = [_FakeTrack("Totally Different", "Phish", rating_key=1)]
    assert m.match_candidates(_FakeSection(), "Wilson", "Phish", library) == []


def test_gather_matches_empty_setlist_raises(monkeypatch):
    monkeypatch.setattr(m, "fetch_setlist", lambda sid, key: {"artist": {}})
    with pytest.raises(m.SetlistError):
        m.gather_matches(_CONFIG, "abc123")


def test_gather_matches_setlist_error_wraps(monkeypatch):
    def boom(sid, key):
        raise LookupError("no such setlist")
    monkeypatch.setattr(m, "fetch_setlist", boom)
    with pytest.raises(m.SetlistError):
        m.gather_matches(_CONFIG, "abc123")


class _FakePlaylistObj:
    def __init__(self, title, rating_key, items=()):
        self.title = title
        self.ratingKey = rating_key
        self.type = "playlist"
        self._items = list(items)
        self.added = []

    def items(self):
        return list(self._items)

    def addItems(self, tracks):
        self.added.extend(tracks)
        self._items.extend(tracks)


class _FakeCreatePlex:
    def __init__(self, playlists=()):
        self.created = None
        self._playlists = list(playlists)

    def fetchItem(self, key):
        return _FakeTrack(f"track-{key}", "Phish", rating_key=int(key))

    def playlists(self):
        return list(self._playlists)

    def createPlaylist(self, title, items=None):
        self.created = (title, list(items or []))
        pl = _FakePlaylistObj(title, rating_key=999, items=list(items or []))
        self._playlists.append(pl)
        return pl


def test_create_playlist_creates_and_records_history(monkeypatch, tmp_path):
    fake = _FakeCreatePlex()
    monkeypatch.setattr(m, "connect_plex", lambda u, t: fake)
    hist = tmp_path / "history.json"
    monkeypatch.setattr(m, "history_path", lambda: hist)

    name = m.create_playlist(
        _CONFIG, "Phish - MSG", ["10", "11"],
        history_meta={"id": "abc123", "url": "u", "artist": "Phish",
                      "date": "2023-12-31", "missing": 1,
                      "missing_tracks": [{"artist": "Phish", "title": "Destiny Unbound"}]})

    assert name == "Phish - MSG"
    assert fake.created[0] == "Phish - MSG"
    assert len(fake.created[1]) == 2          # two tracks fetched + added
    saved = m.load_history(hist)
    assert saved["abc123"]["playlist_name"] == "Phish - MSG"
    assert saved["abc123"]["matched"] == 2
    assert saved["abc123"]["playlist_rating_key"] == 999   # key stored for update
    assert saved["abc123"]["missing_tracks"] == [
        {"artist": "Phish", "title": "Destiny Unbound"}]


def test_create_playlist_no_history_when_meta_none(monkeypatch, tmp_path):
    fake = _FakeCreatePlex()
    monkeypatch.setattr(m, "connect_plex", lambda u, t: fake)
    hist = tmp_path / "history.json"
    monkeypatch.setattr(m, "history_path", lambda: hist)
    m.create_playlist(_CONFIG, "PL", ["1"], history_meta=None)
    assert not hist.exists()


def test_create_playlist_skips_history_with_blank_id(monkeypatch, tmp_path):
    # A meta with an empty id must not write a junk history entry keyed by "".
    fake = _FakeCreatePlex()
    monkeypatch.setattr(m, "connect_plex", lambda u, t: fake)
    hist = tmp_path / "history.json"
    monkeypatch.setattr(m, "history_path", lambda: hist)
    m.create_playlist(_CONFIG, "PL", ["1"], history_meta={"id": ""})
    assert not hist.exists()


# --- find_playlist / add_to_playlist (update flow) -------------------------

def test_find_playlist_by_key_then_name():
    one = _FakePlaylistObj("One", 1)
    two = _FakePlaylistObj("Two", 2)
    plex = _FakeCreatePlex(playlists=[one, two])
    assert m.find_playlist(plex, rating_key=2) is two
    assert m.find_playlist(plex, name="One") is one
    assert m.find_playlist(plex, rating_key=9, name="Two") is two   # key miss
    assert m.find_playlist(plex, rating_key=9, name="Nope") is None


def test_add_to_playlist_adds_only_new(monkeypatch, tmp_path):
    pl = _FakePlaylistObj("My Show", 999,
                          items=[_FakeTrack("A", "Phish", rating_key=10)])
    plex = _FakeCreatePlex(playlists=[pl])
    monkeypatch.setattr(m, "connect_plex", lambda u, t: plex)
    monkeypatch.setattr(m, "history_path", lambda: tmp_path / "h.json")

    title, added = m.add_to_playlist(
        _CONFIG, 999, "My Show", ["10", "11", "11"],
        history_meta={"id": "abc", "missing": 0, "missing_tracks": []})

    assert title == "My Show"
    assert added == 1                              # 10 present; 11 new; dup 11
    assert [t.ratingKey for t in pl.added] == [11]
    saved = m.load_history(tmp_path / "h.json")
    assert saved["abc"]["matched"] == 2            # 1 existing + 1 added
    assert saved["abc"]["playlist_rating_key"] == 999


def test_add_to_playlist_missing_playlist_raises(monkeypatch):
    plex = _FakeCreatePlex(playlists=[])           # nothing to find
    monkeypatch.setattr(m, "connect_plex", lambda u, t: plex)
    with pytest.raises(m.PlexError):
        m.add_to_playlist(_CONFIG, 999, "Gone", ["10"])

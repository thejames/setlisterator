#!/usr/bin/env python3
"""Create a Plex music playlist from a setlist.fm show and report missing songs.

This script fetches a setlist from setlist.fm, matches each song against your
Plex music library, builds a playlist (in setlist order) from the tracks it
finds, and prints a report of what it could not match, so you know what your
library is missing.

Environment variables (read from a .env file in the working directory, or the
real environment):

    SETLISTFM_API_KEY     Your setlist.fm API key (https://api.setlist.fm/docs/).
    PLEX_BASEURL          Base URL of your Plex server, e.g. http://localhost:32400
    PLEX_TOKEN            Your Plex auth token (X-Plex-Token).
    PLEX_MUSIC_LIBRARY    Name of the music library section. Default: "Music".
    SETLIST_TO_PLEX_HISTORY  Optional path to the processed-setlist history
                          file. Default: ~/.config/setlist_to_plex/history.json
                          (honoring XDG_CONFIG_HOME).

Per-song match decisions are logged to stderr by default (use --quiet to
silence); the actionable report is printed to stdout. Each processed setlist
is recorded so a repeat run prompts (or, non-interactively, skips) unless
--force is given; --no-history disables the history entirely.

Example usage:

    # By setlist ID
    python setlist_to_plex.py 63de4613

    # By full setlist.fm URL
    python setlist_to_plex.py "https://www.setlist.fm/setlist/phish/2023/madison-square-garden-new-york-ny-63de4613.html"

    # Override the auto-generated playlist name
    python setlist_to_plex.py 63de4613 --name "Phish @ MSG NYE"

    # Re-process a setlist you've already done
    python setlist_to_plex.py 63de4613 --force

    # List previously created playlists (no config needed)
    python setlist_to_plex.py --history

    # Backfill missing-track detail for old history entries
    python setlist_to_plex.py --backfill
"""

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency guard
    print("Missing dependency 'python-dotenv'. Run: pip install -r requirements.txt",
          file=sys.stderr)
    sys.exit(1)

try:
    from plexapi.server import PlexServer
    from plexapi import exceptions as plex_exceptions
except ImportError:  # pragma: no cover - dependency guard
    print("Missing dependency 'plexapi'. Run: pip install -r requirements.txt",
          file=sys.stderr)
    sys.exit(1)


SETLISTFM_REST_BASE = "https://api.setlist.fm/rest/1.0/"
SETLISTFM_API_BASE = SETLISTFM_REST_BASE + "setlist/"

logger = logging.getLogger("setlist_to_plex")

# Exit codes
EXIT_OK = 0
EXIT_CONFIG = 2        # missing/invalid configuration
EXIT_SETLIST = 3       # setlist fetch / parse problems
EXIT_PLEX = 4          # Plex connection / library problems


class ConfigError(Exception):
    """Required environment configuration is missing or invalid."""


class SetlistError(Exception):
    """A setlist could not be fetched, parsed, or had no playable songs."""


class PlexError(Exception):
    """Connecting to Plex, finding the library, or creating a playlist failed."""


# Whether auditions transcode by default (see the auditioning section below).
# Lives here, beside its consumer, so the env fallback and the value handed to
# templates cannot drift apart.
AUDITION_TRANSCODE_DEFAULT = True


def truthy(raw):
    """Is this string a yes? Single source for flags from env and query alike,
    so an env var and a URL parameter can't disagree about what 'on' means."""
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_flag(name, default):
    """Read a boolean environment variable, falling back when it isn't set."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return truthy(raw)


def load_config():
    """Read configuration from the environment (and a .env file).

    Returns a dict with api_key, plex_baseurl, plex_token, music_library.
    Raises ConfigError listing any missing required variables.
    """
    load_dotenv()
    config = {
        "api_key": os.environ.get("SETLISTFM_API_KEY"),
        "plex_baseurl": os.environ.get("PLEX_BASEURL"),
        "plex_token": os.environ.get("PLEX_TOKEN"),
        "music_library": os.environ.get("PLEX_MUSIC_LIBRARY", "Music"),
        # Default for the audition quality preference; the UI can override it
        # per browser. On by default: auditions are for identifying a song, and
        # capped MP3 costs a quarter of the bytes of the average FLAC here.
        "audition_always_transcode": _env_flag(
            "AUDITION_ALWAYS_TRANSCODE", AUDITION_TRANSCODE_DEFAULT),
    }
    missing = [name for name, key in (
        ("SETLISTFM_API_KEY", "api_key"),
        ("PLEX_BASEURL", "plex_baseurl"),
        ("PLEX_TOKEN", "plex_token"),
    ) if not config[key]]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
            + ". Set them in a .env file or your environment.")
    return config


# ---------------------------------------------------------------------------
# Normalization & matching helpers
# ---------------------------------------------------------------------------

# Words/abbreviations we canonicalize so "Pt. 2" matches "Part 2", etc.
_REPLACEMENTS = (
    (r"\bpt\b", "part"),
    (r"\band\b", "&"),
)


def _collapse(text):
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s&]", " ", text)       # keep word chars, spaces, ampersand
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_simple(text):
    """Conservative normalization used for 'exact' comparison."""
    if not text:
        return ""
    return _collapse(text)


def _join_acronyms(text):
    """Join dotted acronyms so "N.I.B." -> "NIB", "U.S.A" -> "USA".

    Matches a run of single letters each followed by a dot (at least two, so a
    word like "Mr." or "will.i.am" is left alone) and strips the dots — otherwise
    _collapse would turn them into spaces ("n i b") and they'd never match the
    undotted form ("nib")."""
    return re.sub(r"\b(?:[A-Za-z]\.){2,}[A-Za-z]?",
                  lambda mt: mt.group(0).replace(".", ""), text)


def normalize_aggressive(text):
    """Looser normalization: drop parentheticals, feat., leading 'the', join
    dotted acronyms, and canonicalize Pt./Part, and/&. Used for fuzzy
    comparison."""
    if not text:
        return ""
    text = text.lower()
    # Drop bracketed/parenthetical asides: (live), [remastered], etc.
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)
    # Drop featured-artist clauses.
    text = re.sub(r"\b(feat|ft|featuring|with)\.?\b.*$", " ", text)
    text = _join_acronyms(text)   # "n.i.b." -> "nib" before dots become spaces
    text = _collapse(text)
    for pattern, repl in _REPLACEMENTS:
        text = re.sub(pattern, repl, text)
    # Drop a leading article that survives normalization.
    text = re.sub(r"^the ", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def artists_match(a, b):
    """Fuzzy, case-insensitive comparison of two artist names."""
    return normalize_aggressive(a) == normalize_aggressive(b)


# ---------------------------------------------------------------------------
# setlist.fm
# ---------------------------------------------------------------------------

def parse_setlist_id(arg):
    """Accept a bare setlist ID or a full setlist.fm URL and return the ID.

    setlist.fm URLs end in '...-<id>.html', where the id is an 8-char hex-ish
    token. A bare argument is assumed to already be the id.
    """
    arg = arg.strip()
    if "setlist.fm" in arg or arg.startswith("http"):
        # The id is the trailing token before .html, after the final hyphen.
        match = re.search(r"-([0-9a-fA-F]+)\.html", arg)
        if match:
            return match.group(1)
        raise ValueError(f"Could not parse a setlist ID out of URL: {arg}")
    return arg


# setlist.fm throttles the free API hard, and a 429 is often transient — a
# retry a second later usually succeeds. Back off a few times before giving up.
_SFM_RATE_LIMIT_RETRIES = 3
_SFM_RATE_LIMIT_BACKOFF = (1, 2, 4)   # seconds to wait before each retry


def _retry_after_seconds(resp):
    """Parse a Retry-After header (delta-seconds form) into a capped float, or None.

    setlist.fm sends plain seconds; the HTTP-date form (or anything unparseable)
    yields None so the caller falls back to its own backoff. Capped at 10s so a
    bogus value can't hang the request.
    """
    raw = getattr(resp, "headers", {}).get("Retry-After")
    if not raw:
        return None
    try:
        return min(max(0.0, float(raw)), 10.0)
    except (TypeError, ValueError):
        return None


def _setlistfm_get(url, api_key, params=None, not_found="That was not found."):
    """GET a setlist.fm REST endpoint and return its parsed JSON.

    Centralizes auth, error mapping, and JSON parsing so every caller behaves
    the same. Retries a rate-limited (429) request a few times with backoff
    (honoring Retry-After when present) before surfacing it. Raises
    ConnectionError (unreachable), LookupError (404), PermissionError (bad key),
    or RuntimeError (429 after retries, other non-200, or non-JSON).
    """
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    resp = None
    for attempt in range(_SFM_RATE_LIMIT_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.exceptions.RequestException as exc:
            raise ConnectionError(f"Could not reach setlist.fm: {exc}") from exc
        if resp.status_code != 429 or attempt == _SFM_RATE_LIMIT_RETRIES:
            break
        wait = _retry_after_seconds(resp)
        if wait is None:
            wait = _SFM_RATE_LIMIT_BACKOFF[attempt]
        logger.warning("setlist.fm rate-limited (429); retrying in %ss "
                       "(attempt %d/%d).", wait, attempt + 1,
                       _SFM_RATE_LIMIT_RETRIES)
        time.sleep(wait)

    if resp.status_code == 404:
        raise LookupError(not_found)
    if resp.status_code in (401, 403):
        raise PermissionError(
            "setlist.fm rejected the API key (check SETLISTFM_API_KEY).")
    if resp.status_code == 429:
        raise RuntimeError(
            "setlist.fm is rate-limiting requests (HTTP 429). "
            "Wait a minute and try again.")
    if resp.status_code != 200:
        raise RuntimeError(
            f"setlist.fm returned HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError("setlist.fm returned a non-JSON response.") from exc


def fetch_setlist(setlist_id, api_key):
    """Fetch and return the raw setlist JSON dict from setlist.fm."""
    return _setlistfm_get(
        SETLISTFM_API_BASE + setlist_id, api_key,
        not_found=f"No setlist found with ID '{setlist_id}'.")


def fetch_attended(username, api_key, max_pages=50):
    """Return the shows a setlist.fm user has marked attended ("I was there").

    Pages through GET /user/{username}/attended (20 setlists/page) and returns
    a list of light row dicts in setlist.fm's order (most recent first):
    {id, url, artist, venue, city, date}. Songs are intentionally omitted — the
    list only needs identity + display fields; each row's Preview re-fetches the
    full setlist through the normal pipeline. Raises LookupError if the username
    is unknown, plus the usual connection/permission errors from _setlistfm_get.
    """
    url = SETLISTFM_REST_BASE + f"user/{username}/attended"
    rows = []
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(0.5)   # be polite to the API across pages
        try:
            data = _setlistfm_get(
                url, api_key, params={"p": page},
                not_found=f"No setlist.fm user named '{username}' (is it public?).")
        except LookupError:
            if page == 1:
                raise         # page 1 -> the username itself is unknown/private
            break             # later pages -> we've simply run off the end
        setlists = data.get("setlist") or []
        for sl in setlists:
            show = extract_show(sl)
            rows.append({
                "id": sl.get("id", ""),
                "url": show["url"],
                "artist": show["artist"],
                "venue": show["venue"],
                "city": show["city"],
                "date": show["date"],
            })
        # Last page is short (or empty); stop before spending another request.
        if len(setlists) < (data.get("itemsPerPage") or 20):
            break
    else:
        logger.warning("Stopped paging attended shows for '%s' at the "
                       "%d-page cap; some may be missing.", username, max_pages)
    return rows


def extract_show(data):
    """Pull the fields we care about out of the setlist JSON.

    Returns a dict: artist, venue, city, date, songs (ordered list of titles).
    Flattens every set/encore into one ordered list and skips tape/intro
    entries that have no real song name.
    """
    artist = (data.get("artist") or {}).get("name") or "Unknown Artist"

    venue_obj = data.get("venue") or {}
    venue = venue_obj.get("name") or "Unknown Venue"
    city = (venue_obj.get("city") or {}).get("name") or "Unknown City"

    # setlist.fm gives eventDate as DD-MM-YYYY; present it as ISO YYYY-MM-DD.
    raw_date = data.get("eventDate") or ""
    try:
        date = datetime.strptime(raw_date, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        date = raw_date  # keep whatever we got if the format is unexpected

    url = data.get("url") or ""

    songs = []
    # The API uses {"sets": {"set": [...]}}. Be tolerant of a bare "set" too.
    sets_container = data.get("sets") or {}
    set_list = sets_container.get("set") if isinstance(sets_container, dict) else None
    if set_list is None:
        set_list = data.get("set") or []

    for set_obj in set_list:
        for song in set_obj.get("song", []):
            name = (song.get("name") or "").strip()
            if not name:
                continue  # empty entry
            if song.get("tape"):
                continue  # walk-on / intro tape, not performed
            songs.append(name)

    return {
        "artist": artist,
        "venue": venue,
        "city": city,
        "date": date,
        "url": url,
        "songs": songs,
    }


def build_playlist_name(show):
    """Auto-generate: '{artist} - {venue}, {city} ({date})'."""
    return f"{show['artist']} - {show['venue']}, {show['city']} ({show['date']})"


# ---------------------------------------------------------------------------
# Plex
# ---------------------------------------------------------------------------

def connect_plex(baseurl, token):
    """Connect to Plex, raising a clear error on failure."""
    try:
        return PlexServer(baseurl, token)
    except plex_exceptions.Unauthorized as exc:
        raise PermissionError(
            "Plex rejected the token (check PLEX_TOKEN).") from exc
    except requests.exceptions.RequestException as exc:
        raise ConnectionError(
            f"Could not reach Plex at {baseurl}: {exc}") from exc


def get_music_section(plex, section_name):
    """Return the music LibrarySection, or raise if it isn't found."""
    try:
        section = plex.library.section(section_name)
    except plex_exceptions.NotFound as exc:
        available = ", ".join(s.title for s in plex.library.sections()) or "(none)"
        raise LookupError(
            f"No music library section named '{section_name}'. "
            f"Available sections: {available}") from exc
    if section.type != "artist":
        raise LookupError(
            f"Library section '{section_name}' is not a music library "
            f"(type='{section.type}').")
    return section


class PlexService:
    """Adapter letting gather_matches target a music backend.

    ``connect`` returns the ``(client, section)`` pair the matcher searches
    against. It calls the module-level ``connect_plex``/``get_music_section`` by
    name (not via ``self``) so tests that monkeypatch those still take effect.
    """

    name = "plex"

    def connect(self, config):
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
        section = get_music_section(plex, config["music_library"])
        return plex, section


PLEX_SERVICE = PlexService()


def _track_artist_name(track):
    """Best-effort album-artist name for a Plex track."""
    # grandparentTitle is the album artist; originalTitle often holds the
    # track's own (featured) artist when it differs from the album artist.
    return getattr(track, "grandparentTitle", None) or \
        getattr(track, "originalTitle", None) or ""


def _track_album(track):
    """The album a Plex track is pulled from (parentTitle)."""
    return getattr(track, "parentTitle", None) or ""


def _track_year(track):
    """The release year of a Plex track's album, or None if unknown.

    Plex sends ``parentYear`` on every <Track> on both endpoints the matcher
    uses, so the year rides along on tracks we already fetch — no album lookups,
    and no requests of any kind. plexapi 4.18.1 doesn't map it
    (``Track._loadData`` reads only ``year``), so it comes off the raw element;
    the plain attribute is probed first so this starts working by itself if
    plexapi ever maps it, and so offline fakes can set one the way they do
    ``parentTitle``.

    Never raises, and a non-positive or unparseable year counts as unknown —
    which sorts last, not first (see ADR 0006).
    """
    raw = getattr(track, "parentYear", None)
    if raw is None:
        # Both fallbacks read the raw element, never `getattr(track, "year")`:
        # plexapi *does* define `year` on Track and leaves it None here, and
        # reading a None public attribute off a partial object silently fires a
        # per-track reload request (PlexPartialObject.__getattribute__). Names
        # starting with "_" are exempt from that, so this stays free.
        try:
            attrib = track._data.attrib
            raw = attrib.get("parentYear") or attrib.get("year")
        except Exception:
            raw = None
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return None
    return year if year > 0 else None


# Sort position for a candidate whose album year is unknown: after every dated
# one. Undated means unknown, not old.
_YEAR_UNKNOWN = float("inf")


def _year_key(track):
    """``_track_year`` as a sort key, undated last."""
    year = _track_year(track)
    return _YEAR_UNKNOWN if year is None else year


# A resolved match for one setlist song. tier is one of TIER_NAMES; source is
# 'scoped' (the artist's own tracks) or 'global' (fallback title search).
Match = namedtuple("Match", "track quality tier source")

# Result of a playlist create/edit. ``poster`` is None when no image was
# supplied, True when the poster upload succeeded, False when it was attempted
# but failed (best-effort — see ADR 0002). The web layer keys its "couldn't set
# the image" warning off a False here; the CLI never supplies an image, so it
# stays None. CreateResult (create) and SetResult (in-place edit) name their
# results symmetrically so callers read fields instead of tuple positions.
CreateResult = namedtuple("CreateResult", "name poster")
SetResult = namedtuple("SetResult", "name count poster")

# Human-readable names for the _title_rank tiers (index == rank).
TIER_NAMES = ("exact", "loose", "medley", "prefix")

# Separators that join songs in a single library track (medleys).
_MEDLEY_SPLIT = re.compile(r"\s*(?:/|→|>|;)\s*")


def _split_medley(title):
    """Split a medley title into its component songs."""
    return [part for part in _MEDLEY_SPLIT.split(title) if part.strip()]


def _title_rank(target_simple, target_aggr, cand_title):
    """Compare a candidate track title to the target song.

    Returns a rank (lower is better) or None for no match:
        0  simple-normalized titles are identical            -> 'exact'
        1  aggressive-normalized titles match (parens/feat.) -> 'fuzzy'
        2  a medley segment of the candidate matches         -> 'fuzzy'
        3  candidate tokens start with the target's tokens   -> 'fuzzy'
    """
    if not target_simple:
        return None
    if normalize_simple(cand_title) == target_simple:
        return 0
    if target_aggr and normalize_aggressive(cand_title) == target_aggr:
        return 1
    # Medley: "Hello Skinny / Constantinople" should match "Hello Skinny".
    if target_aggr:
        for segment in _split_medley(cand_title):
            if normalize_aggressive(segment) == target_aggr:
                return 2
    # Token-prefix: candidate title leads with the full target title, e.g.
    # "Tommy the Cat - Live". Require a multi-word target to avoid matching a
    # short title against an unrelated longer one ("Bob" -> "Bobby Brown").
    target_tokens = target_aggr.split()
    if len(target_tokens) >= 2:
        cand_tokens = normalize_aggressive(cand_title).split()
        if cand_tokens[:len(target_tokens)] == target_tokens:
            return 3
    return None


def _ranked_matches(target_simple, target_aggr, tracks, setlist_artist, scoped):
    """Return [(overall_rank, track, quality, tier), ...] sorted best-first.

    Every track whose title matches at some tier is included. When ``scoped``
    is True the tracks are already known to be by the setlist artist, so artist
    matching is assumed; otherwise it is checked per track and artist mismatches
    are demoted below every artist-confirmed match.

    Same-rank ties break on album year ascending — the earliest album is the
    default version (ADR 0006). Year sorts strictly *within* a rank, never
    across one, so a better title always wins over an older album and a medley
    is never floated above a cleaner match. Undated albums come last, and ties
    still keep library order because the sort is stable.
    """
    ranked = []
    for track in tracks:
        title_rank = _title_rank(target_simple, target_aggr, track.title)
        if title_rank is None:
            continue
        artist_ok = True if scoped else \
            artists_match(_track_artist_name(track), setlist_artist)
        overall = title_rank if artist_ok else title_rank + 4
        quality = "exact" if (artist_ok and title_rank == 0) else "fuzzy"
        ranked.append((overall, track, quality, TIER_NAMES[title_rank]))
    # Stable: candidates on equally-old albums keep input (library) order.
    ranked.sort(key=lambda r: (r[0], _year_key(r[1])))
    return ranked


def _best_match(target_simple, target_aggr, tracks, setlist_artist, scoped):
    """Return the single best (overall_rank, track, quality, tier), or None."""
    ranked = _ranked_matches(target_simple, target_aggr, tracks,
                             setlist_artist, scoped)
    return ranked[0] if ranked else None


def _prefer_album_first(candidates, prefer_album):
    """Stable-reorder a Match list so matches on ``prefer_album`` lead.

    A preferred-album match outranks title tier — a live recording's titles span
    tiers (exact "Tommy the Cat", loose "...(Live)", prefix "...- Live at X") —
    but a *medley* is never floated this way (it would grab the wrong track over
    a clean single). The sort is stable, so within the preferred and the
    non-preferred group the existing tier order is kept. ``prefer_album`` is an
    exact Plex album title, so a case-insensitive equality compare suffices.
    """
    if not prefer_album:
        return candidates
    want = prefer_album.strip().lower()
    return sorted(candidates, key=lambda c: 0 if (
        c.tier != "medley" and _track_album(c.track).strip().lower() == want)
        else 1)


def resolve_artist(section, setlist_artist):
    """Find the library Artist matching the setlist artist, or None."""
    try:
        candidates = section.searchArtists(title=setlist_artist, maxresults=10)
    except Exception:
        return None
    for artist in candidates:
        if artists_match(getattr(artist, "title", ""), setlist_artist):
            return artist
    return None


def _search_candidates(section, title, target_aggr):
    """Global track search (used as a fallback for covers / missing artist)."""
    try:
        candidates = section.searchTracks(title=title, maxresults=50)
    except Exception:
        candidates = []
    # Fallback: search on the aggressively-normalized title (drops parens/feat)
    # in case the raw title is too specific to hit the index.
    if not candidates and target_aggr and target_aggr != normalize_simple(title):
        try:
            candidates = section.searchTracks(title=target_aggr, maxresults=50)
        except Exception:
            candidates = []
    return candidates


def match_candidates(section, title, setlist_artist, artist_tracks, limit=5):
    """Return up to ``limit`` candidate Plex tracks for a setlist song.

    Best-first and deduped by rating key; empty if nothing matched. Strategy:
    first match against ``artist_tracks`` (the setlist artist's own tracks,
    fetched once) using local normalization — this sidesteps Plex's search
    tokenizer, which can miss tracks over punctuation/Unicode quirks. Only if
    that finds nothing do we fall back to a global title search, which also
    covers songs performed as covers of other artists. Each item is a
    Match(track, quality, tier, source). (Album preference is applied separately
    by the caller via _prefer_album_first, so this stays single-purpose.)
    """
    target_simple = normalize_simple(title)
    target_aggr = normalize_aggressive(title)

    ranked = _ranked_matches(target_simple, target_aggr, artist_tracks,
                             setlist_artist, scoped=True)
    source = "scoped"
    if not ranked:
        searched = _search_candidates(section, title, target_aggr)
        ranked = _ranked_matches(target_simple, target_aggr, searched,
                                 setlist_artist, scoped=False)
        source = "global"

    results = []
    seen = set()
    for _, track, quality, tier in ranked:
        key = getattr(track, "ratingKey", None)
        dedupe = key if key is not None else id(track)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        results.append(Match(track, quality, tier, source))
        if len(results) >= limit:
            break
    return results


def match_song(section, title, setlist_artist, artist_tracks):
    """Return the single best Match for a setlist song, or None.

    Thin wrapper over match_candidates so the CLI and existing callers stay on
    one code path.
    """
    candidates = match_candidates(section, title, setlist_artist, artist_tracks)
    return candidates[0] if candidates else None


def _album_coverage(per_song_albums):
    """Tally how many setlist songs each album can supply, highest first.

    ``per_song_albums`` is one entry per matched song: the set of (non-medley)
    album titles that song matched on. Returns [{"album", "songs"}, ...] sorted
    by coverage descending, then album title.

    This is a *suggestion*, not a decision: it orders the preview's album
    dropdown, so a full live recording of the show leads the list with its song
    count and is one click away. Nothing is preferred automatically (ADR 0006).
    """
    counts = {}
    for albums in per_song_albums:
        for album in albums:
            counts[album] = counts.get(album, 0) + 1
    return [{"album": album, "songs": n} for album, n in
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))]


def unique_playlist_name(plex, name):
    """Append a numeric suffix if a playlist with this name already exists."""
    # plex.playlists() can yield objects without a .title: plexapi builds each
    # child element via a registry that only resolves <Playlist type="playlist">
    # to a real Playlist, and any stray element falls back to a titleless Tag.
    # Guard with getattr so such an element can't crash collision detection.
    existing = {pl.title for pl in plex.playlists() if getattr(pl, "title", None)}
    if name not in existing:
        return name
    suffix = 2
    while f"{name} ({suffix})" in existing:
        suffix += 1
    return f"{name} ({suffix})"


# ---------------------------------------------------------------------------
# Processed-setlist history (a small JSON store keyed by setlist ID)
# ---------------------------------------------------------------------------

def history_path():
    """Path to the JSON history file (override with SETLIST_TO_PLEX_HISTORY)."""
    override = os.environ.get("SETLIST_TO_PLEX_HISTORY")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "setlist_to_plex" / "history.json"


def _load_json_store(path):
    """Load a JSON dict store; tolerate a missing or corrupt file."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        logger.warning("Could not read %s (%s); starting fresh.", path, exc)
        return {}


def _save_json_store(path, data):
    """Write a JSON dict store atomically (temp file + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load_history(path):
    """Load the history dict; tolerate a missing or corrupt file."""
    return _load_json_store(path)


def save_history(path, history):
    """Write the history dict atomically (temp file + replace)."""
    _save_json_store(path, history)


def _forget_history(playlist_id):
    """Drop any history entry pointing at a (now-deleted) playlist id."""
    hist_file = history_path()
    history = load_history(hist_file)
    keys = [k for k, e in history.items()
            if str(e.get("playlist_rating_key")) == str(playlist_id)]
    for k in keys:
        del history[k]
    if keys:
        try:
            save_history(hist_file, history)
        except OSError as exc:
            logger.warning("Could not write history (%s).", exc)


def _update_history_playlist(playlist_id, name, count):
    """Refresh name/track-count on any history entry for an edited playlist.

    No-op when the playlist isn't in history (e.g. an externally-made one).
    """
    hist_file = history_path()
    history = load_history(hist_file)
    changed = False
    for entry in history.values():
        if str(entry.get("playlist_rating_key")) == str(playlist_id):
            entry["playlist_name"] = name
            entry["matched"] = count
            changed = True
    if changed:
        try:
            save_history(hist_file, history)
        except OSError as exc:
            logger.warning("Could not write history (%s).", exc)


def history_entry_for_playlist(playlist_id):
    """The history entry for an existing Plex playlist, or None.

    None for a playlist this app never created (the editor works on any Plex
    playlist), which is the signal to leave its summary alone.
    """
    for entry in load_history(history_path()).values():
        if str(entry.get("playlist_rating_key")) == str(playlist_id):
            return entry
    return None


def should_process(history, setlist_id, force, is_tty, prompt_fn=input):
    """Decide whether to process a setlist given prior history.

    New IDs and ``force`` always proceed. A previously-processed ID prompts
    when interactive (TTY), and is skipped otherwise so automation never hangs.
    """
    if setlist_id not in history or force:
        return True
    entry = history[setlist_id]
    when = entry.get("processed_at", "previously")
    name = entry.get("playlist_name", "(unknown)")
    notice = f"Setlist {setlist_id} was already processed {when} as '{name}'."
    if is_tty:
        print(notice, file=sys.stderr)
        return prompt_fn("Re-process? [y/N] ").strip().lower() in ("y", "yes")
    print(notice + " Use --force to re-process.", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# setlist.fm "Songs on Albums": scrape per-song album data from the page
# (the JSON API only returns song names; the HTML page maps songs to albums)
# ---------------------------------------------------------------------------

_SETLISTFM_UA = ("Mozilla/5.0 (compatible; setlisterator/1.0; "
                 "+https://github.com/thejames/setlisterator)")


def _parse_album_section(page_html):
    """Parse a setlist.fm page's 'Songs on Albums' block into
    {normalized song title -> album}. Songs in the 'Covers' bucket are skipped
    (no real album). Returns {} if the section isn't present.
    """
    start = page_html.find("setlistAlbumStats")
    if start == -1:
        return {}
    section = page_html[start:start + 12000]
    mapping = {}
    # Each album is a block beginning with a coloured circle icon, then an
    # <a rel="nofollow">Album</a>, then its songs link to /stats/songs/ pages.
    for block in re.split(r"fa fa-circle", section)[1:]:
        head = re.search(r'rel="nofollow"[^>]*>([^<]+)</a>', block)
        if not head:
            continue
        album = html.unescape(head.group(1)).strip()
        # "Covers" and "Others" are setlist.fm's non-album buckets, not albums.
        if not album or album.lower() in ("covers", "others"):
            continue
        for song in re.findall(r'/stats/songs/[^"]*"[^>]*>([^<]+)</a>', block):
            mapping[normalize_aggressive(html.unescape(song))] = album
    return mapping


def fetch_album_map(url):
    """Return {normalized song title -> album} scraped from a setlist.fm page.

    Best-effort and fail-soft: any network/HTTP/parse problem yields {} (the
    album hint is then left to MusicBrainz). Empty url -> {}.
    """
    if not url:
        return {}
    try:
        resp = requests.get(url, headers={"User-Agent": _SETLISTFM_UA}, timeout=20)
    except requests.exceptions.RequestException:
        return {}
    if resp.status_code != 200:
        return {}
    try:
        return _parse_album_section(resp.text)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# MusicBrainz: best-guess album for a missing track (free, no API key)
# ---------------------------------------------------------------------------

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2/recording/"
_MB_UA = "setlisterator/1.0 ( https://github.com/thejames/setlisterator )"
_mb_last_request = 0.0  # monotonic timestamp, for ~1 req/sec rate limiting


def _mb_get(artist, title):
    """Query the MusicBrainz recording search; return parsed JSON or None.

    Honors MusicBrainz's ~1 request/second rate limit and fails soft (None) on
    any network/HTTP/JSON error so callers degrade gracefully when offline.
    """
    global _mb_last_request
    wait = 1.1 - (time.monotonic() - _mb_last_request)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.get(
            MUSICBRAINZ_API,
            params={"query": f'artist:"{artist}" AND recording:"{title}"',
                    "fmt": "json", "limit": 25},
            headers={"User-Agent": _MB_UA}, timeout=15)
        _mb_last_request = time.monotonic()
        return resp.json() if resp.status_code == 200 else None
    except (requests.exceptions.RequestException, ValueError):
        _mb_last_request = time.monotonic()
        return None


def lookup_album(artist, title):
    """Best-guess studio album for a track via MusicBrainz; "" if unknown.

    Counts how often each studio release-group (primary type 'Album', no
    secondary types like Live/Compilation) backs the matching recordings and
    returns the most frequent — the reissue-heavy canonical album outvotes
    one-off live/compilation releases.
    """
    if not (artist and title):
        return ""
    data = _mb_get(artist, title)
    if not data:
        return ""
    freq = {}
    for rec in data.get("recordings", []):
        for rel in rec.get("releases", []):
            rg = rel.get("release-group") or {}
            if rg.get("primary-type") != "Album" or rg.get("secondary-types"):
                continue
            name = rg.get("title")
            if name:
                freq[name] = freq.get(name, 0) + 1
    return max(freq, key=lambda n: freq[n]) if freq else ""


def enrich_missing_albums(history, limit=40):
    """Fill a still-empty 'album' on history missing_tracks via MusicBrainz —
    the fallback for tracks setlist.fm's "Songs on Albums" didn't cover. Lazy +
    cached: only tracks with no album and not yet MB-checked are looked up, at
    most ``limit`` per call. Returns True if any entry changed.
    """
    changed = False
    done = 0
    for entry in history.values():
        for track in entry.get("missing_tracks", []):
            if track.get("album") or track.get("album_checked"):
                continue
            if done >= limit:
                return changed
            track["album"] = lookup_album(track.get("artist", ""),
                                          track.get("title", ""))
            track["album_checked"] = True   # don't re-query a no-match
            changed = True
            done += 1
    return changed


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_history():
    """Print previously created playlists, newest first, from the history file."""
    entries = list(load_history(history_path()).values())
    if not entries:
        print("No history yet — create a playlist and it'll show up here.")
        return
    entries.sort(key=lambda e: e.get("processed_at", ""), reverse=True)
    print(f"History ({len(entries)}):")
    for e in entries:
        when = e.get("processed_at", "?")
        artist = e.get("artist", "Unknown")
        date = e.get("date", "?")
        name = e.get("playlist_name", "—")
        counts = f"{e.get('matched', 0)} added"
        if e.get("missing"):
            counts += f" · {e.get('missing')} missing"
        print(f"  {when}  {artist} ({date})  \"{name}\"  [{counts}]")


def print_report(playlist_name, added_count, missing, fuzzy):
    """Print the human-readable summary."""
    print()
    print("=" * 70)
    print(f"Playlist: {playlist_name}")
    print(f"Tracks added: {added_count}")
    print("=" * 70)

    if missing:
        print()
        print(f"MISSING TRACKS ({len(missing)}) — not found in your library:")
        for row in missing:
            pos, artist, title = row[0], row[1], row[2]
            album = row[3] if len(row) > 3 else ""
            print(f"  {pos}. {artist} - {title}" + (f"  [{album}]" if album else ""))

    if fuzzy:
        print()
        print(f"FUZZY MATCHES ({len(fuzzy)}) — spot-check for false positives:")
        for pos, want, got in fuzzy:
            print(f"  {pos}. setlist: {want}")
            print(f"      plex:    {got}")

    if not missing and not fuzzy:
        print()
        print("Every song matched exactly. Nice library.")
    print()


# ---------------------------------------------------------------------------
# Core pipeline (shared by the CLI and the web app)
# ---------------------------------------------------------------------------

def gather_matches(config, setlist_id, name=None, prefer_album=None,
                   service=None):
    """Fetch a setlist and match every song against a music service's catalog.

    Read-only: no playlist is created. Returns a dict with the show metadata,
    the resolved playlist name, and matched/missing/fuzzy lists (matched
    entries are deduped and carry the track rating key so a playlist can be
    built later without re-matching). Raises SetlistError or PlexError (with
    a human-readable message) on failure.

    ``service`` selects the backend to match against (a PlexService by default);
    it supplies the ``(client, section)`` the two-tier matcher searches.

    ``prefer_album`` biases which version of a song is chosen when it exists on
    several albums: a non-empty title floats that album above title tier, while
    ``None`` and ``""`` alike mean no preference — the default is then the
    earliest album carrying each song (ADR 0006). No album is ever preferred
    automatically. The preference and the available album options (by coverage,
    which is what populates the preview's dropdown) are returned as
    ``preferred_album`` / ``album_options``.
    """
    try:
        data = fetch_setlist(setlist_id, config["api_key"])
        show = extract_show(data)
    except (LookupError, PermissionError, ConnectionError, RuntimeError) as exc:
        raise SetlistError(str(exc)) from exc
    if not show["songs"]:
        raise SetlistError(
            f"Setlist '{setlist_id}' has no playable songs "
            "(empty setlist or all tape/intro entries).")

    playlist_name = name or build_playlist_name(show)
    logger.info("Setlist: %s — %s, %s (%s) — %d songs", show["artist"],
                show["venue"], show["city"], show["date"], len(show["songs"]))
    if show["url"]:
        logger.info("Source:  %s", show["url"])

    service = service or PLEX_SERVICE
    try:
        _client, section = service.connect(config)
    except (PermissionError, ConnectionError, LookupError) as exc:
        raise PlexError(str(exc)) from exc

    artist = resolve_artist(section, show["artist"])
    artist_tracks = []
    if artist is not None:
        try:
            artist_tracks = artist.tracks()
        except Exception:
            artist_tracks = []
        logger.info("Library: matching against %d tracks by %s",
                    len(artist_tracks), artist.title)
    else:
        logger.info("Library: '%s' not found as an artist; "
                    "using global track search", show["artist"])

    def _describe(match):
        return {
            "rating_key": getattr(match.track, "ratingKey", None),
            "track_title": match.track.title,
            "track_artist": _track_artist_name(match.track),
            "album": _track_album(match.track),
            "tier": match.tier,
            "source": match.source,
            "quality": match.quality,
        }

    # setlist.fm's page maps songs to albums; use it to label missing tracks.
    album_map = fetch_album_map(show["url"])

    # Pass 1: match once with no album preference, both to tally which albums
    # cover the most of the setlist (the dropdown's order) and to reuse as the
    # result. limit=10 so several album versions of a song survive re-ordering.
    first_pass = [(position, title,
                   match_candidates(section, title, show["artist"],
                                    artist_tracks, limit=10))
                  for position, title in enumerate(show["songs"], start=1)]
    per_song_albums = [
        {_track_album(c.track) for c in cands
         if c.tier != "medley" and _track_album(c.track)}
        for _, _, cands in first_pass if cands]
    album_options = _album_coverage(per_song_albums)

    # Earliest-album ordering is silent when it can't work, so say so: a library
    # whose tracks carry no year at all falls back to plain library order.
    all_candidates = [c for _, _, cands in first_pass for c in cands]
    if all_candidates and not any(_track_year(c.track) for c in all_candidates):
        logger.debug("Album:   no candidate resolved an album year; "
                     "falling back to library order")

    preferred_album = prefer_album or ""      # None and "" both mean earliest
    if preferred_album:
        logger.info("Album:   preferring %r", preferred_album)

    # Apply the preference by re-ordering each song's pass-1 candidates in memory
    # (no second Plex search), then trim to the usual 5 shown per song.
    per_song = [(position, title,
                 _prefer_album_first(cands, preferred_album)[:5])
                for position, title, cands in first_pass]

    matched = []
    missing = []
    fuzzy = []
    songs = []   # the full setlist in order, each row flagged matched or not
    for position, title, candidates in per_song:
        # The album setlist.fm says this song is from. Distinct from a matched
        # row's "album" (the Plex album of the chosen track): this one is a
        # property of the *song*, known even when nothing in the library
        # matched, so it labels every state in the playlist summary.
        release = album_map.get(normalize_aggressive(title), "")
        if not candidates:
            missing.append((position, show["artist"], title, release))
            songs.append({"position": position, "title": title,
                          "matched": False, "artist": show["artist"],
                          "release": release})
            logger.info("  ✗ %2d. %s → no match", position, title)
            continue
        best = candidates[0]
        album = _track_album(best.track)
        alt = f"  (+{len(candidates) - 1} alt)" if len(candidates) > 1 else ""
        logger.info("  ✓ %2d. %s → %r on %r  [%s/%s]%s", position, title,
                    best.track.title, album, best.tier, best.source, alt)
        if best.quality == "fuzzy":
            got = f"{_track_artist_name(best.track)} - {best.track.title}"
            if album:
                got += f" [{album}]"
            fuzzy.append((position, f"{show['artist']} - {title}", got))
        # One row per setlist song (no dedupe here): each gets its own picker.
        # Duplicate track choices are collapsed when the playlist is created.
        entry = {"position": position, "title": title,
                 "candidates": [_describe(c) for c in candidates]}
        entry.update(_describe(best))  # default fields mirror the best candidate
        matched.append(entry)
        songs.append({**entry, "matched": True, "release": release})

    return {
        "setlist_id": setlist_id,
        "show": show,
        "playlist_name": playlist_name,
        "songs": songs,
        "matched": matched,
        "missing": missing,
        "fuzzy": fuzzy,
        "preferred_album": preferred_album,
        "album_options": album_options,
        "service": service.name,
    }


def song_map(result):
    """The per-song map a playlist summary is derived from.

    One row per setlist position — its title, the album setlist.fm says the song
    is from, and the track it matched (None when nothing did). Built from a
    gather_matches result so the CLI and the web produce the same shape; the web
    round-trips it through a form, where a user's selection can replace a row's
    key. Keys are strings so that round trip can't change their type.
    """
    return [{"position": song["position"],
             "title": song["title"],
             "album": song.get("release", ""),
             "rating_key": (str(song["rating_key"])
                            if song.get("matched")
                            and song.get("rating_key") is not None else None)}
            for song in result["songs"]]


def _merge_song_maps(stored, incoming):
    """Merge a freshly-matched per-song map over the stored one.

    A stored key is the track that actually went into the playlist — a version
    the user may have selected themselves — so it wins over a re-match's default
    for the same song. A fresh match only fills a gap: the song that had nothing
    in the library and now does, which is the whole point of an update.
    """
    if not stored:
        return incoming
    keys = {s.get("position"): s.get("rating_key") for s in stored}
    return [{**song, "rating_key": keys.get(song.get("position"))
             or song.get("rating_key")} for song in incoming]


def _record_history(playlist_name, playlist_rating_key, matched_count,
                    history_meta, service="plex"):
    """Write/merge the history entry for a created or updated playlist.

    ``service`` tags which backend the playlist lives in (always "plex"); the
    field is retained in the schema so older entries stay readable.
    """
    if not (history_meta and history_meta.get("id")):
        return
    setlist_id = history_meta["id"]
    hist_file = history_path()
    history = load_history(hist_file)
    entry = history.get(setlist_id, {})
    entry.update({
        "id": setlist_id,
        "url": history_meta.get("url", entry.get("url", "")),
        "artist": history_meta.get("artist", entry.get("artist", "")),
        "date": history_meta.get("date", entry.get("date", "")),
        "playlist_name": playlist_name,
        "playlist_rating_key": playlist_rating_key,
        "service": service,
        "source": history_meta.get("source", "setlist"),
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "venue": history_meta.get("venue", entry.get("venue", "")),
        "city": history_meta.get("city", entry.get("city", "")),
        "matched": matched_count,
        "missing": history_meta.get("missing", 0),
        "missing_tracks": history_meta.get("missing_tracks", []),
        # The per-song map the Plex summary is derived from: one row per
        # setlist position with the track it matched (or None). Only paths that
        # re-match supply it, so an absent map must not wipe a stored one.
        # Reconciling a fresh map against the stored one is the update path's
        # job (see add_to_playlist) — a fresh create must keep its own picks.
        "songs": history_meta.get("songs") or entry.get("songs", []),
    })
    history[setlist_id] = entry
    try:
        save_history(hist_file, history)
    except OSError as exc:
        logger.warning("Could not write history at %s (%s).", hist_file, exc)


def _write_summary(playlist, history_meta):
    """Rebuild the playlist's Plex summary from its live membership.

    Best-effort throughout, matching the poster policy: a failure here logs and
    returns, never undoing or blocking the create/update/edit that triggered it.
    Writes nothing when the summary would be empty, so a playlist with a good
    summary is never blanked by a path that lacks the data to rebuild it.
    """
    try:
        # addItems/removeItems don't refresh plexapi's cached items().
        playlist.reload()
        member_keys = {str(getattr(item, "ratingKey", ""))
                       for item in playlist.items()}
    except Exception as exc:
        logger.warning("Could not read playlist membership for the summary "
                       "(%s).", exc)
        return
    summary = _playlist_summary(history_meta, member_keys)
    if not summary:
        return
    try:
        playlist.editSummary(summary)
    except Exception as exc:
        logger.warning("Could not set playlist summary (%s).", exc)


def _song_states(songs, member_keys):
    """Split a per-song map into (added, declined, missing) by playlist state.

    Every setlist song is in exactly one state, decided by the map's matched
    ``rating_key`` against ``member_keys`` (the playlist's live membership):
    matched and present -> added, matched but absent -> declined, never matched
    -> missing. Derived at each write, never remembered, so a track removed in
    the editor — or directly in Plex — reads correctly next time.
    """
    members = {str(k) for k in member_keys or ()}
    added, declined, missing = [], [], []
    for song in songs:
        key = song.get("rating_key")
        if key is None or str(key) == "":
            missing.append(song)
        elif str(key) in members:
            added.append(song)
        else:
            declined.append(song)
    return added, declined, missing


def _summary_bullets(heading, songs):
    """A blank separator line, the section heading, then one bullet per song."""
    lines = ["", heading]
    for song in songs:
        title = (song.get("title") or "").strip()
        album = (song.get("album") or "").strip()
        pos = song.get("position")
        head = f"#{pos} {title}" if pos else title   # setlist position
        lines.append(f"  • {head} — {album}" if album else f"  • {head}")
    return lines


def _playlist_summary(history_meta, member_keys):
    """Build the Plex playlist summary text from show metadata.

    A self-contained record: which show this is, how much of it the playlist
    holds, the songs still missing from your library, and the
    setlist.fm source link. Derived — never merged into what's already there —
    so every write path can rebuild it from scratch.

    ``member_keys`` is the playlist's current membership (Plex rating keys);
    together with the entry's per-song map it decides each song's state. See
    ``_song_states``.

    Returns "" when there's nothing trustworthy to say — no metadata, a
    manually-built playlist, or a history entry predating the per-song map —
    so the caller skips the write rather than blanking a good summary.
    """
    meta = history_meta or {}
    # Manually-built playlists have no setlist behind them; the setlist-shaped
    # summary (counts, "full run", source link) would be misleading, so skip it.
    if not meta or meta.get("source") == "manual":
        return ""
    # Only songs with a title render as bullets, and the map is what the whole
    # summary is derived from — without it there is nothing honest to write.
    songs = [s for s in (meta.get("songs") or [])
             if (s.get("title") or "").strip()]
    if not songs:
        return ""

    added, declined, missing = _song_states(songs, member_keys)
    lines = []

    # Show header: enough to identify the gig if the playlist is ever renamed.
    artist = (meta.get("artist") or "").strip()
    place = ", ".join(p for p in ((meta.get("venue") or "").strip(),
                                  (meta.get("city") or "").strip()) if p)
    head = " · ".join(p for p in (artist, place) if p)
    if head:
        lines.append(head)
    if (meta.get("date") or "").strip():
        lines.append(meta["date"].strip())
    if lines:
        lines.append("")

    # Counts line. Two songs can share one recording (a medley, or a reprise
    # matching its parent), so note the smaller unique-track count when they
    # diverge — otherwise "N added" contradicts Plex's own item count.
    unique = len({str(s["rating_key"]) for s in added})
    note = ""
    if unique < len(added):
        note = (f" ({unique} unique track{'' if unique == 1 else 's'}; "
                "some songs share a recording)")
    counts = [f"{len(songs)} song{'' if len(songs) == 1 else 's'}",
              f"{len(added)} added{note}"]
    if declined:
        counts.append(f"{len(declined)} declined")
    if missing:
        counts.append(f"{len(missing)} missing")
    lines.append(" · ".join(counts) + ".")

    # Full run only when every setlist song is in the playlist — nothing
    # missing from the library and nothing declined.
    if not declined and not missing:
        lines += ["", "This is the full run of the show."]
    else:
        # Missing leads: it's the actionable list (the gaps in your library).
        if missing:
            lines += _summary_bullets(
                f"Missing ({len(missing)}) — not in your library:", missing)
        if declined:
            lines += _summary_bullets(
                f"Declined ({len(declined)}) — in your library, not added:",
                declined)

    url = meta.get("url")
    if url:
        lines += ["", f"Source: {url}"]
    lines.append("Created by Setlist-er-ator. 🤘")
    return "\n".join(lines)


# Playlist poster upload policy — the domain rule for what a poster may be set
# from. The web layer reads the multipart bytes and writes the temp file; the
# accept/reject decision lives here so it isn't business logic in the Flask
# layer. Keyed by MIME type -> the temp-file extension to save under.
POSTER_MAX_BYTES = 10 * 1024 * 1024   # ~10 MB
_POSTER_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png",
                      "image/webp": ".webp"}
# Derived once so the <input accept> string and the human size hint stay in
# lockstep with the validation above. The web layer threads these to the
# template and (via DOM data-attrs) the JS pre-check, so no build step is
# needed and the three consumers can't drift from this single source.
POSTER_ACCEPT = ",".join(_POSTER_EXTENSIONS)          # image/jpeg,image/png,image/webp
POSTER_MAX_LABEL = f"{POSTER_MAX_BYTES // (1024 * 1024)} MB"


def check_poster(mimetype, size):
    """Validate an uploaded poster against the allowed types and size ceiling.

    Returns ``(extension, reason)``: on acceptance the temp-file extension to
    use and ``None`` (e.g. ``(".jpg", None)``); on rejection ``None`` and a
    short reason (``(None, "too large")`` / ``(None, "unsupported type")``).
    """
    if size > POSTER_MAX_BYTES:
        return None, "too large"
    ext = _POSTER_EXTENSIONS.get(mimetype or "")
    if ext is None:
        return None, "unsupported type"
    return ext, None


def set_playlist_poster(playlist, poster_path):
    """Upload a poster image to a Plex playlist from a local file path.

    Sets both the poster and — best-effort — the square art, so square-grid
    clients show the image too. Fail-soft by design: any upload error is logged,
    never raised, so the caller's playlist save always survives an image hiccup
    (see ADR 0002). Returns True if the poster upload succeeded, False otherwise;
    that False is what surfaces the "couldn't set the image" warning in the web
    UI. A square-art-only failure just logs — the poster is the visible thumb.
    """
    try:
        playlist.uploadPoster(filepath=poster_path)
        poster_ok = True
    except Exception as exc:
        logger.warning("Could not set playlist poster (%s).", exc)
        poster_ok = False
    try:
        playlist.uploadSquareArt(filepath=poster_path)
    except Exception as exc:
        logger.warning("Could not set playlist square art (%s).", exc)
    return poster_ok


def create_playlist(config, name, rating_keys, history_meta=None,
                    poster_path=None):
    """Create a Plex playlist from track rating keys and record history.

    ``rating_keys`` is an ordered list of Plex track rating keys (as produced
    by gather_matches). ``history_meta`` is an optional dict of show fields
    (id/url/artist/date/missing) to record once the playlist exists.
    ``poster_path`` is an optional local image file to set as the playlist
    poster (best-effort — a failure never undoes the created playlist).
    Returns a CreateResult (final name + poster status). Raises PlexError on
    failure to create the playlist itself.
    """
    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
    except (PermissionError, ConnectionError) as exc:
        raise PlexError(str(exc)) from exc

    tracks = []
    seen = set()
    for key in rating_keys:
        # Dedupe: different setlist songs can resolve to the same track (e.g. a
        # medley), and it should appear in the playlist only once.
        if str(key) in seen:
            continue
        seen.add(str(key))
        try:
            tracks.append(plex.fetchItem(int(key)))
        except Exception:
            logger.warning("Could not fetch track %s; skipping.", key)
    if not tracks:
        raise PlexError("None of the matched tracks could be loaded "
                        "from Plex; playlist not created.")

    final_name = unique_playlist_name(plex, name)
    try:
        playlist = plex.createPlaylist(final_name, items=tracks)
    except plex_exceptions.PlexApiException as exc:
        raise PlexError(f"Failed to create playlist: {exc}") from exc

    # Record a self-contained summary (show, counts, missing-with-albums, source
    # link) on the Plex playlist, derived from what actually landed in it.
    _write_summary(playlist, history_meta)

    # Best-effort poster — None when no image was supplied, else the upload's
    # success. Runs after the playlist exists, so it can never undo creation.
    poster = set_playlist_poster(playlist, poster_path) if poster_path else None

    _record_history(final_name, getattr(playlist, "ratingKey", None),
                    len(tracks), history_meta)
    return CreateResult(final_name, poster)


def find_playlist(plex, rating_key=None, name=None):
    """Return a Plex Playlist by rating key (preferred) or exact title, or None.

    Matches within plex.playlists() so it works regardless of how the playlist
    is keyed, and tolerates the titleless objects that endpoint can yield.
    """
    try:
        playlists = plex.playlists()
    except Exception:
        return None
    if rating_key:
        for pl in playlists:
            if str(getattr(pl, "ratingKey", "")) == str(rating_key):
                return pl
    if name:
        for pl in playlists:
            if getattr(pl, "title", None) == name:
                return pl
    return None


def add_to_playlist(config, rating_key, name, rating_keys, history_meta=None):
    """Add tracks (by rating key) to an existing playlist; record history.

    Add-only: skips keys already in the playlist (and dedupes). Returns
    (playlist_title, added_count). Raises PlexError if the playlist is gone.
    """
    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
    except (PermissionError, ConnectionError) as exc:
        raise PlexError(str(exc)) from exc

    playlist = find_playlist(plex, rating_key=rating_key, name=name)
    if playlist is None:
        raise PlexError("That playlist no longer exists in Plex — "
                        "create a new one instead.")

    existing = set()
    try:
        for item in playlist.items():
            key = getattr(item, "ratingKey", None)
            if key is not None:
                existing.add(str(key))
    except Exception:
        pass

    tracks = []
    seen = set()
    for key in rating_keys:
        if str(key) in existing or str(key) in seen:
            continue
        seen.add(str(key))
        try:
            tracks.append(plex.fetchItem(int(key)))
        except Exception:
            logger.warning("Could not fetch track %s; skipping.", key)

    if tracks:
        try:
            playlist.addItems(tracks)
        except plex_exceptions.PlexApiException as exc:
            raise PlexError(f"Failed to add tracks: {exc}") from exc

    # Reconcile the re-match against what the playlist already holds *before*
    # anything reads the map: this update re-matched the whole setlist, so its
    # defaults would otherwise displace selections the user made earlier and
    # those songs would read as declined.
    if history_meta and history_meta.get("songs"):
        stored = history_entry_for_playlist(getattr(playlist, "ratingKey", None))
        history_meta = {**history_meta,
                        "songs": _merge_song_maps((stored or {}).get("songs"),
                                                  history_meta["songs"])}

    # The whole point of the update: a song that was missing is now owned, so
    # the summary Plex is showing has gone stale. Rebuild it.
    _write_summary(playlist, history_meta)

    _record_history(playlist.title, getattr(playlist, "ratingKey", None),
                    len(existing) + len(tracks), history_meta)
    return playlist.title, len(tracks)


def delete_playlist(config, playlist_id):
    """Delete a Plex playlist and drop its history entry; return its title.

    If the playlist is already gone from Plex, its history entry is still
    cleared (so Delete also clears a stale row) and "" is returned.
    """
    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
    except (PermissionError, ConnectionError) as exc:
        raise PlexError(str(exc)) from exc

    playlist = find_playlist(plex, rating_key=playlist_id)
    title = ""
    if playlist is not None:
        title = playlist.title
        try:
            playlist.delete()
        except plex_exceptions.PlexApiException as exc:
            raise PlexError(f"Failed to delete playlist: {exc}") from exc
    _forget_history(playlist_id)
    return title


def list_playlists(config):
    """List Plex audio playlists, flagging which this app created and how.

    A playlist is "app-created" if its rating key appears in our history
    store; ``source`` says how — "setlist" for concert playlists (also the
    fallback for entries predating the field), "manual" for Build-page ones,
    None for playlists that aren't ours ("setlist" wins should a key carry
    both). Returns dicts sorted by title: ``{rating_key, title, count,
    app_created, source}`` (``count`` from ``leafCount``, so no per-playlist
    items() call). Raises PlexError on connect failure.
    """
    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
    except (PermissionError, ConnectionError) as exc:
        raise PlexError(str(exc)) from exc

    sources = {}
    for e in load_history(history_path()).values():
        key = e.get("playlist_rating_key")
        if key is not None and sources.get(str(key)) != "setlist":
            sources[str(key)] = e.get("source") or "setlist"
    try:
        playlists = plex.playlists(playlistType="audio")
    except Exception as exc:
        raise PlexError(f"Could not list playlists: {exc}") from exc

    rows = []
    for pl in playlists:
        key = getattr(pl, "ratingKey", None)
        rows.append({
            "rating_key": key,
            "title": getattr(pl, "title", "") or "",
            "count": getattr(pl, "leafCount", None),
            "app_created": str(key) in sources,
            "source": sources.get(str(key)),
        })
    rows.sort(key=lambda r: r["title"].lower())
    return rows


def get_playlist_tracks(config, rating_key):
    """Return an existing playlist's tracks for the editor.

    ``{rating_key, title, tracks: [{rating_key, artist, title, album,
    rating}]}`` (``rating`` is the Plex star rating, 0–10 or None).
    Raises PlexError if the playlist is gone.
    """
    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
    except (PermissionError, ConnectionError) as exc:
        raise PlexError(str(exc)) from exc

    playlist = find_playlist(plex, rating_key=rating_key)
    if playlist is None:
        raise PlexError("That playlist no longer exists in Plex.")

    tracks = [{
        "rating_key": getattr(item, "ratingKey", None),
        "artist": _track_artist_name(item),
        "title": getattr(item, "title", "") or "",
        "album": _track_album(item),
        "rating": getattr(item, "userRating", None),
    } for item in playlist.items()]
    return {"rating_key": getattr(playlist, "ratingKey", None),
            "title": playlist.title, "tracks": tracks}


def get_album_tracks(config, rating_key):
    """Return an album's tracks in playing order (for 'add whole album').

    ``[{rating_key, title, artist, album, rating}]`` (``rating`` is the Plex
    star rating, 0–10 or None). Raises PlexError if the album can't be
    loaded.
    """
    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
    except (PermissionError, ConnectionError) as exc:
        raise PlexError(str(exc)) from exc
    try:
        album = plex.fetchItem(int(rating_key))
        tracks = album.tracks()
    except Exception as exc:
        raise PlexError(f"Could not load album: {exc}") from exc
    return [{
        "rating_key": getattr(t, "ratingKey", None),
        "artist": _track_artist_name(t),
        "title": getattr(t, "title", "") or "",
        "album": _track_album(t),
        "rating": getattr(t, "userRating", None),
    } for t in tracks]


# --- auditioning ------------------------------------------------------------
# Auditioning is listening to a candidate track before selecting it (see
# CONTEXT.md). It plays to the browser, not to a Plex client, and is
# best-effort: a failure here never touches a selection. Delivery — direct vs
# proxied, direct vs transcoded — is ADR-0004.

# Codecs a browser decodes natively, so the original file can be streamed as-is
# and the scrubber works off a real Content-Length. Anything else is transcoded
# for compatibility only, never to save bandwidth. ALAC is the case that makes
# this branch load-bearing rather than theoretical.
AUDITION_DIRECT_CODECS = frozenset({"mp3", "aac", "flac", "opus", "vorbis", "pcm"})

# Transcode ceiling. Quality is not the point of an audition; playing at all is.
AUDITION_MAX_BITRATE = 256


def audition_mode(codec, force_transcode=False):
    """'direct' if a browser can decode `codec` as-is, else 'transcode'.

    `force_transcode` is the quality preference — on, every track transcodes
    regardless of codec, which is what makes an audition cheap over a remote
    link (a capped MP3 is roughly a quarter of the bytes of the average FLAC
    here). Compatibility still overrides preference in the other direction:
    turning it off never makes an undecodable codec play directly.

    Unknown or missing codecs transcode. That is the safe direction: a
    transcode that wasn't needed still plays, whereas a direct stream the
    browser can't decode is indistinguishable from silence.
    """
    if force_transcode or not codec:
        return "transcode"
    return "direct" if str(codec).strip().lower() in AUDITION_DIRECT_CODECS \
        else "transcode"


def audition_source(config, rating_key, force_transcode=False, offset=0):
    """Everything needed to audition one track.

    Returns ``{mode, rating_key, direct_url, duration, title, artist, album}``.
    ``mode`` is from `audition_mode`; ``duration`` is milliseconds (or None).

    ``direct_url`` is tokenized and points straight at Plex. Whether the client
    uses it or asks the app to proxy the same track instead is the *client's*
    call, since only the browser knows what it can reach (ADR-0004) — so the
    proxy URL is composed by the web layer, which owns its own routes, not
    here. Raises PlexError.
    """
    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
    except (PermissionError, ConnectionError) as exc:
        raise PlexError(str(exc)) from exc
    try:
        key = int(rating_key)
        track = plex.fetchItem(key)
        media = (getattr(track, "media", None) or [None])[0]
        if media is None:
            raise ValueError("track has no media")
        mode = audition_mode(getattr(media, "audioCodec", None), force_transcode)
        direct_url = _audition_direct_url(plex, track, media, mode, offset)
    except Exception as exc:
        raise PlexError(f"Could not load track: {exc}") from exc
    return {
        "mode": mode,
        "rating_key": key,
        "direct_url": direct_url,
        "duration": getattr(track, "duration", None),
        "title": getattr(track, "title", "") or "",
        "artist": _track_artist_name(track),
        "album": _track_album(track),
    }


def _audition_direct_url(plex, track, media, mode, offset=0):
    """The Plex URL an audition reads from, tokenized.

    Direct mode serves the original file part, which supports HTTP Range — that
    is what makes the scrubber accurate, and why `offset` is meaningless there
    (the client seeks by byte range instead).

    Transcode mode asks Plex for capped MP3, which serves no byte ranges at
    all: a Range request comes back 200 from the top of the file. `offset` is
    how you seek such a stream — Plex begins encoding at that many seconds in,
    so the client restarts the stream to move forward. Verified against the
    live server: offset=180 on a 369s track returns ~195s of audio.
    """
    if mode == "direct":
        part = (getattr(media, "parts", None) or [None])[0]
        if part is None or not getattr(part, "key", None):
            raise ValueError("track has no playable file part")
        return plex.url(part.key, includeToken=True)
    params = urlencode({
        "path": track.key,
        "mediaIndex": 0,
        "partIndex": 0,
        "protocol": "http",
        "offset": max(0, int(offset or 0)),
        "copyts": 0,
        "maxAudioBitrate": AUDITION_MAX_BITRATE,
        "X-Plex-Platform": "Chrome",
    })
    return plex.url(f"/music/:/transcode/universal/start.mp3?{params}",
                    includeToken=True)


def set_playlist(config, rating_key, name, rating_keys, poster_path=None):
    """Apply an edit to an existing Plex playlist in place: rename + set the
    exact ordered membership. Edits the real playlist so it keeps its rating key
    (and any history link).

    ``rating_keys`` is the desired ordered track list (deduped like
    create_playlist). ``poster_path`` is an optional local image file to set as
    the playlist poster (best-effort — never undoes the edit). Returns a
    SetResult (name + track count + poster status, None when no image was
    supplied). Raises PlexError.
    """
    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
    except (PermissionError, ConnectionError) as exc:
        raise PlexError(str(exc)) from exc

    playlist = find_playlist(plex, rating_key=rating_key)
    if playlist is None:
        raise PlexError("That playlist no longer exists in Plex.")

    final_title = name or playlist.title
    if name and name != playlist.title:
        try:
            playlist.editTitle(name)
        except plex_exceptions.PlexApiException as exc:
            raise PlexError(f"Failed to rename playlist: {exc}") from exc

    # Desired unique ordered keys; current items keyed by rating key.
    desired = list(dict.fromkeys(str(k) for k in rating_keys))
    current = {str(getattr(it, "ratingKey", "")): it for it in playlist.items()}

    drop = [it for k, it in current.items() if k not in desired]
    if drop:
        try:
            playlist.removeItems(drop)
        except plex_exceptions.PlexApiException as exc:
            raise PlexError(f"Failed to remove tracks: {exc}") from exc

    add = []
    for k in desired:
        if k not in current:
            try:
                add.append(plex.fetchItem(int(k)))
            except Exception:
                logger.warning("Could not fetch track %s; skipping.", k)
    if add:
        try:
            playlist.addItems(add)
        except plex_exceptions.PlexApiException as exc:
            raise PlexError(f"Failed to add tracks: {exc}") from exc

    # addItems/removeItems don't refresh plexapi's cached items(); reload so the
    # reorder loop sees the true membership (and playlistItemIDs for new tracks).
    if drop or add:
        playlist.reload()
    # Apply the exact order via moveItem (after=None puts a track first; each
    # subsequent one lands after the previous) — but only when the current order
    # differs, so a rename-only save doesn't fire a PUT per track.
    by_key = {str(getattr(it, "ratingKey", "")): it for it in playlist.items()}
    if list(by_key) != desired:
        prev = None
        for k in desired:
            item = by_key.get(k)
            if item is None:
                continue
            try:
                playlist.moveItem(item, after=prev)
            except plex_exceptions.PlexApiException as exc:
                raise PlexError(f"Failed to reorder tracks: {exc}") from exc
            prev = item

    count = sum(1 for k in desired if k in by_key)
    # Best-effort poster — after membership so an image hiccup never undoes the
    # edit. None when no image was supplied (empty field = keep existing poster).
    poster = set_playlist_poster(playlist, poster_path) if poster_path else None
    _update_history_playlist(getattr(playlist, "ratingKey", None),
                             final_title, count)
    # Membership just changed, so added/declined have too. Rebuild from the
    # stored per-song map — no matcher call, and a no-op for a playlist this
    # app never created (no history entry) or one predating the map.
    _write_summary(playlist,
                   history_entry_for_playlist(getattr(playlist, "ratingKey",
                                                      None)))
    return SetResult(final_title, count, poster)


def backfill_history(config):
    """Fill in missing_tracks for history entries that have a missing count but
    no track list (created before track-level history). Re-matches each setlist
    against the current library. Idempotent — only touches count-only entries.
    Returns the number of entries updated.
    """
    path = history_path()
    history = load_history(path)
    updated = 0
    for sid, entry in history.items():
        if not entry.get("missing") or entry.get("missing_tracks"):
            continue  # nothing missing, or already has the detail
        ref = entry.get("url") or entry.get("id")
        if not ref:
            continue
        label = entry.get("playlist_name", sid)
        try:
            result = gather_matches(config, parse_setlist_id(ref), None)
        except (SetlistError, PlexError, ValueError) as exc:
            logger.warning("Backfill skipped '%s' (%s).", label, exc)
            continue
        entry["missing_tracks"] = [
            {"position": r[0], "artist": r[1], "title": r[2],
             "album": (r[3] if len(r) > 3 else "")}
            for r in result["missing"]]
        entry["missing"] = len(entry["missing_tracks"])
        logger.info("Backfilled '%s' — %d missing.", label, entry["missing"])
        updated += 1
    if updated:
        save_history(path, history)
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create a Plex playlist from a setlist.fm show.")
    parser.add_argument(
        "setlist", nargs="?",
        help="setlist.fm setlist ID or full setlist URL")
    parser.add_argument(
        "--name", help="override the auto-generated playlist name")
    parser.add_argument(
        "--history", action="store_true",
        help="list previously created playlists and exit")
    parser.add_argument(
        "--backfill", action="store_true",
        help="fill in missing-track detail for old history entries and exit")
    parser.add_argument(
        "--quiet", action="store_true",
        help="suppress per-song match logging on stderr")
    parser.add_argument(
        "--force", action="store_true",
        help="re-process even if this setlist was processed before")
    parser.add_argument(
        "--no-history", action="store_true",
        help="do not read or write the processed-setlist history")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s", stream=sys.stderr)

    # --history just reads the local file — no setlist or Plex config needed.
    if args.history:
        print_history()
        return EXIT_OK

    # --backfill re-matches old shows, so it needs config (but no setlist arg).
    if args.backfill:
        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_CONFIG
        n = backfill_history(config)
        print(f"Backfilled {n} entr{'y' if n == 1 else 'ies'}." if n
              else "Nothing to backfill — all entries already have track detail.")
        return EXIT_OK

    if not args.setlist:
        parser.error(
            "a setlist ID or URL is required (or use --history / --backfill)")

    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG

    try:
        setlist_id = parse_setlist_id(args.setlist)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETLIST

    # Skip work if we've already built a playlist for this setlist (checked
    # before the API call to save quota). --force / --no-history bypass this.
    if not args.no_history:
        history = load_history(history_path())
        if not should_process(history, setlist_id, args.force,
                              sys.stdin.isatty()):
            return EXIT_OK

    # --- Match -----------------------------------------------------------
    try:
        result = gather_matches(config, setlist_id, args.name)
    except SetlistError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETLIST
    except PlexError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PLEX

    if not result["matched"]:
        print_report(result["playlist_name"], 0, result["missing"],
                     result["fuzzy"])
        print("No tracks matched — playlist not created.", file=sys.stderr)
        return EXIT_OK

    # --- Create the playlist ---------------------------------------------
    rating_keys = [m["rating_key"] for m in result["matched"]
                   if m["rating_key"] is not None]
    history_meta = None if args.no_history else {
        "id": setlist_id,
        "url": result["show"]["url"],
        "artist": result["show"]["artist"],
        "venue": result["show"]["venue"],
        "city": result["show"]["city"],
        "date": result["show"]["date"],
        "songs": song_map(result),
        "missing": len(result["missing"]),
        "missing_tracks": [
            {"position": r[0], "artist": r[1], "title": r[2],
             "album": (r[3] if len(r) > 3 else "")}
            for r in result["missing"]],
    }
    try:
        final_name = create_playlist(
            config, result["playlist_name"], rating_keys, history_meta).name
    except PlexError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PLEX

    added = len(dict.fromkeys(rating_keys))  # unique tracks actually added
    print_report(final_name, added, result["missing"], result["fuzzy"])
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

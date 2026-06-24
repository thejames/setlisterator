#!/usr/bin/env python3
"""Create a Plex music playlist from a setlist.fm show and report missing songs.

This script fetches a setlist from setlist.fm, matches each song against your
Plex music library, builds a playlist (in setlist order) from the tracks it
finds, and prints a report of what it could not match so you know what to buy.

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
import json
import logging
import os
import re
import sys
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path

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


SETLISTFM_API_BASE = "https://api.setlist.fm/rest/1.0/setlist/"

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


def normalize_aggressive(text):
    """Looser normalization: drop parentheticals, feat., leading 'the', and
    canonicalize Pt./Part, and/&. Used for fuzzy comparison."""
    if not text:
        return ""
    text = text.lower()
    # Drop bracketed/parenthetical asides: (live), [remastered], etc.
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)
    # Drop featured-artist clauses.
    text = re.sub(r"\b(feat|ft|featuring|with)\.?\b.*$", " ", text)
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


def fetch_setlist(setlist_id, api_key):
    """Fetch and return the raw setlist JSON dict from setlist.fm."""
    url = SETLISTFM_API_BASE + setlist_id
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.exceptions.RequestException as exc:
        raise ConnectionError(f"Could not reach setlist.fm: {exc}") from exc

    if resp.status_code == 404:
        raise LookupError(f"No setlist found with ID '{setlist_id}'.")
    if resp.status_code in (401, 403):
        raise PermissionError(
            "setlist.fm rejected the API key (check SETLISTFM_API_KEY).")
    if resp.status_code != 200:
        raise RuntimeError(
            f"setlist.fm returned HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError("setlist.fm returned a non-JSON response.") from exc


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


def _track_artist_name(track):
    """Best-effort album-artist name for a Plex track."""
    # grandparentTitle is the album artist; originalTitle often holds the
    # track's own (featured) artist when it differs from the album artist.
    return getattr(track, "grandparentTitle", None) or \
        getattr(track, "originalTitle", None) or ""


def _track_album(track):
    """The album a Plex track is pulled from (parentTitle)."""
    return getattr(track, "parentTitle", None) or ""


# A resolved match for one setlist song. tier is one of TIER_NAMES; source is
# 'scoped' (the artist's own tracks) or 'global' (fallback title search).
Match = namedtuple("Match", "track quality tier source")

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
    are demoted below every artist-confirmed match. Ties keep library order.
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
    ranked.sort(key=lambda r: r[0])  # stable: equal-rank ties keep input order
    return ranked


def _best_match(target_simple, target_aggr, tracks, setlist_artist, scoped):
    """Return the single best (overall_rank, track, quality, tier), or None."""
    ranked = _ranked_matches(target_simple, target_aggr, tracks,
                             setlist_artist, scoped)
    return ranked[0] if ranked else None


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
    Match(track, quality, tier, source).
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


def load_history(path):
    """Load the history dict; tolerate a missing or corrupt file."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        logger.warning("Could not read history at %s (%s); starting fresh.",
                       path, exc)
        return {}


def save_history(path, history):
    """Write the history dict atomically (temp file + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


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
    """Fill 'album' on history missing_tracks via MusicBrainz, lazily + cached:
    only items without an 'album' key are looked up, at most ``limit`` per call
    (bounds page latency; the rest resolve on later loads). Returns True if any
    entry changed, so the caller can persist.
    """
    changed = False
    done = 0
    for entry in history.values():
        for track in entry.get("missing_tracks", []):
            if "album" in track:
                continue
            if done >= limit:
                return changed
            track["album"] = lookup_album(track.get("artist", ""),
                                          track.get("title", ""))
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
        for pos, artist, title in missing:
            print(f"  {pos}. {artist} - {title}")

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

def gather_matches(config, setlist_id, name=None):
    """Fetch a setlist and match every song against the Plex library.

    Read-only: no playlist is created. Returns a dict with the show metadata,
    the resolved playlist name, and matched/missing/fuzzy lists (matched
    entries are deduped and carry the Plex track rating key so a playlist can
    be built later without re-matching). Raises SetlistError or PlexError (with
    a human-readable message) on failure.
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

    try:
        plex = connect_plex(config["plex_baseurl"], config["plex_token"])
        section = get_music_section(plex, config["music_library"])
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

    matched = []
    missing = []
    fuzzy = []
    songs = []   # the full setlist in order, each row flagged matched or not
    for position, title in enumerate(show["songs"], start=1):
        candidates = match_candidates(section, title, show["artist"],
                                      artist_tracks)
        if not candidates:
            missing.append((position, show["artist"], title))
            songs.append({"position": position, "title": title,
                          "matched": False, "artist": show["artist"]})
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
        songs.append({**entry, "matched": True})

    return {
        "setlist_id": setlist_id,
        "show": show,
        "playlist_name": playlist_name,
        "songs": songs,
        "matched": matched,
        "missing": missing,
        "fuzzy": fuzzy,
    }


def _record_history(playlist_name, playlist_rating_key, matched_count,
                    history_meta):
    """Write/merge the history entry for a created or updated playlist."""
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
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "matched": matched_count,
        "missing": history_meta.get("missing", 0),
        "missing_tracks": history_meta.get("missing_tracks", []),
    })
    history[setlist_id] = entry
    try:
        save_history(hist_file, history)
    except OSError as exc:
        logger.warning("Could not write history at %s (%s).", hist_file, exc)


def create_playlist(config, name, rating_keys, history_meta=None):
    """Create a Plex playlist from track rating keys and record history.

    ``rating_keys`` is an ordered list of Plex track rating keys (as produced
    by gather_matches). ``history_meta`` is an optional dict of show fields
    (id/url/artist/date/missing) to record once the playlist exists. Returns
    the final (possibly suffix-deduped) playlist name. Raises PlexError on
    failure.
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

    _record_history(final_name, getattr(playlist, "ratingKey", None),
                    len(tracks), history_meta)
    return final_name


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

    _record_history(playlist.title, getattr(playlist, "ratingKey", None),
                    len(existing) + len(tracks), history_meta)
    return playlist.title, len(tracks)


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
        entry["missing_tracks"] = [{"artist": a, "title": t}
                                   for (_pos, a, t) in result["missing"]]
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
        "date": result["show"]["date"],
        "missing": len(result["missing"]),
        "missing_tracks": [{"artist": a, "title": t}
                           for (_pos, a, t) in result["missing"]],
    }
    try:
        final_name = create_playlist(
            config, result["playlist_name"], rating_keys, history_meta)
    except PlexError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PLEX

    added = len(dict.fromkeys(rating_keys))  # unique tracks actually added
    print_report(final_name, added, result["missing"], result["fuzzy"])
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

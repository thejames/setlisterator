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
"""

import argparse
import json
import logging
import os
import re
import sys
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


def _best_match(target_simple, target_aggr, tracks, setlist_artist, scoped):
    """Return (overall_rank, track, quality, tier) for the best track, or None.

    When ``scoped`` is True the tracks are already known to be by the setlist
    artist, so artist matching is assumed; otherwise it is checked per track
    and artist mismatches are demoted below every artist-confirmed match.
    """
    best = None
    for track in tracks:
        title_rank = _title_rank(target_simple, target_aggr, track.title)
        if title_rank is None:
            continue
        artist_ok = True if scoped else \
            artists_match(_track_artist_name(track), setlist_artist)
        overall = title_rank if artist_ok else title_rank + 4
        quality = "exact" if (artist_ok and title_rank == 0) else "fuzzy"
        if best is None or overall < best[0]:
            best = (overall, track, quality, TIER_NAMES[title_rank])
        if overall == 0:
            break  # can't beat an exact artist+title hit
    return best


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


def match_song(section, title, setlist_artist, artist_tracks):
    """Find the best Plex track for a setlist song.

    Strategy: first match against ``artist_tracks`` (the setlist artist's own
    tracks, fetched once) using local normalization — this sidesteps Plex's
    search tokenizer, which can miss tracks over punctuation/Unicode quirks.
    Only if that finds nothing do we fall back to a global title search, which
    also covers songs performed as covers of other artists.

    Returns a Match (track, quality, tier, source), or None for no match.
    """
    target_simple = normalize_simple(title)
    target_aggr = normalize_aggressive(title)

    scoped = _best_match(target_simple, target_aggr, artist_tracks,
                         setlist_artist, scoped=True)
    if scoped is not None:
        _, track, quality, tier = scoped
        return Match(track, quality, tier, "scoped")

    candidates = _search_candidates(section, title, target_aggr)
    best = _best_match(target_simple, target_aggr, candidates,
                       setlist_artist, scoped=False)
    if best is None:
        return None
    _, track, quality, tier = best
    return Match(track, quality, tier, "global")


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
# Reporting
# ---------------------------------------------------------------------------

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
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create a Plex playlist from a setlist.fm show.")
    parser.add_argument(
        "setlist", help="setlist.fm setlist ID or full setlist URL")
    parser.add_argument(
        "--name", help="override the auto-generated playlist name")
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

    load_dotenv()
    api_key = os.environ.get("SETLISTFM_API_KEY")
    plex_baseurl = os.environ.get("PLEX_BASEURL")
    plex_token = os.environ.get("PLEX_TOKEN")
    music_library = os.environ.get("PLEX_MUSIC_LIBRARY", "Music")

    missing_config = [
        var for var, val in (
            ("SETLISTFM_API_KEY", api_key),
            ("PLEX_BASEURL", plex_baseurl),
            ("PLEX_TOKEN", plex_token),
        ) if not val
    ]
    if missing_config:
        print("Missing required environment variable(s): "
              + ", ".join(missing_config), file=sys.stderr)
        print("Set them in a .env file or your environment. See the module "
              "docstring for details.", file=sys.stderr)
        return EXIT_CONFIG

    # --- Fetch & parse setlist -------------------------------------------
    try:
        setlist_id = parse_setlist_id(args.setlist)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETLIST

    # Skip work if we've already built a playlist for this setlist (checked
    # before the API call to save quota). --force / --no-history bypass this.
    hist_file = history_path()
    history = {} if args.no_history else load_history(hist_file)
    if not args.no_history and not should_process(
            history, setlist_id, args.force, sys.stdin.isatty()):
        return EXIT_OK

    try:
        data = fetch_setlist(setlist_id, api_key)
    except (LookupError, PermissionError, ConnectionError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETLIST

    show = extract_show(data)
    if not show["songs"]:
        print(f"Setlist '{setlist_id}' has no playable songs "
              "(empty setlist or all tape/intro entries).", file=sys.stderr)
        return EXIT_SETLIST

    playlist_name = args.name or build_playlist_name(show)
    logger.info("Setlist: %s — %s, %s (%s) — %d songs", show["artist"],
                show["venue"], show["city"], show["date"], len(show["songs"]))
    if show["url"]:
        logger.info("Source:  %s", show["url"])

    # --- Connect to Plex --------------------------------------------------
    try:
        plex = connect_plex(plex_baseurl, plex_token)
        section = get_music_section(plex, music_library)
    except (PermissionError, ConnectionError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PLEX

    # Resolve the setlist artist once and pull their tracks for local matching.
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

    # --- Match each song --------------------------------------------------
    matched_tracks = []
    missing = []
    fuzzy = []
    seen_keys = set()
    for position, title in enumerate(show["songs"], start=1):
        match = match_song(section, title, show["artist"], artist_tracks)
        if match is None:
            missing.append((position, show["artist"], title))
            logger.info("  ✗ %2d. %s → no match", position, title)
            continue
        track = match.track
        logger.info("  ✓ %2d. %s → %r  [%s/%s]", position, title,
                    track.title, match.tier, match.source)
        if match.quality == "fuzzy":
            got = f"{_track_artist_name(track)} - {track.title}"
            fuzzy.append((position, f"{show['artist']} - {title}", got))
        # Dedupe: a medley track can match more than one setlist song.
        key = getattr(track, "ratingKey", None) or id(track)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        matched_tracks.append(track)

    if not matched_tracks:
        print_report(playlist_name, 0, missing, fuzzy)
        print("No tracks matched — playlist not created.", file=sys.stderr)
        return EXIT_OK

    # --- Create the playlist ---------------------------------------------
    final_name = unique_playlist_name(plex, playlist_name)
    try:
        plex.createPlaylist(final_name, items=matched_tracks)
    except plex_exceptions.PlexApiException as exc:
        print(f"Failed to create playlist: {exc}", file=sys.stderr)
        return EXIT_PLEX

    # Record the run only now that a playlist actually exists.
    if not args.no_history:
        history[setlist_id] = {
            "id": setlist_id,
            "url": show["url"],
            "artist": show["artist"],
            "date": show["date"],
            "playlist_name": final_name,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
            "matched": len(matched_tracks),
            "missing": len(missing),
        }
        try:
            save_history(hist_file, history)
        except OSError as exc:
            logger.warning("Could not write history at %s (%s).", hist_file, exc)

    print_report(final_name, len(matched_tracks), missing, fuzzy)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

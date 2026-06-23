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

Example usage:

    # By setlist ID
    python setlist_to_plex.py 63de4613

    # By full setlist.fm URL
    python setlist_to_plex.py "https://www.setlist.fm/setlist/phish/2023/madison-square-garden-new-york-ny-63de4613.html"

    # Override the auto-generated playlist name
    python setlist_to_plex.py 63de4613 --name "Phish @ MSG NYE"
"""

import argparse
import os
import re
import sys
from datetime import datetime

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


def match_song(section, title, setlist_artist):
    """Find the best Plex track for a setlist song.

    Returns (track, quality) where quality is 'exact', 'fuzzy', or None.
    'exact'  -> artist matches and simple-normalized titles are identical.
    'fuzzy'  -> matched only after looser normalization, or artist differs.
    None     -> no acceptable match found.
    """
    target_simple = normalize_simple(title)
    target_aggr = normalize_aggressive(title)

    candidates = []
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

    best = None        # (rank, track, quality); lower rank is better
    for track in candidates:
        cand_simple = normalize_simple(track.title)
        cand_aggr = normalize_aggressive(track.title)
        artist_ok = artists_match(_track_artist_name(track), setlist_artist)

        title_exact = cand_simple == target_simple and target_simple != ""
        title_fuzzy = cand_aggr == target_aggr and target_aggr != ""

        if artist_ok and title_exact:
            rank, quality = 0, "exact"
        elif artist_ok and title_fuzzy:
            rank, quality = 1, "fuzzy"
        elif title_exact:
            rank, quality = 2, "fuzzy"   # title nails it but artist differs
        elif title_fuzzy:
            rank, quality = 3, "fuzzy"
        else:
            continue

        if best is None or rank < best[0]:
            best = (rank, track, quality)
        if rank == 0:
            break  # can't do better than an exact artist+title hit

    if best is None:
        return None, None
    return best[1], best[2]


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
    args = parser.parse_args(argv)

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
    print(f"Setlist: {show['artist']} — {show['venue']}, {show['city']} "
          f"({show['date']}) — {len(show['songs'])} songs")
    if show["url"]:
        print(f"Source:  {show['url']}")

    # --- Connect to Plex --------------------------------------------------
    try:
        plex = connect_plex(plex_baseurl, plex_token)
        section = get_music_section(plex, music_library)
    except (PermissionError, ConnectionError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PLEX

    # --- Match each song --------------------------------------------------
    matched_tracks = []
    missing = []
    fuzzy = []
    for position, title in enumerate(show["songs"], start=1):
        track, quality = match_song(section, title, show["artist"])
        if track is None:
            missing.append((position, show["artist"], title))
            continue
        matched_tracks.append(track)
        if quality == "fuzzy":
            got = f"{_track_artist_name(track)} - {track.title}"
            fuzzy.append((position, f"{show['artist']} - {title}", got))

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

    print_report(final_name, len(matched_tracks), missing, fuzzy)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

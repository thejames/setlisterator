# setlist_to_plex

Create a Plex music playlist from a [setlist.fm](https://www.setlist.fm/) show,
and report which songs are missing from your library so you know what to buy.

## Install

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` and fill it in:

| Variable             | Required | Description                                      |
| -------------------- | -------- | ------------------------------------------------ |
| `SETLISTFM_API_KEY`  | yes      | setlist.fm API key (https://api.setlist.fm/docs/)|
| `PLEX_BASEURL`       | yes      | e.g. `http://localhost:32400`                    |
| `PLEX_TOKEN`         | yes      | your Plex `X-Plex-Token`                          |
| `PLEX_MUSIC_LIBRARY` | no       | library section name (default `Music`)           |

## Usage

```bash
# By setlist ID
./.venv/bin/python setlist_to_plex.py 63de4613

# By full setlist.fm URL (the ID is parsed out)
./.venv/bin/python setlist_to_plex.py "https://www.setlist.fm/setlist/primus/2026/td-amp-ballantyne-charlotte-nc-6b756686.html"

# Override the auto-generated playlist name
./.venv/bin/python setlist_to_plex.py 63de4613 --name "Phish @ MSG"
```

The playlist name defaults to `{artist} - {venue}, {city} ({YYYY-MM-DD})`. If a
playlist with that name already exists, a numeric suffix is appended rather than
failing.

## How matching works

Each setlist song is matched to a Plex track in two stages:

1. **Artist-scoped (primary).** The setlist artist is resolved once in your
   library and *all* of their tracks are pulled and compared locally. This
   sidesteps Plex's search tokenizer, which can otherwise miss tracks over
   punctuation or Unicode quirks (e.g. an ASCII hyphen `-` in the setlist vs a
   typographic hyphen `‐` in your library) and over result truncation on common
   titles.
2. **Global search (fallback).** Only if the artist isn't in your library, or a
   song isn't among their tracks (covers of other artists), a global title
   search is used.

Titles are compared with normalization (lowercased, punctuation stripped,
whitespace collapsed) across four tiers:

| Tier  | Quality | Example                                                       |
| ----- | ------- | ------------------------------------------------------------- |
| exact | exact   | `Run Like an Antelope!!!` == `run like an antelope`           |
| loose | fuzzy   | `Wilson, Pt. 2` ≈ `Wilson Part 2`; drops `(...)`, `feat. ...` |
| medley| fuzzy   | `Hello Skinny / Constantinople` matches `Hello Skinny`        |
| prefix| fuzzy   | `Tommy the Cat - Live` matches `Tommy the Cat` (2+ word title)|

Anything past the exact tier is reported under **FUZZY MATCHES** so you can
spot-check for false positives. A medley track that matches more than one
setlist song is added to the playlist only once.

## Output

- Playlist name and number of tracks added.
- **MISSING TRACKS** — songs with no match, as `{position}. {artist} - {title}`.
- **FUZZY MATCHES** — non-exact matches, setlist title vs the Plex track.

Exit codes: `0` success, `2` config error, `3` setlist error, `4` Plex error.

## Tests

```bash
./.venv/bin/pip install pytest
./.venv/bin/python -m pytest
```

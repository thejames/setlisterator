# Setlist-er-ator

Turns a setlist.fm show into a Plex playlist. This glossary covers the language of the **preview** step — the human review screen between matching a setlist and creating the playlist — and of the **playlist summary** the app writes back to Plex.

## Language

**Preview session**:
One pass through the preview screen for a single setlist, from the first match to either creating the playlist or leaving the page. State lives only in the page; leaving discards it.
_Avoid_: Edit session, draft.

**Selection**:
The chosen Plex track for one song in the setlist, plus whether that song is included in the playlist. Every setlist song has at most one selection.
_Avoid_: Pick, choice, match (a *match* is what the matcher proposes; a *selection* is what the user settles on).

**Touched selection**:
A selection the user has explicitly acted on — changing the version, re-pointing it via search, accepting or rejecting a fuzzy match, filling a missing song, or toggling its include state. Intent-based, not diff-based: interacting with a row freezes it even if the resulting value equals what the matcher proposed. The opposite is an **untouched** selection, still exactly as the matcher proposed it.
_Avoid_: Edited, dirty, manual.

**Sticky**:
The property that a touched selection survives a re-match within the same preview session. Changing the preferred album re-matches only untouched selections; touched ones are frozen.
_Avoid_: Pinned, locked, persisted (nothing is written to disk — sticky is in-session only).

**Preferred album**:
The album the matcher favours when a setlist song appears on more than one album in the library. Applies only to untouched selections.
_Avoid_: Preferred release, source album.

**Playlist poster**:
The image shown as a playlist's thumbnail in Plex — optionally set from an uploaded file when a playlist is created or later edited in the app. What users informally call the playlist's "folder image".
_Avoid_: Folder image, cover, artwork, thumbnail.

**Playlist summary**:
The text the app writes to a Plex playlist's summary field: a self-contained record of how much of the show you have, which songs are still worth buying, and a link back to setlist.fm. Derived, never authored — it is rewritten from scratch whenever the playlist changes, so it is never edited in place or merged.
_Avoid_: Description, notes, blurb.

**Added / Declined / Missing**:
The three states a setlist song can be in, relative to one playlist. **Added** — its matched track is in the playlist. **Declined** — it matched a track in your library, but that track is not in the playlist. **Missing** — nothing in your library matched it, so it's a buy candidate. Every setlist song is in exactly one state, and the state is derived fresh at each write from the stored match data plus the playlist's live membership.
_Avoid_: Excluded (an *action* the user took on the preview screen; declined is a *state*, and also covers a track removed later in the editor or directly in Plex). Unmatched, absent, skipped.

The distinction that matters: **declined means you own it, missing means you don't.** Only missing songs belong on a buy list.

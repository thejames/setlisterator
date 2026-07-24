# Setlist-er-ator

Turns a setlist.fm show into a Plex playlist. This glossary covers the language of the **preview** step — the human review screen between matching a setlist and creating the playlist.

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

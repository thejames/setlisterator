# Setlist-er-ator

Turns a setlist.fm show into a Plex playlist. This glossary covers the language of the **preview** step — the human review screen between matching a setlist and creating the playlist — of **auditioning** a track before choosing it, and of the **playlist summary** the app writes back to Plex.

## Language

**Preview session**:
One pass through the preview screen for a single setlist, from the first match to either creating the playlist or leaving the page. State lives only in the page; leaving discards it.
_Avoid_: Edit session, draft.

**Selection**:
The chosen Plex track for one song in the setlist, plus whether that song is included in the playlist. Every setlist song has at most one selection.
_Avoid_: Pick, choice, match (a *match* is what the matcher proposes; a *selection* is what the user settles on).

**Touched selection**:
A selection the user has explicitly acted on — changing the version, re-pointing it via search, accepting or rejecting a fuzzy match, filling a missing song, or toggling its include state. Intent-based, not diff-based: interacting with a row freezes it even if the resulting value equals what the matcher proposed. The opposite is an **untouched** selection, still exactly as the matcher proposed it.
**Auditioning is not a touch** — listening is how you decide, not a decision.
_Avoid_: Edited, dirty, manual.

**Audition**:
Listening to a candidate track in the app to check it's the song you expect, before deciding whether to select it. Any candidate can be auditioned, not just the selected one, so two versions of a song can be compared without committing to either — an audition leaves the selection untouched and still eligible for re-match. Not a Plex playback session: it plays to your browser, not to a Plex client.
_Avoid_: Preview (that's the review screen), play/playback (that's what Plex does to a client), sample, scrub (that's the control, not the act).

**Sticky**:
The property that a touched selection survives a re-match within the same preview session. Changing the preferred album re-matches only untouched selections; touched ones are frozen.
_Avoid_: Pinned, locked, persisted (nothing is written to disk — sticky is in-session only).

**Preferred album**:
The album the matcher favours when a setlist song appears on more than one album in the library. Always explicitly chosen — never inferred from the setlist — and applies only to untouched selections. Absent one, the **earliest album** decides.
_Avoid_: Preferred release, source album.

**Earliest album**:
The default source for a version — the oldest-dated album in your library carrying that song. Applied per song, so songs in one setlist can come from different albums. Used whenever no preferred album is named, and only beneath title match quality, so a better title always wins over an older album. An album with no year sorts last, never first: undated means unknown, not old.
_Avoid_: Earliest release, first release, original release (a *release* is the album setlist.fm attributes a song to), oldest track.

**Playlist poster**:
The image shown as a playlist's thumbnail in Plex — optionally set from an uploaded file when a playlist is created or later edited in the app. What users informally call the playlist's "folder image".
_Avoid_: Folder image, cover, artwork, thumbnail.

**Playlist summary**:
The text the app writes to a Plex playlist's summary field: a self-contained record of how much of the show you have, which songs are still missing from your library, and a link back to setlist.fm. Derived, never authored — it is rewritten from scratch whenever the playlist changes, so it is never edited in place or merged.
_Avoid_: Description, notes, blurb.

**Added / Declined / Missing**:
The three states a setlist song can be in, relative to one playlist. **Added** — its matched track is in the playlist. **Declined** — it matched a track in your library, but that track is not in the playlist. **Missing** — nothing in your library matched it, so it's a gap to fill. Every setlist song is in exactly one state, and the state is derived fresh at each write from the stored match data plus the playlist's live membership.
_Avoid_: Excluded (an *action* the user took on the preview screen; declined is a *state*, and also covers a track removed later in the editor or directly in Plex). Unmatched, absent, skipped.

The distinction that matters: **declined means it's in your library, missing means it isn't.** Only missing songs belong on the missing list.

All three states describe your *library*, never your ownership. A missing song may well be one you own on vinyl, on a CD, or in another service — the app only knows it isn't in Plex. Hence **missing**, never "to buy": what you do about a gap is your business, and buying is only one of the options.
_Avoid_: Buy list, shopping list, wanted, to buy (all presume the gap is closed by a purchase).

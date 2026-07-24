# Preferred-album re-match via a JSON endpoint, not a full-page submit

Changing the **Preferred album** on the preview screen must re-match the setlist while keeping the user's **touched selections** sticky (see `CONTEXT.md`). We do this by having the album `<select>` POST to a small JSON endpoint (`/rematch`) that reuses the authoritative matcher (`gather_matches` → `_prefer_album_first`) and returns the fresh per-song candidates; the client then patches only untouched rows in place, leaving touched rows frozen. The previous behaviour — a full `<form>` submit to `/preview` that re-rendered the page — is what discarded every in-page selection.

## Considered options

- **Client-side re-order (rejected).** The multi-match dropdowns already carry every candidate in the DOM, so the reorder *could* run entirely in JavaScript with no network. Rejected because it forks the ranking rules — the medley exclusion in `_prefer_album_first` and the tier semantics that let a preferred-album (often looser-tier "…(Live)") version outrank an exact match — into a second copy that the network-free Python test suite cannot cover. This repo treats the matcher as its crown-jewel single source of truth (see `CLAUDE.md`).
- **JSON re-match endpoint (chosen).** Keeps all ranking in Python, offline-testable, and lets the client stay a dumb patcher.

## Consequences

- No-JS users keep the old behaviour: the album form's **Apply** button still does a full `/preview` submit, which resets selections. Accepted — the app is a local, JS-enabled personal tool.
- `/rematch` re-runs the *whole* match (re-fetches the setlist, re-pulls the artist's tracks) on every album change, exactly as today's **Apply** button already does — no worse per toggle, and now without a full-page reload. A later optimisation could cache the match and reorder only, since the candidate set is invariant across album choices; not done now.

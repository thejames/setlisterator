# Playlist poster upload is best-effort

The web app lets you attach an image (Plex **playlist poster**, see `CONTEXT.md`) to a playlist, uploaded from your browser, on all three write paths: setlist create, manual Build, and the existing-playlist editor. We decided the image is **always best-effort**: an image problem — unsupported type, oversized file, Plex rejecting it, a network blip — **never blocks or undoes the underlying save**. The playlist is created or updated regardless, and we show a soft warning if the poster couldn't be set.

The alternative was to validate the file up front and reject the submit so the user could pick another. We rejected it because on the setlist flow `/create` is the payoff of a whole **preview session** (picks, sticky selections, all carried in the form); bouncing to an error page over a bad image would discard that hard-won state. The Build page and editor share the same stance for consistency.

## Consequences

- One shared helper, `set_playlist_poster(playlist, poster_path) → bool`, does the upload — `uploadPoster` (its result is the return) plus a best-effort `uploadSquareArt` (log-only). Both `create_playlist` and `set_playlist` grow an optional `poster_path` and call it; the editor route reaches the same helper. The poster result drives the user-facing warning.
- `create_playlist`'s return grows from a bare name string to carry the poster status (`None` when no image was supplied, so the CLI path is unaffected).
- The editor **redirects** to `/playlists` on save, with no result page to warn on, so a poster failure there surfaces via a redirect notice banner rather than an inline block. On the editor an **empty** file field means "leave the existing poster untouched" — a save never clobbers a poster the user didn't replace.
- We deliberately do **not** set Flask's `MAX_CONTENT_LENGTH`: a 413 would reject the whole POST and lose the form (the preview session on create), exactly what this decision exists to prevent. Oversized files are accepted then declined, not rejected at the door.
- A no-JS user gets only the post-submit warning; the client-side type/size pre-check is a progressive enhancement, not the safety net.

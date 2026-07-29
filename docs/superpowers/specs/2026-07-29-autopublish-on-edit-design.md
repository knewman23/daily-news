# Republishing when the archive is edited by hand

**Date:** 2026-07-29
**Status:** approved, shipped

## The problem

Skipping or restoring a topic changes what the published page should show, but
the hosted copy only ever updated from the 11am run. So between edits the live
site silently disagreed with the local one, with nothing saying so.

## Not a push per click

The obvious reading of "push when I skip something" is one commit and one push
per click. Triaging a day is half a dozen toggles in a row, which would be six
near-identical commits and six queued Pages builds — GitHub serialises those, so
the site would be minutes behind while the history filled with noise.

So requests **coalesce**: every edit restarts a quiet timer, default 15s, and one
publish covers whatever arrived meanwhile. Six toggles become one commit reading
`news: 2026-07-29 topic visibility (6 changes)`.

## `src/autopublish.py`

A `Publisher` that request threads can call. `publish.publish()` already does the
dangerous parts correctly — stages only `site/`, commits nothing when nothing
changed, never raises — so this module is only about *when*.

Its three rules:

- **A publish failure never fails the edit.** The digest is already written by the
  time this runs. `request()` returns immediately and nothing here raises into a
  request handler.
- **One publish at a time.** A change arriving mid-push re-arms the timer instead
  of starting a second git against the same repository.
- **Nothing is lost.** A batch is cleared only once handed to `publish()`, and a
  change arriving during a publish re-arms for the next one. `flush()` on ctrl-c
  publishes an edit still inside its quiet window rather than dropping it.

`publish.publish()` gained a `summary` argument. Its default subject is
`news: <date> (N topics)`, which would be a lie about a hand edit — that is a new
day's news, and this is not.

## Status, not silence

A background push that failed would leave the reader believing the live site
matches when it does not. `GET /api/publish` reports
`{state, message, pending}`, and the page shows a small line in the toolbar:
`publishing soon…` → `publishing…` → `published ✓`, or `publish failed ⚠` with the
git error as its tooltip. Live-only, via `data-live-only`, so the published build
neither renders it nor has a server to ask.

The page polls every two seconds while anything is in flight, and stops once the
state settles back to idle — the server clears a finished result after 20s, which
is what makes the line disappear on its own.

## The bug this design walked into

A finished result is cleared by a timer. The first version compared the state
string: clear if the state is still what I settled on. Two publishes in a row both
settle on `published`, so the **first** timer would clear the **second** result
early — and the same bug on a `failed` result silently retracts the only warning
that the hosted copy is behind.

Found by watching `/api/publish` while publishing twice inside one linger window,
not by reading the code. Fixed with a generation counter: a linger may only clear
the result it was armed for.

## Verification

`pytest` covers the module with the timer and `publish()` injected — a test that
waited out a real 15s window would be slower than the rest of the suite together,
and testing "did it wait" by waiting tests nothing. 26 tests, including the
mid-publish race and both stale-linger cases.

End-to-end, the git path was driven from a real browser click against a
**throwaway repo pushing to a local bare remote**, so the real repository and the
real `news/` were never touched. Confirmed: the commit lands on the remote with
the right subject, two rapid edits produce one commit reading "(2 changes)", the
published JSON reflects the change, and neither the journal nor any skipped
topic's body appears anywhere in it.

That run also caught an unanchored `data/` in the *fixture's* `.gitignore`, which
excluded `site/data/` and published an empty archive — the exact failure the real
`.gitignore` carries a comment about. The real one is correct; the fixture was
not.

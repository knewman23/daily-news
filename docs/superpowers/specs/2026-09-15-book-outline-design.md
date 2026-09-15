# Outlining a book from the archive

**Date:** 2026-09-15
**Status:** approved, not yet shipped

## The problem

Fifty-six days of digests are roughly 107,000 words across 1,364 topics. That is
already a book's worth of raw material, and it grows every morning. But it is a
pile of days, not a structure: there is no way to ask which stories recur, which
died after a news cycle, or what the archive is actually *about*.

The goal is a living outline — parts and threads derived from the archive, kept
current as new days arrive, reviewable locally.

## What this is not

**Not a thesis.** The archive holds summarized events and nothing else: all 56
days have empty note markers, so no point of view has ever been written down.
The outline organises material. It does not supply an argument, and it should
not be mistaken for one.

**Not a view of the period.** The corpus is what seven Instagram accounts chose
to post, five of them aligned commentary accounts. Frequency here measures
creator attention, not significance. The design answers this by computing
per-thread provenance (below) rather than by pretending the bias is absent.

## Two levels, and why

Items are classified into **threads** — narrow named story arcs such as "Iran war
authorization fight". Threads are grouped into **parts**, which are the
book-shaped units.

Classification targets threads only. Parts are nothing but lists of thread ids,
so rearranging, splitting, merging or renaming a part rewrites `outline.json`
alone and reclassifies not one item.

This is the whole reason for two levels. A single-level model forces a choice
between stable assignments and an evolving outline; splitting them buys both.

## Storage

`book/` is gitignored in this repository and is its own git repo pointing at a
private remote. It must never enter `site/`, which is committed to a public
repository and served by GitHub Pages.

```
book/
  outline.json          the frozen structure: parts -> threads
  days/2026-09-15.json  one file per classified day
```

Nothing under `news/` is touched. Assignments are a sidecar keyed by headline,
which keeps digest files under the sole ownership of `notes.py` and `topics.py`
as `CLAUDE.md` requires. A book feature must not be able to damage the journal.

### `outline.json`

```json
{
  "version": 3,
  "revised_at": "2026-09-15",
  "parts": [
    { "id": "unauthorized-war", "title": "The War Nobody Authorized",
      "thread_ids": ["iran-authorization", "force-readiness"] }
  ],
  "threads": [
    { "id": "iran-authorization",
      "title": "Iran war authorization fight",
      "description": "Congressional attempts to constrain or retroactively
                      authorize hostilities",
      "created": "2026-09-15" }
  ]
}
```

### `days/<date>.json`

```json
{ "date": "2026-09-15", "outline_version": 3,
  "assignments": [
    { "headline": "Republican files eight articles of impeachment...",
      "thread_id": "iran-authorization" } ],
  "unfiled": [
    { "headline": "Developer seeks land exchange...",
      "why": "no thread covers public-lands policy" } ] }
```

Every day records the `outline_version` it was filed under, so a structural
revision can re-file exactly the stale days rather than all of them.

Thread ids are slugs and are never reused. A retired thread's id stays dead, so
an old day file can never silently re-point at different meaning.

**Provenance is computed, never stored.** The review page joins assignments back
to the `sources:` line already in `news/*.md`. A thread fed entirely by one
handle is therefore visible as such, with no duplicated data to drift.

## Daily flow

After `run_daily.py` writes the digest, a new step classifies that day:

1. Read the day's topics through `digest.py` — the same parser the server uses.
2. Read `book/outline.json`.
3. One `claude -p` call: the thread list with descriptions, plus the day's
   headlines and snippets. Returns a thread id per headline, or unfiled with a
   reason.
4. Write `book/days/<date>.json`.

One call per day, not per item, for the reason recorded in
`docs/notes/claude-cli-contract.md`: every call carries ~27k tokens of fixed
overhead, so batching is the difference between one call and thirty.

**Only published topics are classified.** Items the summarizer left out as off
topic are off topic for the book too.

**A classification failure never fails the run.** Same rule as `publish.py`: by
that point the digest is written and emailed, and being unable to reach the model
is worth reporting, not worth discarding a good run over. The day is simply left
unclassified, and `book.py sync` picks it up later.

## Commands

`book.py` sits at the repository root beside `run_daily.py`, `export_static.py`
and `serve.py`.

| Command | Effect |
|---|---|
| `book.py bootstrap` | Derive the first outline from the whole archive |
| `book.py sync` | Classify every day that has no day file |
| `book.py sync --refile` | Also re-file days stamped with an older version |
| `book.py outline` | Re-derive from the archive, print a proposed diff, write nothing |
| `book.py outline --accept` | Apply the proposal and bump the version |

### Bootstrap

1,364 topics do not fit in one prompt, so bootstrap runs in two passes: candidate
threads per week, then one consolidation call that merges candidates into the
final thread list and groups them into parts. Roughly eight to ten calls. This is
the one genuinely expensive operation and it runs once.

### Revision

`book.py outline` proposes and never applies. The diff names each change and what
motivated it — new threads are shown with the unfiled items that argue for them,
merges with the overlap that suggests them. `--accept` applies it, bumps
`version`, and leaves existing day files stamped with the old one.

Machine-proposed, human-approved, matching how `config/sources.json` and the
skipped list already work here.

## Review page

A new view in `web/`, marked `data-live-only` so `export_static.py` strips it
from the published build exactly as it strips the journal and the skip controls.
The mechanism already exists and is already tested; this adds no new way to leak.

`/api/book` in `src/serve.py` returns the outline plus a per-thread rollup: item
count, date range, contributing handles, and the items themselves. The page shows
parts and their threads, per-thread provenance so single-source threads are
obvious at a glance, and the unfiled queue.

Read-only in this first cut. Structural change goes through `book.py outline`,
where a diff can be reviewed before it lands.

## Errors

| Condition | Behaviour |
|---|---|
| No `book/` yet | Commands say to run `bootstrap`; nothing is auto-created |
| `claude -p` fails | Same retry and parse rules as `summarize.py` |
| Malformed `outline.json` | Raise, never silently rebuild — as `sources.py` does |
| Day already classified | `sync` skips it unless `--refile` |

## Testing

`tests/test_chapters.py`, following `tests/CLAUDE.md`: fake runner, `tmp_path`,
no network. Covers a clean classification, unfiled items, version stamping, a
malformed outline, and that a model failure is non-fatal to the run.

`tests/test_serve.py` gains coverage for `/api/book`.
`tests/test_export_static.py` gains a case asserting the book view is stripped
from the published build — the one failure here that would be a privacy leak.

There is no JS harness, so the page itself is verified by running `serve.py` and
looking at it.

## Out of scope

Drafting chapter prose. Editing the outline from the UI. Exporting the book
anywhere. Cleaning the tag vocabulary — real (`defence` vs `defense`, `[politics`
parsing artifacts) but not on this path.

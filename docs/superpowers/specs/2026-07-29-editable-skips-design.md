# Editing what the filter left out

**Date:** 2026-07-29
**Status:** approved

## The problem

`[interests]` decides what counts as news, and every topic it drops is reported
behind the `skipped (n)` chip. The judgement is sometimes wrong in both
directions: a story worth reading gets dropped, and something not worth reading
gets kept. Today the only remedy is editing `config.toml` and re-running the day,
which re-summarizes everything to fix one topic.

## The constraint that shapes the design

A skipped topic **has no body anywhere on disk.** The prompt asks for
`{"headline": str, "reason": str}` for skipped entries, `summarize_day` flattens
each to a `"headline: reason"` string, and `RunRecord.skipped` stores the strings
in `logs/runs.json`. The digest holds only kept topics.

So "restore it to the feed" has nothing to restore. Making the toggle possible at
all means asking the model for full content on skipped topics too, and keeping
them in the digest.

## Storage: a per-topic `skipped:` meta line

```markdown
## Merch drop announcement
tags: media
sources: [@somehandle](https://www.instagram.com/p/AAA/)
skipped: promotional, reports no news

Body text, stored even though it is filtered out of the feed.
```

Absent or empty means kept. Skipped topics are rendered after the kept ones.

Chosen over a separate `## Skipped` region for two reasons. `digest._topic`
already consumes a run of meta lines through `_META_LINE`, so parsing is one word
in a regex and one branch. More importantly, **toggling becomes inserting or
deleting a single line inside one section** rather than moving blocks of text
around a file that also contains the journal. A bug in a `news/` writer destroys
the only thing in this project that cannot be regenerated, so the smallest
possible edit is the correct shape.

Frontmatter `tags:` and `sources:` keep being built from kept topics only, so the
topic chips continue to mean "topics in the feed".

## Two records, two questions

The `skipped (n)` chip reads `logs/runs.json` today. That has to move, because
the run log is **history**: it records what the 11am run decided. Editing it to
reflect a hand-restore would make it lie about what happened.

After this change:

- **the digest is current state** — what is skipped now, including hand edits.
  The chip, `/api/day`, `/api/days`, and the export read this.
- **the run log is unchanged** — what that run decided. The Runs panel keeps
  reading it.

Both stay true, and they will legitimately disagree once a topic is restored by
hand. That disagreement is information, not drift.

## Shape change to the API

`skipped` becomes a list of `{"headline": str, "reason": str}` rather than
`"headline: reason"` strings. This also removes the `': '` split in
`showSkipped`, which was guessing at a boundary inside a string the model wrote.

## `src/topics.py`

A second mutator of `news/`, alongside `notes.py`. `notes.py`'s claim to be the
only one is updated rather than quietly falsified.

Contract, mirroring `notes.py` because the stakes are the same:

- Operates only within one `## <headline>` section, and only before the notes
  heading.
- Refuses unless the headline matches **exactly one** section. Zero or several is
  an error, never a guess — restoring the wrong topic is a silent wrong edit.
- Refuses a reason containing a newline or a notes marker, for the reason
  `write_notes` refuses forged markers: it would let a value forge a structural
  boundary.
- Rebuilds as prefix + edited section + suffix, with prefix and suffix copied
  verbatim. The notes block is never inside the edited span.

Errors map to HTTP 409, like `NotesMarkerError`.

## The front-end wrinkle

The feed is one `innerHTML` assignment from rendered markdown, so there are no
per-topic DOM handles. After setting it, walk `article.querySelectorAll('h2')`
and insert a control per heading, keyed by a `data-headline` attribute stamped on
each heading before anything is added around it.

**The control must not go inside the heading.** The first attempt appended the
button to the `h2`, which looked right and read fine — but the accessibility tree
showed the heading had stopped being a heading, reported as bare text instead.
Headings are the primary means of navigating a page without sight, and the feed is
nothing but headings and paragraphs. The button is therefore a sibling, with the
pair wrapped in a `.topic-head` flex row that restores the shared line. Two CSS
rules keyed on `h2` being a direct child of `.article` needed the wrapper spelling
added alongside them, since the static build has no wrapper.

Each button's accessible name includes its headline ("Skip My trip to Moab"),
because a page of identically-named `skip` buttons is not navigable either.

Live mode only. The static build never renders the controls and the endpoints do
not exist there.

Skipping prompts for a reason, prefilled `marked by hand`: the point of the
skipped list is that nothing is dropped without a visible reason, and a
hand-skip with no reason would be the one silent drop in the system. Restoring
does not prompt. Both reload the day and the day index so the chip count is right.

## Search

A restored topic is an ordinary feed topic — searchable, and its tags appear as
chips. Skipped topics stay out of `search` and `all_tags`, matching today's
behaviour, where they are not in the file at all.

## Existing days

The eight days already on disk have skips only in the run log, with no bodies.
They are re-summarized offline after this ships:

```bash
for d in 2026-07-22 … 2026-07-29; do
  .venv/bin/python run_daily.py --date $d --no-fetch --quiet
done
```

No Instagram traffic — `--no-fetch` works from the transcripts and sidecars on
disk. `run_daily` reads the existing journal before writing and carries it into
the new file, so notes survive. The bodies will be reworded and topics may
regroup, which is accepted.

## Verification

`pytest` covers `render`, `digest`, `topics`, `serve`, and `export_static`. The
one that matters most is `test_topics.py`: a toggle must leave the journal and
every other section byte-identical.

The front end has no test harness, so it was verified in a browser against a
scratch archive served by the real `serve.py` — never against `news/`, which the
verification would have written to. Confirmed:

- a hand-skip prompts, leaves the feed, and appears in the chip with its reason
- cancelling the prompt changes nothing
- restoring brings the topic back with its body, tags, and source links
- the restored topic becomes searchable and its tag returns to the chips, while a
  still-skipped copy of the same headline on another day does not
- the journal survives skip → restore → skip, and the file on disk stays
  well-formed with one `skipped:` line per section
- a day whose every topic was dropped says so instead of rendering blank
- the published build lists the skips with reasons, offers no controls, and
  contains neither the journal nor any skipped topic's body

### One more day of work than expected

The eight existing days were re-summarized as planned. `news/` was copied to
`data/news-backup-2026-07-29/` first — it is git-ignored, so there is no version
control to fall back on, and the journal inside those files is unrecoverable if
the notes carry-over were to fail.

# Browsing a growing archive

**Date:** 2026-07-29
**Status:** approved

## The problem

`renderDays()` flattens every day in the archive into one list. At eight days
that reads well. At three hundred it is a scroll well inside a `40vh` box, and
the only way to reach February is to drag.

Two separate needs are hiding in that one list:

- **Browsing** — "what is in this archive, and roughly when?"
- **Reading through** — "I finished this day, give me the one before it."

A single flat list serves the first badly and the second not at all.

## What is being built

### Month sections

The Days panel groups its days under collapsible month headings, newest month
first and open:

```
▾ July 2026
    Jul 29    138 !
    Jul 28      9
▸ June 2026        (30)
▸ May 2026         (31)
```

A collapsed month shows how many days it holds, so the shape of the archive
stays visible without expanding anything.

**Expansion state lives in `state.openMonths`, not in the DOM.** `renderDays()`
runs on every `showDay()`, so a `<details open>` attribute set by the user would
be thrown away on the next click. The Set is seeded with the newest month, and
the month containing `state.activeDate` is always added to it — which is what
makes clicking a March search hit open March rather than landing on a day the
sidebar does not show.

`max-height: 40vh; overflow-y: auto` stays on the container. One expanded month
can still be thirty-one rows, so the cap still does work; it just stops being
the only thing between the reader and a three-hundred-row list.

### Older / Newer, under the article

```
← Older              Newer →
Jul 28                    —
```

`state.days` is newest-first (`digest.list_days` sorts `reverse=True`), so for
the active day at index `i`, `days[i + 1]` is older and `days[i - 1]` is newer.
Getting that backwards is the one easy mistake here, and it is silent.

Each side is a button labelled with its date, or an inert dash at the ends of
the archive — a dash rather than nothing, so reaching the oldest day does not
shift the layout.

Both sides call `showDay()`, which already runs `confirmDiscardNotes()`. The
unsaved-notes prompt therefore covers this new way of leaving a day without any
new code, and that is the reason to route through `showDay()` rather than
duplicating its work.

The nav sits **between the article and the journal**: it closes the reading, and
the notes box stays the last thing on the page.

## What is deliberately not being built

- A jump-to-date input. Month sections cover browsing; a second control
  overlaps.
- Arrow-key shortcuts.
- Pagination of the days list itself. The month grouping already bounds its
  height.

## Scope

`web/index.html`, `web/app.js`, `web/style.css`. Nothing in `src/`, and no
change to `export_static.py`: it copies `app.js` and `style.css` verbatim, and
the static build populates `state.days` from `data/days.json` with the same
shape the live API returns. Both features work in the published site for free.

## Verification

There is no JS test harness in this project — every test is Python, and the
export tests assert against synthetic `index.html` fixtures rather than the real
markup. So `pytest` will not cover this, and saying otherwise would be a lie
about what was checked.

It is verified by running `serve.py` and confirming:

- the newest month is open on load and older months are collapsed with counts
- expanding June, then clicking a July day, leaves June expanded
- a search hit from a collapsed month opens that month when clicked
- Older on the oldest day and Newer on the newest render dashes, not buttons
- Older/Newer with unsaved notes still prompts
- the same behaviour after `export_static.py`, opened from `site/`

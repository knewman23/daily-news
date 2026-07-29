# web/ — the front end

Three files, no build step, no CDN, no framework: `index.html`, `app.js`,
`style.css`. Vanilla ES modules-free script, loaded with a plain `<script>` tag.
Keep it that way — the whole point is that the published site is static files a
browser can open with nothing installed.

## `web/` is the source; `site/` is the output

`export_static.py` copies these files verbatim into `site/` and rewrites
`index.html`. It deletes and rebuilds `site/` wholesale, so **editing `site/` by
hand loses the change silently.** After any edit here:

```bash
.venv/bin/python export_static.py
```

## Two modes, one codebase

`<body data-mode>` decides, and `app.js` reads it once into `READ_ONLY`:

- **live** — talks to the local Python server. Journal, sources, runs all work.
- **static** — the GitHub Pages build. Reads pre-generated JSON from `data/`.

The mode comes from the markup, not the hostname: hostname sniffing breaks the
moment the local server is reached by anything other than `127.0.0.1`.

The `source` object in `app.js` is the single place that decides where data comes
from. Add new endpoints there rather than branching on `READ_ONLY` at each fetch.

**Live-only markup carries `data-live-only`.** `export_static.py` strips those
elements out of the static build, and `start()` removes them from the DOM. They
are *removed*, never hidden with CSS — a `display: none` add-a-handle form is
still a working form to anyone who opens devtools.

## innerHTML has exactly one permitted use

The server renders each day's markdown to HTML, and `showDay()` assigns it to
`article.innerHTML`. That content is generated locally by this project from the
user's own files.

**Everything that comes from outside goes in as `textContent`** — handles,
headlines, log lines, error messages, skip reasons. The `el()` helper takes
`textContent` in its props for this reason; reach for `innerHTML` and you have
almost certainly made a mistake.

## The palette is measured, not chosen

The `:root` custom properties were sampled from `header.PNG` with k-means, with
the percentage of the image each colour covers noted in the comments.
Substituting eyeballed values will not sit right against the artwork. Use the
existing variables; do not add hex literals.

Deliberately a single light treatment — no dark mode. The artwork is painted on
cream stock and a dark inversion fights it.

## Layout

One breakpoint, `46rem`, and it is duplicated in both files by necessity:
`style.css` draws the two layouts, and `app.js` reads it via
`matchMedia('(max-width: 46rem)')` because the drawer is toggled with the
`hidden` attribute. Leaving `hidden` set at desktop width would hide the rail
from assistive technology even though CSS draws it. **Change one, change both.**

## Re-render, do not patch

`renderDays()`, `renderTags()`, `loadSources()`, and `loadRuns()` each rebuild
their subtree with `replaceChildren()`. There is no diffing and no reconciliation.

The consequence to remember: **any state you want to survive a re-render must
live in `state`, not in the DOM.** `state.openMonths` exists for exactly this
reason — a `<details open>` attribute set by the user would be discarded on the
next `showDay()`.

## Dates

`prettyDate()` builds a `Date` from the parts, never from the ISO string:
`new Date('2026-07-28')` parses as UTC midnight and renders as the 27th in any
negative-offset timezone. `DISPLAY_TZ` pins the "last updated" stamp to the clock
the pipeline runs on, so reading the site from another timezone cannot show an
update time that disagrees with the day headings.

`state.days` is **newest-first** (`digest.list_days` sorts `reverse=True`). So at
index `i`, `days[i + 1]` is *older* and `days[i - 1]` is *newer*. Getting this
backwards is silent.

# web/ — the front end

No build step, no CDN, no framework: `index.html`, `app.js`, `style.css`,
`theme.js`, `eagle.png`, and `fonts/`. Vanilla ES modules-free scripts, loaded
with plain `<script>` tags. Keep it that way — the whole point is that the
published site is static files a browser can open with nothing installed. The
four woff2 faces are self-hosted for the same reason: a Google Fonts link is a
third-party dependency and a render-blocking round trip.

## `web/` is the source; `site/` is the output

`export_static.py` copies these files verbatim into `site/` and rewrites
`index.html`. Flat files are listed in its `ASSETS`; `fonts/` is copied whole
via `ASSET_DIRS`, because the faces are referenced from `@font-face` in the
stylesheet rather than from the markup, so there is no URL there to fingerprint.
**A new file in `web/` is not published until it is named in one of those two.**

It deletes and rebuilds `site/` wholesale, so **editing `site/` by hand loses
the change silently.** After any edit here:

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

## The palette is shared, not local

The `:root` tokens are lifted verbatim from `knewman23.github.io/styles.css`,
which `ai-frontier/site/style.css` also copies. All three sites are deliberately
one system, so a visitor moving between them sees one page: same tokens, same
dark band, same breadcrumbs, same theme toggle, same two typefaces.

**So the tokens are not ours to retune.** Changing a value here silently forks
the system. If a colour genuinely needs to change, change it in the portfolio
and copy it down to the other two. Use the variables; do not add hex literals.

The one local addition is the `--alert` family, for the things this app has and
a portfolio index does not: a failed run, a stale handle, an error in a log.

Dark is a token swap, never a fork — `--card` is still "the surface above
`--bg`" in both themes, so no component carries a dark branch. There are exactly
two exceptions, both deliberate:

- **The band stays dark in both themes**, as it does on the other two sites. It
  is the constant that makes the three feel like one place.
- **`.crest` is inverted rather than swapped.** `eagle.png` is transparent-backed
  black line art, so it takes the page's own background in light mode, and
  `filter: invert(1)` turns the strokes white for dark. One asset, and a browser
  that drops the filter still shows the drawing.

**This site defaults to light, and the other two follow the OS.** That is the
one place the three diverge. `style.css` therefore carries *no*
`prefers-color-scheme` block at all, and `theme.js` never consults the media
query — dark is reachable only through the button, and is then remembered in
`localStorage`. The stylesheet and the script have to agree about this; if you
reintroduce the media query in one, reintroduce it in both or the button will
disagree with the page.

The pre-paint boot script in `<head>` is what stops a flash of the wrong palette
when navigating in from the portfolio. It must stay inline and stay before the
stylesheet.

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

## The feed is one innerHTML blob

`showDay` assigns the whole rendered day at once, so there are no per-topic
elements to attach behaviour to. `indexHeadings` therefore stamps each `h2` with
`data-headline` *before* anything is added around it, and everything downstream
identifies a topic by that attribute rather than by `textContent`.

**Do not put controls inside a generated heading.** A `<button>` inside an `<h2>`
drops the heading out of the accessibility tree, and headings are how the page is
navigated without sight. `addSkipControls` wraps the heading and its button in a
`.topic-head` flex row instead — same visual result, semantics intact. This was
observed in the a11y tree, not guessed.

## Dates

`prettyDate()` builds a `Date` from the parts, never from the ISO string:
`new Date('2026-07-28')` parses as UTC midnight and renders as the 27th in any
negative-offset timezone. `DISPLAY_TZ` pins the "last updated" stamp to the clock
the pipeline runs on, so reading the site from another timezone cannot show an
update time that disagrees with the day headings.

`state.days` is **newest-first** (`digest.list_days` sorts `reverse=True`). So at
index `i`, `days[i + 1]` is *older* and `days[i - 1]` is *newer*. Getting this
backwards is silent.

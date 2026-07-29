# Instagram Daily News Digest — Design

**Date:** 2026-07-28
**Status:** Approved

## Purpose

Produce one dated markdown file per day summarizing the spoken content of daily
video posts from a curated list of Instagram accounts, and a local web app for
reading past days, searching across them, and journaling personal reactions.

Instagram is the only source. No other news feeds, no RSS, no APIs beyond
Instagram itself.

## Success criteria

1. A single unattended run at 11am produces `news/YYYY-MM-DD.md` containing
   one section per distinct news topic, with sources attributed.
2. A story covered by several accounts on the same day appears once.
3. `python serve.py` opens a page listing every past date, renders the selected
   day, filters by search text and topic tag, and saves journal notes back into
   that day's markdown file without altering the generated summary.
4. The same page manages the source list: add a handle, remove it, or disable it
   without editing a file. The next 11am run honors the change.
5. Re-running after a partial failure completes the day without redoing
   already-finished work.

## Non-goals

- Any news source other than Instagram.
- Stories.
- Multi-run-per-day polling. One run, 11am.
- Remote hosting, authentication, or multi-user support. Localhost only.
- Cross-day de-duplication. Each day stands alone (see Decisions).

## Decisions

| Area | Choice | Rationale |
|---|---|---|
| Instagram access | `instaloader` with the user's own session cookie | No public read API exists for third-party accounts. Accepted tradeoff: violates Instagram ToS; mitigated by a single daily run. |
| Transcription | `faster-whisper`, local, `small` model default | Free, offline, no audio leaves the machine. ~5–15s per 60s reel on Apple Silicon. |
| Summarization | `claude -p` headless CLI | Uses the existing Claude Code subscription. No API key, no per-token cost, best available model quality. |
| Language | Python 3.13 throughout | `instaloader` and `faster-whisper` are Python; stdlib `http.server` and `markdown` cover the web app. No npm, no build step. |
| Scheduler | launchd `StartCalendarInterval` | On a laptop that sleeps through 11am, launchd fires on wake. cron silently skips the run. |
| De-duplication | Same-day across accounts only | Each day is a complete standalone picture. Cross-day suppression risks dropping real developments in ongoing stories. |
| Filtering | Full-text search + Claude-assigned topic tags | Tags live in frontmatter; search needs no vocabulary maintenance. Both are cheap given the summary is already structured. |
| Journaling | Local server writes into the same day's `.md` | One file per day holds news and reactions together. Marker comments isolate the writable region. |
| Source list | `config/sources.json`, server-writable, managed from the web UI | Keeps machine-written state out of the hand-edited `config.toml`. A malformed write can't break whisper settings or the port. |
| Removing a source | Disable by default, hard delete available | Disabling keeps the handle's past contributions attributable without pulling new posts. Delete is a separate, explicit action. |
| Image posts | Included, with the on-image text read by OCR | Some followed accounts (`oafnation_actual`) post news as text-on-image, frequently with an empty caption — verified on a live post whose entire content existed only in the image. Excluding them drops that account's output almost entirely. |
| OCR engine | Apple Vision via `pyobjc`, local | Built into macOS: no model download, no API key, no per-image cost. Measured 0.1–0.4s per image with accurate results on real posts. Tesseract would need a Homebrew dependency and reads stylised headline text less reliably. |
| Fetch window | Per-handle `last_pull_at` watermark, not a fixed lookback | Everything posted since that handle's last successful pull is collected, so a missed or failed run is made up on the next one instead of silently losing posts. Per-handle rather than global means one rate-limited account doesn't cost the others their window. Capped by `max_lookback_days` so a long-stale watermark can't trigger a full profile crawl. |

## Architecture

```
daily-news/
├── config.toml              whisper model, port, paths — hand-edited
├── config/sources.json      handle list — written by the web UI
├── run_daily.py             orchestrator; entry point for launchd
├── src/
│   ├── sources.py           load/add/remove/toggle handles
│   ├── fetch.py             instaloader → data/raw/<date>/
│   ├── transcribe.py        ffmpeg + faster-whisper → data/transcripts/<date>/
│   ├── summarize.py         transcripts → claude -p → news/<date>.md
│   ├── notes.py             read/write the notes marker block
│   ├── digest.py            frontmatter parse, day index, search index
│   └── serve.py             stdlib HTTP server + JSON endpoints
├── web/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── news/                    YYYY-MM-DD.md — the deliverable
├── data/
│   ├── raw/<date>/          downloaded mp4 + caption.json
│   └── transcripts/<date>/  .txt per post
├── tests/
└── docs/superpowers/specs/
```

### Units and their boundaries

Each module has one job, a narrow interface, and is testable without network.

**`sources.py`** — `load() -> list[Source]`, `add(handle) -> Source`,
`set_enabled(handle, bool) -> None`, `remove(handle) -> None`
Owns `config/sources.json` and is the only writer of it. `add` normalizes the
handle (strips `@`, lowercases, rejects URLs and whitespace), rejects duplicates,
then performs one instaloader profile lookup to confirm the account exists and is
reachable; on failure the handle is not added and the reason is returned. Writes
atomically via temp file plus rename so an interrupted save cannot truncate the
list. Depends on: filesystem, instaloader (lookup only).

**`fetch.py`** — `fetch_day(date, cfg) -> Stats`
Downloads video feed posts and reels for each enabled handle, using that handle's
own `last_pull_at` watermark as the cutoff rather than a fixed window. Writes
`data/raw/<date>/<handle>_<shortcode>.mp4` and a sibling
`<handle>_<shortcode>.json` holding caption, timestamp, and permalink.
Skips any post whose mp4 already exists. Advances the handle's watermark only
after that handle's fetch succeeds. Depends on: instaloader, filesystem.

**`ocr.py`** — `ocr_day(raw_dir, out_dir, cfg) -> (list[Transcript], Stats)`
For each downloaded image without an extracted-text file: run Apple Vision text
recognition and write `data/transcripts/<date>/<handle>_<shortcode>.txt`. Returns
the same `Transcript` record `transcribe.py` produces, tagged `kind="image"`, so
everything downstream treats spoken and on-image text identically. Carousels OCR
each slide and concatenate. Depends on: pyobjc Vision, filesystem.

**`transcribe.py`** — `transcribe_day(date) -> list[Transcript]`
For each mp4 without a matching transcript: extract 16kHz mono wav via ffmpeg,
run faster-whisper, write `data/transcripts/<date>/<handle>_<shortcode>.txt`.
Videos with no audio track or an empty transcription are skipped and logged.
Depends on: ffmpeg binary, faster-whisper, filesystem. No knowledge of Instagram.

**`summarize.py`** — `summarize_day(date, transcripts) -> str`
Assembles one prompt containing every transcript with its handle and caption,
invokes `claude -p`, and receives JSON: a list of topics, each with headline,
body, tags, and contributing sources. Renders that JSON to the markdown format
below. Depends on: `claude` on PATH. Pure function given the subprocess result,
so the renderer is unit-testable against fixture JSON.

**`notes.py`** — `read_notes(path) -> str`, `write_notes(path, text) -> None`
The only code permitted to mutate a file in `news/`. Rewrites strictly between
`<!-- notes:start -->` and `<!-- notes:end -->`. If markers are missing or
malformed, raises rather than guessing. Depends on: filesystem only.

**`digest.py`** — `list_days() -> list[DayMeta]`, `render_day(date) -> Html`
Parses frontmatter, builds the date index and the search corpus, converts
markdown to HTML via the `markdown` package. Read-only with respect to `news/`.

**`serve.py`** — binds `127.0.0.1:8420`. Serves `web/` statically plus:

| Endpoint | Purpose |
|---|---|
| `GET /api/days` | Date index with tags and post counts |
| `GET /api/day/<date>` | Rendered HTML, frontmatter, current notes |
| `GET /api/search?q=` | Matching topic sections across all days |
| `POST /api/notes/<date>` | Save notes; delegates to `notes.py` |
| `GET /api/sources` | Handle list with enabled state and last-seen date |
| `POST /api/sources` | Add a handle; 400 with a reason if invalid or unreachable |
| `PATCH /api/sources/<handle>` | Enable or disable |
| `DELETE /api/sources/<handle>` | Hard delete from the list |

**`run_daily.py`** — calls fetch → transcribe → summarize in order, each stage
skipping completed work. Reads enabled handles from `sources.py`. Logs to
`logs/YYYY-MM-DD.log`. Non-zero exit and a macOS notification on failure.

### Web UI layout

- **Left rail** — every past date, newest first
- **Main pane** — the selected day's summary, markdown rendered server-side via
  the `markdown` package. No CDN, no JS dependencies.
- **Top bar** — search box that live-filters topic sections across all days,
  plus clickable tag chips
- **Below each day** — textarea for journal notes, saving to
  `POST /api/notes/<date>`
- **Sources panel** — collapsible; lists each handle with an enable/disable
  toggle and a delete button, plus an input to add a new one. Add shows the
  rejection reason inline when the lookup fails.

## Data flow

```
config/sources.json  (enabled handles)
   │
   ▼  instaloader (authenticated session)
data/raw/2026-07-28/*.mp4 + *.json
   │
   ▼  ffmpeg → wav → faster-whisper
data/transcripts/2026-07-28/*.txt
   │
   ▼  one claude -p call: cluster, dedupe, tag, write
news/2026-07-28.md
   │
   ▼  digest.py (read) / notes.py (write notes block only)
localhost:8420
```

## Source list format

`config/sources.json`, seeded with the initial five handles:

```json
{
  "version": 1,
  "sources": [
    {"handle": "total.hipocrisy",      "enabled": true, "added": "2026-07-28", "last_seen": null},
    {"handle": "aaronparnas",          "enabled": true, "added": "2026-07-28", "last_seen": null},
    {"handle": "cancel.ian.carroll",   "enabled": true, "added": "2026-07-28", "last_seen": null},
    {"handle": "carolinegleich",       "enabled": true, "added": "2026-07-28", "last_seen": null},
    {"handle": "hunteralexanderhowell","enabled": true, "added": "2026-07-28", "last_seen": null}
  ]
}
```

`last_seen` is stamped by `fetch.py` with the date a post was last pulled from
that handle, so the Sources panel makes a silently dead account obvious.

## Output format

```markdown
---
date: 2026-07-28
generated: 2026-07-28T16:31:02-07:00
tags: [politics, markets, tech]
sources: ["@handle1", "@handle2", "@handle3"]
post_count: 12
transcribed_count: 11
incomplete: false
---

# July 28, 2026

## Senate passes the spending bill
tags: politics
sources: @handle1, @handle3

Two to four sentences in news format. A story covered by several
accounts collapses into this single section.

## Nvidia earnings beat estimates
tags: markets, tech
sources: @handle2

...

## My Notes
<!-- notes:start -->
<!-- notes:end -->
```

`incomplete: true` is set when any handle failed to fetch or any video failed to
transcribe, so a thin day is never mistaken for a quiet news day.

## Error handling

| Condition | Behavior |
|---|---|
| Instagram session expired | Log the re-login command, fire a macOS notification, exit non-zero. No silent empty day. |
| Rate limited (HTTP 429) | Exponential backoff up to 3 attempts per handle, then abandon that handle. Continue other handles. Mark `incomplete: true`. |
| Handle not found / private | Log, skip, mark `incomplete: true`. |
| Video has no audio track | Skip, log, count toward `post_count` but not `transcribed_count`. |
| Transcription empty or below a word-count floor | Skip that post, log. |
| ffmpeg missing | Fail fast at startup with install instructions. |
| `claude -p` non-zero or unparseable JSON | Retry once, then fail. Transcripts remain on disk, so a manual re-run costs seconds. |
| Zero posts found across all handles | Write the day's file with an explicit "no posts found" body rather than no file at all. |
| Notes markers missing on save | Raise; return 409 to the browser. Never append blindly. |
| Adding a handle that doesn't exist or is private | Not added. 400 with the reason shown inline in the Sources panel. |
| Adding a duplicate handle | 409, no change. Normalization means `@Foo` and `foo` collide. |
| `sources.json` missing or corrupt | Fail fast with the path and the parse error. Never silently fall back to an empty list, which would produce a wrongly-empty day. |
| All handles disabled | `run_daily.py` exits 0 with a logged notice and writes no file. |
| Adding a handle mid-run | The run uses the list snapshot taken at start. New handles take effect the next day. |

**Idempotency:** every stage is keyed on output existence. Running `run_daily.py`
repeatedly on the same date converges without duplicate downloads,
re-transcription, or duplicated topic sections.

## Testing

pytest, no network, no model downloads in CI.

Priority order:

1. **`notes.py` round-trip** — the critical test. Given a realistic day file,
   writing notes must leave every byte outside the marker block identical.
   Includes: empty notes, notes containing `---`, notes containing the marker
   text itself, missing markers, duplicated markers.
2. **Markdown renderer** — fixture `claude -p` JSON → expected markdown, byte
   for byte. Covers zero topics, one topic, multi-source topics.
3. **Frontmatter and day index** — malformed frontmatter, missing fields,
   out-of-order dates.
4. **`sources.py`** — normalization (`@Foo`, `foo`, a profile URL, trailing
   whitespace all resolve to `foo`), duplicate rejection, enable/disable
   round-trip, atomic write leaves no partial file on simulated crash, corrupt
   JSON raises rather than returning `[]`. Profile lookup mocked.
5. **Stage skip logic** — with outputs pre-created, assert no download and no
   transcription is attempted.
6. **Integration** — three canned transcripts, `claude -p` mocked to return
   fixed JSON, assert the resulting file parses and renders.

Manual verification: one real run against the live handle list, then open the
page and save a note.

## Setup requirements

- `brew install ffmpeg`
- `pip install instaloader faster-whisper markdown`
- `instaloader --login <username>` once, to create the session file
- First transcription downloads the whisper `small` model (~500MB, one time)
- `launchctl load ~/Library/LaunchAgents/com.krys.daily-news.plist`

## Open items

None. The initial handle list is captured above; further sources are added from
the web UI.

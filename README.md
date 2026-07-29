# Daily News

A daily news digest built from the Instagram accounts you follow.

Every morning at 11:00 it pulls the day's posts from a curated handle list,
transcribes the audio from videos and reads the text off image posts, collapses
the same story covered by several accounts into one section, and writes a dated
markdown file. Then it publishes a read-only copy of the archive and emails you
the headlines.

**Read it:** <https://knewman23.github.io/daily-news/>

Instagram is the only source. No RSS, no other feeds.

---

## How it works

```
config/sources.json          the handles being watched
        │
        ▼  instaloader, using a browser session cookie
data/raw/<date>/             *.mp4, *.jpg, and a *.json caption sidecar per post
        │
        ├──▼ ffmpeg → faster-whisper          (video → spoken text)
        └──▼ Apple Vision OCR                 (image → on-screen text)
data/transcripts/<date>/     one *.txt per post
        │
        ▼  one `claude -p` call: cluster, de-duplicate, tag, attribute
news/<date>.md               the digest, plus your journal notes
        │
        ├──▼ serve.py         localhost, full read/write
        └──▼ export_static.py site/ → pushed → GitHub Pages, read-only
```

Every stage is keyed on whether its output already exists, so re-running a day
redoes only what is missing. A failure at the summarize step costs seconds to
retry rather than a fresh round of downloads and transcription.

### Two things that are true by design

**Your journal notes never leave the machine.** They live inside
`news/<date>.md`, `news/` is git-ignored, and the export renders only the news
body. The published site and the repository contain neither.

**A day always gets a file.** Even a day with nothing in it is written, saying
so. Otherwise a silently broken pipeline looks exactly like a quiet news day.

---

## Commands

Every command uses `.venv/bin/python`, not a bare `python` — the dependencies
live in the virtualenv and a bare interpreter will not find them.

```bash
cd ~/Projects/daily-news

# Read the archive: the local app, with the journal, sources, and run history
.venv/bin/python serve.py                       # → http://127.0.0.1:8420
.venv/bin/python serve.py --port 9000           # if 8420 is taken

# Run the pipeline
.venv/bin/python run_daily.py                   # today
.venv/bin/python run_daily.py --date 2026-07-20  # a specific day, or a re-run
.venv/bin/python run_daily.py --full            # re-scan the whole day, ignoring watermarks
.venv/bin/python run_daily.py --date 2026-07-22 --no-fetch --quiet   # summarize a backfilled day

# Backfill past days: one profile walk per handle, paced
.venv/bin/python backfill.py --days 7            # plan only
.venv/bin/python backfill.py --days 7 --execute  # download

# Rebuild the published site without running the pipeline
.venv/bin/python export_static.py

# Tests
.venv/bin/pytest
.venv/bin/pytest tests/test_notes.py -v

# The schedule
launchctl list | grep daily-news                          # is it registered?
launchctl kickstart -p gui/$UID/com.krys.daily-news       # run it now
launchctl bootout gui/$UID ~/Library/LaunchAgents/com.krys.daily-news.plist

# Logs
tail -f logs/$(date +%F).log      # the run in progress
tail -f logs/launchd.err.log      # scheduled-run failures
```

Open the app and the browser in one go:

```bash
cd ~/Projects/daily-news && .venv/bin/python serve.py & sleep 1 && open http://127.0.0.1:8420
```

---

## Daily use

```bash
.venv/bin/python serve.py          # then open http://127.0.0.1:8420
```

The local app has everything: the archive, search, topic filters, the journal,
the handle list, and the run history. The hosted copy is read-only — no journal,
no source list, no runs.

| | |
|---|---|
| Search | Filters topic sections across every day |
| Topic chips | Filter by tag; the active one shows in the toolbar |
| `skipped (n)` chip | Lists what the interest filter left out, with reasons. Dashed, because it is a view rather than a topic |
| My notes | Saved into that day's markdown, inside marker comments |
| Sources | Add, disable, or delete a handle. Adding verifies the account exists |
| Runs | Every run with its counts, duration, failures, and full log |

On a phone those panels move behind the **⋮** button; on a desktop they are a
permanent rail.

### Running it by hand

```bash
.venv/bin/python run_daily.py                      # today
.venv/bin/python run_daily.py --date 2026-07-20     # a specific day, or a re-run
.venv/bin/python run_daily.py --full               # full pull: ignore watermarks
.venv/bin/python export_static.py                  # rebuild site/ only
```

---

## Using this for your own accounts

This is built for one person's machine, but nothing in it is specific to that
person. To run your own:

**What you need.** macOS (the OCR uses the Vision framework built into the OS, and
the scheduler is launchd), Python 3.11+, Homebrew for ffmpeg, an Instagram account
that follows the handles you want, and Claude Code installed and logged in — the
summarizer shells out to `claude -p`, so it uses your existing subscription rather
than an API key.

**Steps.** Fork or clone, then work through *One-time setup* below and change these
five things:

| Where | Change |
|---|---|
| `config/sources.json` | Replace the handles with yours. Or leave it empty and add them from the web UI, which verifies each account exists before saving. |
| `config.toml` → `[fetch] session_user` | Your Instagram username. |
| `config.toml` → `[email]` | Your address, or `enabled = false`. |
| `config.toml` → `[publish]` | `enabled = false` unless you want a public site. |
| `launchd/*.plist` | The paths are absolute and contain a username. Rewrite all four, and pick your own `Label`. |

**A word on the tradeoffs**, since they are choices rather than facts:

- **instaloader with a browser cookie** is against Instagram's terms of service.
  It is a single request per handle per day, which is why it has been fine here,
  but the risk is yours and it is your account. There is no official API for
  reading accounts you do not own.
- **`claude -p` costs no API key but does draw on your subscription usage.** One
  call per day; the base system prompt is ~27k tokens regardless of prompt size,
  which is why the whole day is summarized in a single call. Swap
  `DEFAULT_MODEL` in `src/summarize.py` for a cheaper model if you prefer.
- **Everything is local.** No database, no cloud service, no API keys beyond the
  ones you already have. The cost of that is that it only runs when your laptop
  is awake — launchd catches up on wake, but a machine left shut for a week will
  produce one catch-up digest, not seven.

**Porting off macOS** would mean replacing two things: `src/ocr.py` (Vision →
Tesseract or PaddleOCR) and the launchd plist (→ a systemd timer or cron). The
rest is portable, and both are behind narrow interfaces with injected
dependencies, so their tests do not need rewriting.

---

## One-time setup

### 1. Dependencies

```bash
brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The first transcription downloads the Whisper `small` model (~500 MB, once).
OCR uses the Vision framework built into macOS — no model, no API key.

### 2. Instagram session

Instagram blocks instaloader's login endpoint, so authentication is a browser
cookie import. Log in to instagram.com in Chrome first, then:

```bash
.venv/bin/instaloader --load-cookies chrome \
  --sessionfile ~/.config/instaloader/session-<your-username>
```

macOS will ask for Keychain access ("Chrome Safe Storage") — that is how Chrome's
cookie encryption key is read. Set `session_user` in `config.toml` to the account
the cookie belongs to. Use `brave` instead of `chrome` if that is where you browse.

### 3. Email password

The app password lives in the login Keychain, never in `config.toml` — that file
is committed. Create a Gmail **app password** (not your account password;
`myaccount.google.com/apppasswords`), then:

```bash
security add-generic-password -s daily-news-smtp -a <your-address> -w
```

Paste it at the prompt. Google shows it as four groups of four; the spaces do not
matter, they are stripped when it is read.

### 4. The schedule

```bash
cp launchd/com.krys.daily-news.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.krys.daily-news.plist
launchctl list | grep daily-news          # should print the label
```

launchd rather than cron: on a laptop that is asleep at 11:00, launchd fires the
job on wake and cron simply misses the window.

Force a run to prove the scheduled environment works — this is worth doing once,
because it catches missing-`PATH` failures that never appear in a terminal:

```bash
launchctl kickstart -p gui/$UID/com.krys.daily-news
tail -f logs/launchd.err.log
```

To change the time, edit the plist's `StartCalendarInterval`, then `bootout` and
`bootstrap` again. To stop it:

```bash
launchctl bootout gui/$UID ~/Library/LaunchAgents/com.krys.daily-news.plist
```

### 5. Publishing (optional)

Set `[publish] enabled = true` and the run will export `site/` and push it after
each successful day. A workflow deploys it to GitHub Pages.

Pages' branch setting can only serve the repository root or `/docs`, and `/docs`
holds the design documents, so the deploy runs from
`.github/workflows/pages.yml`. Set **Settings → Pages → Source** to
"GitHub Actions" once. Pages on a private repository needs a paid plan.

---

## When something goes wrong

Open the **Runs** panel. Every run records its counts, its duration, the specific
failures, and links to the full log. `incomplete: true` in a digest's frontmatter
means the day is missing posts — a thin day and a broken day look identical
without it.

| Symptom | Cause and fix |
|---|---|
| `Instagram session is not usable` | The cookie expired. Repeat step 2. |
| A handle shows `?` in Sources | Watched four-plus days with no post ever found — usually a typo or a renamed account. Verify the handle. |
| A day is marked incomplete | One handle failed, or a video would not transcribe. The Runs panel names which. That handle keeps its old watermark and retries tomorrow. |
| No email arrived | Check the Keychain item exists: `security find-generic-password -s daily-news-smtp -a <address> -w`. A send failure is logged but never fails the run. |
| `Daily news published locally but not pushed` | The digest is written and readable; only the push failed. Re-run, or push by hand. |
| Nothing ran at 11:00 | `launchctl list \| grep daily-news`. Then `logs/launchd.err.log` — a missing binary on `PATH` is the usual cause. |

### What counts as news

Not everything a followed account posts is news you want. A creator's personal
video, a merch plug, or a reaction clip that reports nothing new all arrive in the
same feed as the reporting.

`[interests]` in `config.toml` states what to keep and what to leave out, in plain
language rather than keywords — relevance is judged by what a story is *about*, so
"the war in Iran" also catches a report that never says "Iran", and a passing
political aside in a personal vlog is still a personal vlog.

Filtering happens inside the single daily summarize call, not as a second pass per
post: it is a judgement about text the model is already reading.

**Nothing is dropped silently.** Every topic left out on relevance grounds is
reported with a one-line reason — behind the `skipped (n)` chip on the page (on
the published site too), in the log, in the run record, and in the email. A filter you cannot see is indistinguishable from
one that is throwing away news. Edit the lists and re-run if it is too aggressive;
skipped topics never mark a day incomplete, because filtering is the feature
working rather than a failure.

### Disk usage

One day of posts is roughly **270 MB**, almost all of it mp4 — about 98 GB a year
if nothing is cleaned up. Compressing it does not help: mp4 and jpg are already
compressed, and `gzip -9` on a real reel came back at **99.3%** of the original.
So old media is deleted rather than archived.

After each successful run, media older than `[retain] media_days` (default 3) is
removed. What survives is what has lasting value and costs nothing:

| Kept forever | Size |
|---|---|
| `data/transcripts/<date>/*.txt` — the extracted text | ~150 KB/day |
| `data/transcripts/<date>/*.none` — "nothing usable here", with the reason | a few bytes |
| `data/raw/<date>/*.json` — the caption sidecars | ~1 KB/post |
| `news/<date>.md` — the digest | ~15 KB/day |

The `.none` markers matter more than their size suggests. A silent video or an
unreadable graphic will never produce a transcript, so without a record that
extraction *concluded*, the post looks unfinished forever: whisper retries it on
every run, and its media is never released because an un-extracted post's media is
the only copy of its content. Found in practice — five such posts were pinning
media and being re-transcribed daily.

Those three are enough to re-summarize any past day **offline**, which is why the
extract stages find their posts from the sidecars rather than by looking for media.

Three rules make it safe to run unattended: a day is only pruned once its digest
exists; a post is only pruned once its transcript exists; and today is never
pruned whatever the setting says. Set `media_days` very high to keep everything.

### Backfilling past days

`run_daily --date X` walks every profile to build a single day, so backfilling a
month that way would be 210 profile walks across seven handles for what 7 can
collect. **The request pattern is what gets an account flagged**, not the volume
of video pulled from the CDN, so use `backfill.py`, which walks each profile once
and files every post into the day it belongs to.

```bash
.venv/bin/python backfill.py --days 7                 # plan only, downloads nothing
.venv/bin/python backfill.py --days 7 --execute        # download, paced
.venv/bin/python backfill.py --days 30 --handles aaronparnas --max-posts 400
```

A plan always runs first and prints posts per day, estimated media size, and
estimated transcription time, so the cost is known before it is paid.

Then summarize each collected day, which touches no network at all:

```bash
.venv/bin/python run_daily.py --date 2026-07-22 --no-fetch --quiet
```

`--no-fetch` works entirely from the media and sidecars on disk. `--quiet`
suppresses publishing, email, and notifications, so thirty days of backfill do not
send thirty emails — publish once at the end with `export_static.py` or a normal
run.

**On not getting your account limited.** Measured on one handle over 30 days: 250
posts listed in a single walk taking 63 seconds, instaloader pacing itself. Seven
handles over 30 days is roughly 175 API requests plus ~1,100 CDN downloads.

The safeguards, in order of how much they matter:

- A rate-limit response **aborts the entire run and is never retried**. Answering
  a request for less with more is how a slowdown becomes a suspension.
- Progress is on disk after every post, so an aborted run resumes instead of
  starting over. Re-running is cheap and safe.
- instaloader's own rate controller stays on, and `--delay-handles` (90s default)
  and `--delay-posts` (2s) add more on top.
- `--max-posts` caps each handle; a scan ceiling stops a runaway walk.

Prefer several small runs to one large one, and start with a plan.

### Full pulls

A normal run starts from each handle's watermark, so re-running an hour later
fetches nothing — the watermark has already passed those posts. `--full` ignores
the watermarks and re-scans from local midnight of the target day:

```bash
.venv/bin/python run_daily.py --full
```

Use it after a partial run, or when you suspect an earlier run missed something.
It is cheap: posts already on disk are not downloaded again and transcripts are
reused, so the cost is one profile walk per handle. It is still clamped by
`max_lookback_days`, so `--full` on an old date cannot crawl a whole profile.

### The fetch window

Each handle carries its own `last_pull_at` watermark, and everything posted since
its last **successful** pull is collected. A missed or failed run is made up on
the next one rather than lost, and a rate-limited account retries the same window
tomorrow while the others move on. A run that is a day late pulls the backlog
into *that day's* file.

---

## Configuration

`config.toml` holds hand-edited settings. `config/sources.json` holds the handle
list and is written by the web UI — keeping machine-written state out of the
hand-edited file means a bad write cannot corrupt the model choice or the port.

| Section | Worth knowing |
|---|---|
| `[fetch]` | `first_run_lookback_hours` applies only to a handle with no watermark. `max_lookback_days` caps how far a stale watermark can reach back. |
| `[transcribe]` | `model`: `tiny` … `large-v3`. `min_words` is the floor below which a transcript is discarded — it counts the caption too, since a headline graphic OCRs to very little while its caption carries the story. |
| `[retain]` | `media_days`: how long to keep downloaded video and images. See *Disk usage*. |
| `[interests]` | `include` / `exclude`: what counts as news worth keeping. See *What counts as news*. |
| `[publish]` | `enabled`, and which remote and branch to push. |
| `[email]` | Addresses and the Keychain lookup keys. Never the password. |

---

## Tests

```bash
.venv/bin/pytest
```

No network, no model downloads, no API calls — Instagram, Whisper, Vision,
`claude -p`, git, and SMTP are all injected at their boundaries.

The most important test is the notes round-trip in `tests/test_notes.py`: it
proves that saving a journal entry cannot damage the generated summary, which is
the one failure in this project that would destroy something unrecoverable.

---

## Layout

```
run_daily.py         the 11am run: fetch → transcribe → ocr → summarize → publish → email
serve.py             the local web app
export_static.py     builds the read-only site/

src/fetch.py         instaloader, per-handle watermarks
src/transcribe.py    ffmpeg + faster-whisper
src/ocr.py           Apple Vision, for text on images
src/summarize.py     the one `claude -p` call per day
src/render.py        topics → markdown (pure, so the format is pinned by tests)
src/digest.py        reading digests back: index, topics, search, HTML
src/notes.py         the journal block — the only code that mutates a digest
src/publish.py       export and push
src/mailer.py        the summary email
src/runlog.py        run history
src/sources.py       the handle list
src/posts.py         the day's posts, as recorded by the caption sidecars
src/prune.py         deletes transcribed media to reclaim disk
src/serve.py         the HTTP layer

docs/superpowers/    the design spec and implementation plan
```

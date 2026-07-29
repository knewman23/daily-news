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
| My notes | Saved into that day's markdown, inside marker comments |
| Sources | Add, disable, or delete a handle. Adding verifies the account exists |
| Runs | Every run with its counts, duration, failures, and full log |

On a phone those panels move behind the **⋮** button; on a desktop they are a
permanent rail.

### Running it by hand

```bash
.venv/bin/python run_daily.py                      # today
.venv/bin/python run_daily.py --date 2026-07-20     # a specific day, or a re-run
.venv/bin/python export_static.py                  # rebuild site/ only
```

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
src/serve.py         the HTTP layer

docs/superpowers/    the design spec and implementation plan
```

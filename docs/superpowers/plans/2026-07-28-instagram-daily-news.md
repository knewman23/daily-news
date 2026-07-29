# Instagram Daily News Digest Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers-trainual:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An 11am daily job that transcribes audio from a curated set of Instagram accounts and writes a de-duped, topic-sectioned markdown digest, plus a localhost web app for reading past days, searching, journaling, and managing the source list.

**Architecture:** Four resumable pipeline stages (fetch → transcribe → summarize) driven by `run_daily.py` under launchd, each keyed on output existence so re-runs redo only missing work. A separate stdlib HTTP server reads the generated markdown and writes only two things: journal notes (inside comment markers) and `config/sources.json`. Pure logic — normalization, frontmatter parsing, markdown rendering, notes rewriting — lives in modules with no network or subprocess dependencies, so the majority of the system is unit-testable offline.

**Tech Stack:** Python 3.13, `instaloader`, `faster-whisper`, `ffmpeg`, `markdown`, `pyyaml`, `claude -p` CLI, stdlib `http.server` + `tomllib`, pytest, launchd.

**Spec:** `docs/superpowers/specs/2026-07-28-instagram-daily-news-design.md`

---

## Chunk 1: Offline core

Everything in this chunk is pure logic with no network, no subprocess, and no model downloads. It is the majority of the risk surface and all of it is testable in CI.

### Task 1: Project scaffold

**Files:**
- Create: `requirements.txt`, `config.toml`, `pytest.ini`, `src/__init__.py`, `tests/__init__.py`, `.gitignore` (modify)
- Create: `config/sources.json`

- [ ] **Step 1: Write `requirements.txt`**
```
instaloader>=4.14
faster-whisper>=1.1
markdown>=3.7
pyyaml>=6.0
pytest>=8.0
```

- [ ] **Step 2: Write `config.toml`**
Hand-edited settings only — no handle list here (that lives in `config/sources.json`).
```toml
[paths]
news = "news"
raw = "data/raw"
transcripts = "data/transcripts"
logs = "logs"
sources = "config/sources.json"

[fetch]
session_user = ""            # your Instagram username; set before first run
first_run_lookback_hours = 48   # window used only when a handle has no watermark yet
max_lookback_days = 14          # ceiling on the walk-back, so a stale watermark
                                # can't trigger a crawl of a whole profile history
max_retries = 3
backoff_seconds = 30

[transcribe]
model = "small"            # tiny | base | small | medium | large-v3
compute_type = "int8"
min_words = 10             # transcripts below this are treated as empty

[serve]
host = "127.0.0.1"
port = 8420
```

- [ ] **Step 3: Write `config/sources.json` with the seed handles**
Exact content — both null fields must be present, not omitted. `last_pull_at` is
the per-handle fetch watermark (ISO 8601 UTC); `last_seen` is the date a post was
last actually found, which is what makes a silently dead account visible in the UI.
```json
{
  "version": 2,
  "sources": [
    {"handle": "total.hipocrisy", "enabled": true, "added": "2026-07-28", "last_pull_at": null, "last_seen": null},
    {"handle": "aaronparnas", "enabled": true, "added": "2026-07-28", "last_pull_at": null, "last_seen": null},
    {"handle": "cancel.ian.carroll", "enabled": true, "added": "2026-07-28", "last_pull_at": null, "last_seen": null},
    {"handle": "carolinegleich", "enabled": true, "added": "2026-07-28", "last_pull_at": null, "last_seen": null},
    {"handle": "hunteralexanderhowell", "enabled": true, "added": "2026-07-28", "last_pull_at": null, "last_seen": null},
    {"handle": "oafnation_actual", "enabled": true, "added": "2026-07-28", "last_pull_at": null, "last_seen": null}
  ]
}
```

- [ ] **Step 4: Write `pytest.ini`**
Set `testpaths = tests` and add the repo root to the import path so `from src import ...` resolves.

- [ ] **Step 5: Create a virtualenv and install**
Run: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
Expected: all five packages install without error.

- [ ] **Step 6: Verify pytest collects**
Run: `.venv/bin/pytest`
Expected: `no tests ran` — exit code 5, not an import error.

- [ ] **Step 7: Commit**
```bash
git add requirements.txt config.toml config/sources.json pytest.ini src/__init__.py tests/__init__.py .gitignore
git commit -m "chore: project scaffold, config, and seed source list"
```

---

### Task 2: `sources.py` — the handle list

**Files:**
- Create: `src/sources.py`
- Test: `tests/test_sources.py`

Interface: `load(path) -> list[Source]`, `enabled_sources(path) -> list[Source]`, `add(path, handle, lookup=...) -> Source`, `set_enabled(path, handle, flag) -> None`, `remove(path, handle) -> None`, `advance_watermark(path, handle, when) -> None`, `stamp_last_seen(path, handle, date) -> None`, `normalize(raw) -> str`.

`enabled_sources` returns whole records rather than bare handles because `fetch.py`
needs each handle's `last_pull_at` watermark to compute its cutoff.

The `lookup` parameter is an injected callable so tests never touch the network. Production passes an instaloader-backed lookup; `add` calls it once and refuses the handle if it raises.

- [ ] **Step 1: Write failing tests for `normalize`**
Assert all of these produce `"foo.bar"`: `"foo.bar"`, `"@foo.bar"`, `"  @Foo.Bar  "`, `"https://instagram.com/foo.bar"`, `"https://www.instagram.com/foo.bar/"`, `"instagram.com/foo.bar?hl=en"`.
Assert `ValueError` for: `""`, `"@"`, `"two words"`, `"foo/bar"`, `"a" * 40` (over Instagram's 30-char limit).
Dots and underscores must survive — the seed list contains `total.hipocrisy` and `cancel.ian.carroll`.

- [ ] **Step 2: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_sources.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.sources'`

- [ ] **Step 3: Implement `normalize`**
Strip whitespace, strip a scheme/host prefix and any query string, strip leading `@` and trailing `/`, lowercase, then validate against `^[a-z0-9._]{1,30}$`.

- [ ] **Step 4: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_sources.py`
Expected: PASS

- [ ] **Step 5: Write failing tests for load, add, toggle, remove**
Use a `tmp_path` fixture holding a copy of the seed JSON.
- `load` returns 6 sources; `enabled_sources` returns all 6
- `add` with a stub lookup that succeeds appends the handle, `enabled=True`, `added` set to the injected date, `last_pull_at=None`, `last_seen=None`
- `add` with a stub lookup that raises leaves the file byte-identical and surfaces the reason
- `add` of `"@Aaronparnas"` raises a duplicate error — normalization collides with the existing entry
- `set_enabled(handle, False)` then `enabled_sources` omits it but `load` still includes it
- `remove` drops it from `load` entirely
- `remove` / `set_enabled` on an unknown handle raises
- `stamp_last_seen` and `advance_watermark` each update only that handle's field, leaving the other five untouched
- `advance_watermark` never moves a watermark backwards — passing an earlier timestamp than the stored one is a no-op, so an out-of-order or clock-skewed run can't re-open a window that was already closed

- [ ] **Step 6: Write failing tests for the file-safety cases**
- Corrupt JSON (`"{not json"`) → `load` raises with the path in the message. It must NOT return `[]`; an empty list would silently produce an empty digest that looks like a quiet news day.
- Missing file → raises, same reasoning.
- Valid JSON with `sources` missing or not a list → raises.
- Atomic write: after any mutation, assert no `*.tmp` file remains in the directory and the file parses.

- [ ] **Step 7: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_sources.py`
Expected: FAIL on the load/add/toggle/remove and safety cases

- [ ] **Step 8: Implement the rest of `sources.py`**
All mutations follow read → modify → atomic write. The atomic write is the one fragile operation here, so use exactly this shape:
```python
def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)   # atomic on the same filesystem
```
`os.replace` is what makes an interrupted save leave the old file intact rather than a truncated one.

- [ ] **Step 9: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_sources.py`
Expected: PASS

- [ ] **Step 10: Commit**
```bash
git add src/sources.py tests/test_sources.py
git commit -m "feat: source list management with normalization and atomic writes"
```

---

### Task 3: `notes.py` — the journal block

**Files:**
- Create: `src/notes.py`
- Test: `tests/test_notes.py`

Interface: `read_notes(path) -> str`, `write_notes(path, text) -> None`.

This is the highest-risk module in the project: it is the only code that mutates a generated digest, and a bug silently destroys news the user cannot recover. The round-trip test is the most important test in the suite.

- [ ] **Step 1: Write the failing round-trip test**
Build a realistic fixture day file (frontmatter, two topic sections, `## My Notes` with empty markers). Write notes, then assert **every byte outside the marker block is unchanged** — compare the full file text split on the markers, not just a substring check.

- [ ] **Step 2: Write the failing edge-case tests**
- Empty string clears the block back to nothing
- Notes containing `---` do not corrupt frontmatter parsing on re-read
- Notes containing the literal text `<!-- notes:start -->` are rejected, or escaped such that a second round-trip is still stable — pick one and assert it
- Notes containing `## Heading` do not create a phantom topic section (assert `digest.py` topic extraction still finds exactly 2 topics after the write — add this assertion in Task 5 once the parser exists, leave a TODO marker here)
- Multi-line notes with trailing whitespace and a trailing newline
- Writing twice in a row is idempotent for identical input
- Missing markers → raises
- Duplicated markers (two `notes:start`) → raises
- Inverted markers (`end` before `start`) → raises
- `read_notes` on a file with empty markers returns `""`

- [ ] **Step 3: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_notes.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.notes'`

- [ ] **Step 4: Implement `notes.py`**
Locate markers by exact string match, assert exactly one of each and correct order, then rebuild the file as `prefix + start_marker + "\n" + text.strip() + "\n" + end_marker + suffix`. Raise a distinct exception type for malformed markers so `serve.py` can map it to HTTP 409. Write via the same `os.replace` atomic pattern as `sources.py` — extract that helper into `src/atomic.py` and have both modules use it rather than duplicating it.

- [ ] **Step 5: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_notes.py`
Expected: PASS

- [ ] **Step 6: Commit**
```bash
git add src/notes.py src/atomic.py tests/test_notes.py
git commit -m "feat: journal notes read/write isolated to marker block"
```

---

### Task 4: `render.py` — digest JSON to markdown

**Files:**
- Create: `src/render.py`
- Test: `tests/test_render.py`

Split out from `summarize.py` deliberately: rendering is a pure function and gets full test coverage, while `summarize.py` keeps only the subprocess call. Interface: `render_day(date, topics, stats) -> str`.

- [ ] **Step 1: Write failing tests**
Given fixture topic JSON, assert the output matches the spec format byte for byte:
- frontmatter with `date`, `generated`, `tags` (union of all topic tags, deduped, stable order), `sources` (union, `@`-prefixed), `post_count`, `transcribed_count`, `incomplete`
- `# July 28, 2026` human-readable title
- one `## <headline>` per topic, each followed by `tags:` and `sources:` lines and the body
- a trailing `## My Notes` section with empty `<!-- notes:start -->` / `<!-- notes:end -->` markers
- Zero topics → still a valid file with a "No posts found" body and intact notes markers
- A topic with three sources lists all three
- `incomplete: true` when the stats say so
- Output round-trips: feeding it to `notes.write_notes` then `digest` parsing works

- [ ] **Step 2: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_render.py`
Expected: FAIL — module missing

- [ ] **Step 3: Implement `render.py`**
Pure string assembly. Take `generated` as an injected parameter, not `datetime.now()`, so tests are deterministic.

- [ ] **Step 4: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_render.py`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add src/render.py tests/test_render.py
git commit -m "feat: render digest topics to dated markdown"
```

---

### Task 5: `digest.py` — reading days back

**Files:**
- Create: `src/digest.py`
- Test: `tests/test_digest.py`
- Modify: `tests/test_notes.py` (resolve the TODO from Task 3)

Interface: `list_days(news_dir) -> list[DayMeta]`, `parse_day(path) -> Day`, `topics_of(path) -> list[Topic]`, `render_html(path) -> str`, `search(news_dir, query) -> list[Hit]`.

- [ ] **Step 1: Write failing tests for frontmatter and indexing**
- `list_days` returns newest first
- Filenames that aren't `YYYY-MM-DD.md` are ignored, not crashed on
- Missing frontmatter fields fall back to safe defaults (empty tags, zero counts) rather than raising — unlike `sources.json`, a malformed old digest should degrade, not block the whole page
- Absent frontmatter entirely → still listable with the date from the filename

- [ ] **Step 2: Write failing tests for topic extraction and search**
- `topics_of` splits on `## ` and **excludes** `## My Notes`
- A note containing `## Something` does not become a topic — this is the assertion deferred from Task 3
- `search` is case-insensitive, matches topic body and headline, returns date + headline + tags
- `search` with an empty query returns nothing rather than everything
- Tag filtering matches on the topic's own tags, not the day's union

- [ ] **Step 3: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_digest.py`
Expected: FAIL — module missing

- [ ] **Step 4: Implement `digest.py`**
Split frontmatter on the leading `---` fence and parse with `yaml.safe_load`. Render with `markdown.markdown(body, extensions=["extra", "sane_lists"])`. Build the search corpus per request — a few dozen files is fast enough that a cache would be premature.

- [ ] **Step 5: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_digest.py tests/test_notes.py`
Expected: PASS

- [ ] **Step 6: Commit**
```bash
git add src/digest.py tests/test_digest.py tests/test_notes.py
git commit -m "feat: parse, index, search, and render stored digests"
```

---

## Chunk 2: Adapters, server, and scheduling

This chunk touches the network, subprocesses, and the ML model. Tests here mock at the boundary.

### Task 6: Verify the `claude -p` contract

**Files:**
- Create: `docs/notes/claude-cli-contract.md`

Do this before writing `summarize.py`. The CLI's exact output shape must be observed, not assumed — guessing here means writing a parser against a contract that doesn't exist.

- [ ] **Step 1: Probe the CLI**
Run: `claude -p 'Reply with only this JSON: {"ok": true}' --output-format json`
Record the literal stdout, including whether the payload is wrapped (e.g. in a `result` field) and whether the inner JSON arrives fenced in a ```` ```json ```` block.

- [ ] **Step 2: Probe stdin and exit codes**
Run: `echo 'Reply with only: hello' | claude -p --output-format json; echo "exit=$?"`
Confirm whether prompts can be piped (they can be large — a day of transcripts will exceed comfortable argv limits) and what a failure exit code looks like.

- [ ] **Step 3: Write down the observed contract**
Document the exact command, how to reach the JSON payload, and the failure signature. `summarize.py` is written against this file, not against assumption.

- [ ] **Step 4: Commit**
```bash
git add docs/notes/claude-cli-contract.md
git commit -m "docs: record observed claude -p output contract"
```

---

### Task 7: `summarize.py` — cluster, dedupe, tag

**Files:**
- Create: `src/summarize.py`
- Test: `tests/test_summarize.py`

Interface: `build_prompt(transcripts) -> str`, `call_claude(prompt, runner=subprocess.run) -> list[Topic]`, `summarize_day(date, transcripts, stats, runner=...) -> Path`.

- [ ] **Step 1: Write failing tests for `build_prompt`**
Assert the prompt includes every transcript with its handle, caption, and permalink; that handles appear verbatim so the model can attribute sources; and that it states the same-day-only de-dup rule and the required JSON schema. Assert an empty transcript list still produces a valid prompt.

- [ ] **Step 2: Write failing tests for `call_claude`**
Inject a fake `runner`:
- returns well-formed JSON → parsed topics
- returns JSON wrapped in a ```` ```json ```` fence → still parsed (the CLI does this intermittently; tolerate it)
- returns prose with no JSON → retried once, then raises
- non-zero exit → retried once, then raises
- returns valid JSON of the wrong shape (missing `headline`) → raises with a clear message
- assert the retry happens exactly once, not unbounded

- [ ] **Step 3: Write the integration test**
Three canned transcripts where two cover the same story. `runner` mocked to return fixed JSON collapsing them into one topic with two sources. Assert `summarize_day` writes `news/<date>.md`, that `digest.parse_day` reads it back, that `topics_of` finds the expected count, and that `notes.write_notes` then succeeds on it.

- [ ] **Step 4: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_summarize.py`
Expected: FAIL — module missing

- [ ] **Step 5: Implement `summarize.py`**
Prompt instructs: group transcripts into distinct news topics; collapse the same story across accounts into one topic listing every contributing handle; write each in neutral news style, 2–4 sentences; assign 1–3 lowercase topic tags from a stable vocabulary; return only JSON. Use the invocation from Task 6's contract doc, pass the prompt on stdin, and delegate markdown to `render.py`.

- [ ] **Step 6: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_summarize.py`
Expected: PASS

- [ ] **Step 7: Commit**
```bash
git add src/summarize.py tests/test_summarize.py
git commit -m "feat: summarize and dedupe transcripts into topic sections"
```

---

### Task 8: `transcribe.py` — audio to text

**Files:**
- Create: `src/transcribe.py`
- Test: `tests/test_transcribe.py`

Interface: `has_audio(mp4) -> bool`, `extract_wav(mp4, wav) -> None`, `transcribe_file(wav, model) -> str`, `transcribe_day(date, cfg, model_factory=...) -> Stats`.

- [ ] **Step 1: Write failing tests with ffmpeg and the model mocked**
- A `.mp4` whose `.txt` already exists is skipped — assert neither ffmpeg nor the model is invoked. This is the resumability guarantee.
- `has_audio` false → skipped, counted in `post_count` but not `transcribed_count`
- Transcription returning fewer than `min_words` words → treated as empty, skipped, logged
- One file raising does not abort the remaining files
- `model_factory` is called at most once per day-run, not once per file — model load is the expensive part

- [ ] **Step 2: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_transcribe.py`
Expected: FAIL — module missing

- [ ] **Step 3: Implement audio handling**
Exact commands — these flags are not obvious and wrong ones produce silent garbage or a model that rejects the input:
```bash
# audio stream present?  empty stdout means no audio track
ffprobe -v error -select_streams a:0 -show_entries stream=codec_type -of csv=p=0 IN.mp4

# 16 kHz mono PCM wav — what whisper expects
ffmpeg -nostdin -v error -y -i IN.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le OUT.wav
```
`-nostdin` matters: without it ffmpeg can consume the parent process's stdin and hang an unattended launchd run.

- [ ] **Step 4: Implement the transcription loop**
```python
model = WhisperModel(cfg.model, device="cpu", compute_type=cfg.compute_type)
segments, _info = model.transcribe(str(wav), vad_filter=True)
text = " ".join(s.text.strip() for s in segments).strip()
```
`faster-whisper` runs on CPU via CTranslate2 — there is no Metal/MPS backend, so `device="cpu"` with `int8` is correct on Apple Silicon, not a fallback. `vad_filter=True` suppresses the hallucinated filler whisper emits over music-only stretches, which reels have a lot of. Load the model once per run; delete the intermediate wav after each file.

- [ ] **Step 5: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_transcribe.py`
Expected: PASS

- [ ] **Step 6: Smoke-test against real audio**
Record or download any short talking video to `/tmp/probe.mp4`, then run the module against it. Expected: plausible transcript text, and the `small` model downloads on first use (~500MB, one time).

- [ ] **Step 7: Commit**
```bash
git add src/transcribe.py tests/test_transcribe.py
git commit -m "feat: extract audio and transcribe with local faster-whisper"
```

---

### Task 9: `fetch.py` — pulling posts

**Files:**
- Create: `src/fetch.py`
- Test: `tests/test_fetch.py`

Interface: `make_loader(cfg) -> Instaloader`, `lookup_profile(loader, handle) -> None` (the callable `sources.add` injects), `cutoff_for(source, now, cfg) -> datetime`, `fetch_handle(loader, source, cutoff, dest) -> list[PostRef]`, `fetch_day(date, cfg, loader=..., now=...) -> Stats`.

**Watermark semantics.** Each handle carries its own `last_pull_at`. The cutoff for
a handle is that watermark — so everything posted since its last successful pull is
collected, and a missed run is made up on the next one rather than lost. The
watermark advances **only after that handle's fetch succeeds**; a handle that was
rate-limited keeps its old watermark and retries the same window tomorrow, while
the handles that succeeded move forward independently.

- [ ] **Step 1: Write failing tests for `cutoff_for`**
- `last_pull_at` present → cutoff is exactly that timestamp
- `last_pull_at` null (new or freshly added handle) → cutoff is `now - first_run_lookback_hours`
- `last_pull_at` older than `max_lookback_days` → cutoff is clamped to `now - max_lookback_days`, not the stale watermark
- Naive vs timezone-aware timestamps do not silently compare wrong — assert everything is UTC-aware

- [ ] **Step 2: Write failing tests with instaloader faked**
Fake profile objects exposing `is_video`, `date_utc`, `shortcode`, `caption`, `video_url`.
- Only `is_video` posts are downloaded
- Iteration stops at the first post older than `cutoff` rather than walking the whole profile history
- A post posted exactly at the cutoff timestamp is excluded, not fetched twice — the boundary is `>` cutoff, and this is what prevents the last post of the previous run reappearing today
- Two consecutive runs with no new posts in between download nothing the second time
- A post whose `.mp4` already exists is not re-downloaded
- A caption sidecar `.json` is written with caption, UTC timestamp, and permalink
- `advance_watermark` is called with `now` after a successful handle fetch, including when that handle yielded zero posts
- `stamp_last_seen` is called only for a handle that yielded posts

- [ ] **Step 3: Write failing tests for the failure paths**
- Login/session error → raises a distinct `SessionExpired` so `run_daily` can emit the re-login instruction
- 429 / `TooManyRequests` → retried with backoff up to `max_retries`, then that handle is abandoned and `incomplete` is set; assert sleep is injected, not real, so the test is fast
- **A failed handle's watermark is not advanced** — assert `last_pull_at` is byte-identical after the failure, and that the next run re-requests the same window. This is the whole point of per-handle watermarks; getting it wrong loses posts permanently and silently.
- Private or missing profile → skipped, logged, `incomplete` set, watermark untouched
- One handle failing does not prevent later handles from being fetched, and their watermarks still advance

- [ ] **Step 4: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_fetch.py`
Expected: FAIL — module missing

- [ ] **Step 5: Implement `fetch.py`**
Non-obvious instaloader configuration — the defaults download a pile of files this project doesn't want:
```python
L = instaloader.Instaloader(
    save_metadata=False,              # no .json.xz sidecars
    download_comments=False,
    download_geotags=False,
    download_video_thumbnails=False,
    post_metadata_txt_pattern="",     # no caption .txt files
    quiet=True,
)
L.load_session_from_file(cfg.session_user)   # raises if the session is gone
```
Iterate `Profile.from_username(L.context, handle).get_posts()` (newest first), break once `post.date_utc <= cutoff`, and download videos to `data/raw/<date>/<handle>_<shortcode>.mp4`. Cutoff comes from `cutoff_for`, never a fixed window. Advance the watermark to `now` only on the success path — put it after the loop, not in a `finally`.

- [ ] **Step 6: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_fetch.py`
Expected: PASS

- [ ] **Step 7: Authenticate for real, then smoke-test one handle**
Run: `.venv/bin/instaloader --login <your-username>` (interactive — the user runs this, including any 2FA prompt), set `session_user` in `config.toml`, then fetch a single handle.
Expected: one or more `.mp4` files in `data/raw/<today>/`, and that handle's `last_pull_at` set in `config/sources.json`.

- [ ] **Step 8: Commit**
```bash
git add src/fetch.py tests/test_fetch.py
git commit -m "feat: fetch recent video posts per handle via instaloader"
```

---

### Task 9b: `ocr.py` — text on images

**Files:**
- Create: `src/ocr.py`
- Test: `tests/test_ocr.py`
- Modify: `src/fetch.py`, `tests/test_fetch.py` (download image posts too)
- Modify: `src/records.py` (add `Transcript.kind`)

Added after live inspection of `oafnation_actual`: it posts news as text-on-image,
sometimes with a completely empty caption, so the post's entire content exists
only in the image. Verified with Apple Vision — 0.1–0.4s per image, accurate on
real posts, no model download and no API key.

Interface: `extract_text(image, recognizer=...) -> str`,
`ocr_day(raw_dir, out_dir, cfg, recognizer=...) -> (list[Transcript], Stats)`.

- [ ] **Step 1: Add `kind` to `Transcript`**
Defaults to `"audio"`; OCR sets `"image"`. `summarize.build_prompt` labels each
block with it so the model knows whether it is reading speech or on-screen text —
they read very differently and conflating them produces odd summaries.

- [ ] **Step 2: Extend `fetch.py` to download image posts**
Non-video posts download to `<handle>_<shortcode>.jpg` with the same JSON sidecar.
Carousels (`GraphSidecar`) save each slide as `<handle>_<shortcode>_<n>.jpg`.
Update `test_fetch.py`: assert images are downloaded, that a carousel produces one
file per slide, and that the existing skip-if-present behavior covers images too.

- [ ] **Step 3: Write failing tests for `ocr.py` with the recognizer injected**
- An image whose `.txt` already exists is skipped, and the recognizer is not called
- Recognizer returning nothing → skipped, counted but not extracted
- Text below `min_words` → skipped (watermarks like "OAF NATION" are all that
  some images yield, and are not news)
- One image raising does not abort the rest; the day is flagged incomplete
- Carousel slides are concatenated into one transcript in slide order
- Returned records carry `kind="image"` and the sidecar's handle/caption

- [ ] **Step 4: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_ocr.py`
Expected: FAIL — module missing

- [ ] **Step 5: Implement `ocr.py`**
Exact Vision invocation — the pyobjc bridge is not obvious and a wrong
recognition level silently degrades quality:
```python
url = NSURL.fileURLWithPath_(str(path))
handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
request = Vision.VNRecognizeTextRequest.alloc().init()
request.setRecognitionLevel_(0)          # 0 = accurate, 1 = fast
request.setUsesLanguageCorrection_(True)
ok, err = handler.performRequests_error_([request], None)
lines = [str(o.topCandidates_(1)[0].string()) for o in (request.results() or [])
         if o.topCandidates_(1)]
```
Import Vision lazily, as `transcribe.py` does with whisper, so tests and the web
app never pay the framework import.

- [ ] **Step 6: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_ocr.py tests/test_fetch.py`
Expected: PASS

- [ ] **Step 7: Smoke-test against the real account**
Fetch recent `oafnation_actual` posts and OCR them. Expected: readable news text,
including from a post with an empty caption.

- [ ] **Step 8: Commit**
```bash
git add src/ocr.py tests/test_ocr.py src/fetch.py tests/test_fetch.py src/records.py
git commit -m "feat: read on-image text from image posts via Apple Vision"
```

---

### Task 10: `run_daily.py` — the orchestrator

**Files:**
- Create: `run_daily.py`
- Test: `tests/test_run_daily.py`

- [ ] **Step 1: Write failing tests with all three stages faked**
- Stages run in order fetch → transcribe → summarize
- The enabled-handle list is snapshotted once at start; mutating `sources.json` mid-run does not change what this run fetches
- All handles disabled → exit 0, notice logged, no file written
- Zero posts found → a digest file is still written with a "No posts found" body, so a silent failure is distinguishable from a quiet news day
- Any handle or transcript failure → `incomplete: true` in the written frontmatter
- `SessionExpired` → non-zero exit and the re-login command in the log
- Second invocation on the same date with all outputs present → no fetch, no transcribe, and the existing digest's notes block is preserved rather than overwritten

- [ ] **Step 2: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_run_daily.py`
Expected: FAIL — module missing

- [ ] **Step 3: Implement `run_daily.py`**
Accept an optional `--date` for backfill and re-runs. Log to `logs/YYYY-MM-DD.log` and stdout. On failure, fire a macOS notification:
```python
subprocess.run(["osascript", "-e",
    f'display notification "{msg}" with title "Daily News"'], check=False)
```
Before overwriting an existing digest, read its notes block and carry it into the new file — a re-run must never cost the user their journal entry.

- [ ] **Step 4: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_run_daily.py`
Expected: PASS

- [ ] **Step 5: Full end-to-end run**
Run: `.venv/bin/python run_daily.py`
Expected: `news/<today>.md` exists, has topic sections with source attributions, and ends with empty notes markers.

- [ ] **Step 6: Commit**
```bash
git add run_daily.py tests/test_run_daily.py
git commit -m "feat: daily orchestrator with resumable stages and notes preservation"
```

---

### Task 11: `serve.py` — the local API

**Files:**
- Create: `src/serve.py`, `serve.py` (thin launcher)
- Test: `tests/test_serve.py`

- [ ] **Step 1: Write failing tests against the handler**
Drive the server in-process on an ephemeral port with a `tmp_path` news dir.
- `GET /api/days` → newest-first index with tags and counts
- `GET /api/day/<date>` → rendered HTML, frontmatter, current notes
- `GET /api/day/<bad-date>` → 404, and a path-traversal attempt (`../../etc/passwd`, `..%2f..%2f`) → 400, never a file read outside `news/`
- `GET /api/search?q=` empty → empty result, not everything
- `POST /api/notes/<date>` → 200, notes persisted, summary bytes unchanged
- `POST /api/notes/<date>` on a file with broken markers → 409
- `GET/POST/PATCH/DELETE /api/sources` → happy paths, plus 400 on a failed lookup and 409 on a duplicate
- Server binds `127.0.0.1` only

- [ ] **Step 2: Run tests to verify they fail**
Run: `.venv/bin/pytest tests/test_serve.py`
Expected: FAIL — module missing

- [ ] **Step 3: Implement `serve.py`**
`ThreadingHTTPServer` with a `BaseHTTPRequestHandler` subclass routing the endpoints in the spec and serving `web/` statically. Validate every `<date>` against `^\d{4}-\d{2}-\d{2}$` and resolve paths under the news dir before reading — reject anything that escapes. Map the malformed-marker exception to 409 and the source-lookup failure to 400 with the reason in the body.

- [ ] **Step 4: Run tests to verify they pass**
Run: `.venv/bin/pytest tests/test_serve.py`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add src/serve.py serve.py tests/test_serve.py
git commit -m "feat: localhost API for days, search, notes, and sources"
```

---

### Task 12: Web UI

**Files:**
- Create: `web/index.html`, `web/app.js`, `web/style.css`
- Move: `header.PNG` → `web/header.PNG` (so the static route serves it; the repo
  root is not served)

No test task — this is verified by driving the real page. Vanilla JS, no build step, no CDN.

- [ ] **Step 0: Move the header image and define the palette**
`git mv header.PNG web/header.PNG`, then declare the sampled palette as CSS custom
properties on `:root`. Exact values — these were sampled from the artwork with
k-means, not estimated, and eyeballed substitutes will not sit right against it:
```css
:root {
  --parchment: #ECEEE2;      /* 35% of the image — page background */
  --parchment-aged: #DDDACB; /* 28% — header backdrop, rail, card edges */
  --ink: #191B3F;            /* outline ink — body text, nav bar */
  --navy: #10297C;           /* flag canton — links, active nav, chip borders */
  --red: #C51C22;            /* flag stripes — active tag, alerts */
  --gold: #DDA519;           /* banner and talons — rules, hover, focus */
  --slate: #59565C;          /* eagle wings — secondary text, metadata */
}
```
Single light treatment, no dark-mode pair: the artwork is painted on cream stock and
a dark inversion fights it. Keep gold for accents only — it fails contrast as body
text on parchment.

- [ ] **Step 1: Build the shell**
Header with `header.PNG` centered on `--parchment-aged` and a `--gold` rule beneath.
Ink-navy navigation bar under it holding the search box and tag chips. Left rail of
dates (newest first), main pane for the rendered day. Clicking a date loads it;
clicking a tag filters. Constrain the header image with `max-width` and
`height: auto` so it scales down on a narrow window rather than forcing the page to
scroll sideways.

- [ ] **Step 2: Wire search**
Debounced calls to `/api/search`, results grouped by date and showing the matching topic headline. Clicking a result opens that day scrolled to the topic.

- [ ] **Step 3: Wire the journal**
Textarea under the day's content, a save button, and an explicit saved/failed indicator. On 409, show that the file's markers are broken rather than failing silently.

- [ ] **Step 4: Build the Sources panel**
Collapsible. Each handle with an enable/disable toggle and a delete button (delete asks for confirmation — it is the one destructive action in the UI). Add-handle input showing the server's rejection reason inline on 400/409.

- [ ] **Step 5: Verify in the browser**
Run: `.venv/bin/python serve.py` then open `http://127.0.0.1:8420`
Check each of these works: switch days; search a word you know is present; save a note and reload to confirm it persisted; disable a handle and confirm `config/sources.json` changed; add a nonsense handle and confirm the inline error.

- [ ] **Step 6: Confirm the summary survived**
Run: `git diff news/`
Expected: changes confined to inside the notes markers. Nothing else in the digest moved.

- [ ] **Step 7: Commit**
```bash
git add web/
git commit -m "feat: local web app for reading, searching, journaling, and sources"
```

---

### Task 13: Schedule and document

**Files:**
- Create: `launchd/com.krys.daily-news.plist`, `README.md`

- [ ] **Step 1: Write the launchd plist**
`StartCalendarInterval` at 11:00 — launchd fires this on wake if the machine was asleep, which cron does not. Use absolute paths; launchd runs with a minimal environment and no shell profile.
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.krys.daily-news</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/krystofernewman/Projects/daily-news/.venv/bin/python</string>
    <string>/Users/krystofernewman/Projects/daily-news/run_daily.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/krystofernewman/Projects/daily-news</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/krystofernewman/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key>
  <string>/Users/krystofernewman/Projects/daily-news/logs/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/krystofernewman/Projects/daily-news/logs/launchd.err.log</string>
</dict>
</plist>
```
The `PATH` entry is required and both directories matter: `ffmpeg`/`ffprobe` are in `/opt/homebrew/bin`, but `claude` is in `~/.local/bin`. launchd starts with a minimal `PATH` and reads no shell profile, so omitting either one means the 11am run fails while a terminal run succeeds.

- [ ] **Step 2: Install and verify the schedule**
```bash
cp launchd/com.krys.daily-news.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.krys.daily-news.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.krys.daily-news.plist
launchctl list | grep daily-news
```
Expected: the label appears with exit status 0.

- [ ] **Step 3: Force a scheduled run to prove the launchd environment works**
Run: `launchctl start com.krys.daily-news`
Then check `logs/launchd.err.log` is empty and today's digest was written. This catches missing-PATH failures that never appear in a terminal run.

- [ ] **Step 4: Write `README.md`**
Cover: what it does, one-time setup (`brew install ffmpeg`, venv, `instaloader --login`, set `session_user`, load the plist), daily use (`python serve.py`), manual/backfill run (`python run_daily.py --date YYYY-MM-DD`), and the two failure modes worth knowing — session expiry (re-login) and rate limiting (`incomplete: true` in the frontmatter).

- [ ] **Step 5: Run the full suite**
Run: `.venv/bin/pytest`
Expected: all tests pass.

- [ ] **Step 6: Commit**
```bash
git add launchd/ README.md
git commit -m "feat: launchd schedule at 11:00 and setup documentation"
```

---

## Deferred

Out of scope, recorded so they aren't rediscovered as bugs:

- Stories
- Video slides inside a carousel — carousel *images* are OCR'd (Task 9b), but a
  video slide within a carousel is not transcribed
- Cross-day de-duplication — each day stands alone by decision
- Per-topic journal notes — one notes block per day
- Any non-Instagram source

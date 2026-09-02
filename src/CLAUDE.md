# src/ — pipeline modules

Each module owns one stage and is imported by `run_daily.py`, `serve.py`, or
`export_static.py`. Nothing in here imports those three back.

## The modules

| Module | Owns |
|---|---|
| `fetch.py` | the walk, per-handle watermarks, backend selection |
| `fetch_chrome.py` | the `chrome` backend: posts read from a real browser |
| `transcribe.py` | ffmpeg + faster-whisper (video → spoken text) |
| `ocr.py` | Apple Vision (image → on-screen text) |
| `summarize.py` | the one `claude -p` call per day: cluster, de-duplicate, tag, filter |
| `render.py` | topics → markdown |
| `digest.py` | reading digests back: index, topics, search, HTML |
| `notes.py` | the journal block |
| `topics.py` | flipping one topic's `skipped:` line |
| `publish.py` | export and push |
| `autopublish.py` | coalescing background publisher, for hand edits |
| `mailer.py` | the summary email |
| `runlog.py` | run history |
| `sources.py` | the handle list |
| `posts.py` | a day's posts, read from the caption sidecars |
| `prune.py` | deletes transcribed media to reclaim disk |
| `serve.py` | the HTTP layer |
| `config.py` | `config.toml` |
| `atomic.py` | write-then-rename, so a crash cannot truncate a file |
| `records.py` | the dataclasses passed between stages |

## Inject the outside world; do not reach for it

Every external dependency arrives as a parameter with a real default:

```python
def summarize_day(..., runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
```

Production passes nothing and gets `subprocess.run`; tests pass a fake. This is
why the suite needs no network, no model downloads, and no API keys — and why
porting off macOS means replacing `ocr.py` behind its existing interface rather
than rewriting its tests.

Keep it that way. A module that calls `subprocess.run`, opens a socket, or reads
the clock directly at the point of use cannot be tested without mocking the
world, and that is the pattern this codebase is built to avoid.

## Two modules with sharp edges

**Only `notes.py` and `topics.py` may mutate a digest file.** Everything else
reads. The journal lives inside `news/<date>.md` between marker comments, next to
generated content that cannot be regenerated cheaply. If you need to change a
digest from anywhere else, you almost certainly need to re-render it instead.

Both writers follow the same shape, and a third one should too: identify an exact
span, refuse rather than guess when the target is ambiguous, copy everything
outside the span verbatim, and reject any input that could forge a structural
boundary. `topics.py` splits the journal off before it searches, so a note can
never be mistaken for a topic.

## Skipped topics: two records, two questions

A topic the interest filter dropped is stored **in full** in the digest with a
`skipped: <reason>` meta line, so the judgement can be reversed without
recompiling the day. Consequences worth knowing:

- `digest.render_html` renders only kept topics, and filters *before* markdown —
  the published site must not ship the text of everything that was dropped.
- `digest.search` and `digest.all_tags` read kept topics only.
- The **digest is current state** and reflects hand edits. The **run log is
  history** and says what that run decided. They legitimately disagree once a
  topic is restored; do not "fix" that by writing to the run record.
- Frontmatter `tags:`/`sources:` are written once by the run and are not updated
  by a hand edit. Nothing reads them for topic membership, which is what makes
  that safe.

**`render.py` is pure** — topics in, markdown string out, no I/O. That is what
lets `tests/test_render.py` pin the output format exactly. Adding a file read or
a timestamp lookup to it breaks that guarantee, so pass those in.

## Idempotence

Every stage is keyed on whether its output already exists, so re-running a day
redoes only what is missing. Preserve this. It is why a failed summarize costs
seconds to retry rather than a fresh round of downloads and transcription.

A corollary worth knowing: `data/transcripts/<date>/*.none` markers record that
extraction *concluded* with nothing usable. Without them a silent video looks
unfinished forever — re-transcribed on every run, its media never released. An
absent output and a concluded-empty output are different states.

## Fetching

`fetch.py` and `fetch_chrome.py` are the only modules that talk to Instagram.
`fetch.py` owns the walk and the watermarks; the backend behind it is chosen by
`[fetch] backend` and is either `instaloader` (the API, default) or `chrome`
(a real logged-in browser). Both expose the same four methods, and posts from
either wear instaloader's attribute names, so nothing downstream knows which
ran.

**An action block is never retried.** A 400 carrying `feedback_required` means
Instagram has decided the *access pattern* is automated — the credentials are
still good, which is why re-authenticating does not clear it. Answering a
request for less with more is how a slowdown becomes a suspension, so
`ActionBlocked` aborts the whole run on first sight, from either backend. Do
not add a retry, a backoff loop, or a second attempt to that path.

A plain 429 is different and *is* retried with backoff before that one handle is
abandoned — see `_with_retries`. Keep the two distinct.

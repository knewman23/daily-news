# src/ — pipeline modules

Each module owns one stage and is imported by `run_daily.py`, `serve.py`, or
`export_static.py`. Nothing in here imports those three back.

## The modules

| Module | Owns |
|---|---|
| `fetch.py` | instaloader, per-handle watermarks |
| `transcribe.py` | ffmpeg + faster-whisper (video → spoken text) |
| `ocr.py` | Apple Vision (image → on-screen text) |
| `summarize.py` | the one `claude -p` call per day: cluster, de-duplicate, tag, filter |
| `render.py` | topics → markdown |
| `digest.py` | reading digests back: index, topics, search, HTML |
| `notes.py` | the journal block |
| `publish.py` | export and push |
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

**`notes.py` is the only code that may mutate a digest file.** Everything else
reads. The journal lives inside `news/<date>.md` between marker comments, next to
generated content that cannot be regenerated cheaply. If you need to change a
digest from anywhere else, you almost certainly need to re-render it instead.

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

`fetch.py` is the only module that talks to Instagram. A rate-limit response
aborts the whole run and is never retried — answering a request for less with
more is how a slowdown becomes a suspension. Do not add a retry, a backoff loop,
or a second attempt.

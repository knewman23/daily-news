# Daily News — working in this repo

A daily news digest built from Instagram accounts. `README.md` explains what it
does and how the pipeline fits together; read it rather than re-deriving it from
the code. This file is only the rules that are not obvious from reading around.

## Privacy: the one thing that must not go wrong

**`news/` holds the user's private journal.** Each `news/<date>.md` contains the
day's digest *and* hand-written personal notes inside marker comments. The
directory is git-ignored and the notes never leave the machine.

- Do not print, quote, or summarize the contents of `news/` unless asked about a
  specific day.
- Never `git add` `news/`, `data/`, or `logs/`.
- Only `src/notes.py` and `src/topics.py` may mutate a digest file.
- `tests/test_notes.py` is the load-bearing test in this project. It proves that
  saving a journal entry cannot damage the generated summary — the one failure
  here that would destroy something unrecoverable. If it fails, stop.

## Always use the virtualenv

```bash
.venv/bin/python ...      # not `python`
.venv/bin/pytest          # not `pytest`
```

A bare interpreter will not find the dependencies. Every command in `README.md`
is written this way for that reason.

## `web/` is the source; `site/` is generated

`export_static.py` deletes and rebuilds `site/` wholesale on every run. Editing
anything in `site/` by hand loses the change on the next export, and the loss is
silent. Edit `web/`, then:

```bash
.venv/bin/python export_static.py
```

`site/data/**` is generated from `news/`, so it is derived data too — but it is
committed, because it is what GitHub Pages serves.

## Before claiming something works

```bash
.venv/bin/pytest
```

The suite touches no network: Instagram, Whisper, Vision, `claude -p`, git, and
SMTP are all injected at their boundaries. So a green run is fast and means
something.

There is **no JS test harness**. Every test is Python. A change to `web/` is
verified by running the app and looking at it — say so plainly rather than
implying tests covered it:

```bash
.venv/bin/python serve.py     # → http://127.0.0.1:8420
```

## Running the pipeline

Do not run `run_daily.py` without being asked. It hits Instagram, and **the
request pattern is what gets an account flagged.** A rate-limit response aborts
the run and is deliberately never retried; do not add a retry.

To re-summarize a day already on disk, which touches no network at all:

```bash
.venv/bin/python run_daily.py --date 2026-07-22 --no-fetch --quiet
```

## Commits

Lowercase `type: subject`, matching the existing log:

```
news: 2026-07-29 (138 topics)
fix: record extractions that concluded with nothing usable
docs: bring the spec in line with what shipped
```

`news:` commits are written by the pipeline. Do not hand-author them.

## Where things are

- `README.md` — the pipeline, setup, troubleshooting, disk usage, backfilling
- `docs/superpowers/specs/` — design documents, newest first
- `docs/notes/claude-cli-contract.md` — what the summarizer expects from `claude -p`
- `src/CLAUDE.md`, `tests/CLAUDE.md`, `web/CLAUDE.md` — conventions per area
- `config.toml` — hand-edited settings
- `config/sources.json` — the handle list, written by the web UI. Machine-written
  state is kept out of `config.toml` so a bad write cannot corrupt the model
  choice or the port.

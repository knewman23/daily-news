# tests/ — conventions

```bash
.venv/bin/pytest
.venv/bin/pytest tests/test_notes.py -v
```

One test file per module, named for it. Every test is Python; there is no JS
harness, so nothing here covers `web/`.

## The suite never touches the outside world

No network, no model downloads, no API calls, no real `git`, no real SMTP. This
is not enforced by a plugin — it holds because `src/` takes its dependencies as
parameters, and tests pass fakes:

```python
def fake_runner(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0, stdout=json.dumps(TOPICS_JSON))

summarize.summarize_day(..., runner=fake_runner)
```

If a test you are writing seems to need the network, the module under test is
missing a seam. Add the parameter rather than mocking at import time.

Use `tmp_path` for anything that writes. Never point a test at the real `news/`,
`data/`, or `logs/` — `export_static.py` deletes its output directory wholesale,
and a test aimed at the wrong path is destructive.

## `test_notes.py` is the one that matters most

It proves a journal round-trip cannot damage the generated summary. That is the
only failure in this project that destroys something unrecoverable — the digest
can be regenerated, the user's hand-written notes cannot. Treat a failure here as
a stop-work signal, not a test to adjust.

## `test_render.py` pins the markdown format

`render.py` is pure, so these tests assert on exact output. When they fail after
an intentional format change, update the expectation deliberately and check the
new format still parses through `digest.py` — `render` writes what `digest` reads,
and only the tests hold those two together.

## Fixtures

`export_static.py` and `serve.py` tests build synthetic `web/index.html` and
`news/*.md` files rather than reading the real ones, so the suite does not break
every time the real markup changes. Keep that: a test that reads `web/index.html`
turns every UI edit into a test failure without catching a real bug.

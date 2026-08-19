"""Cluster the day's transcripts into topics via the claude CLI.

One subprocess call per day, not per post: every call carries roughly 27k tokens
of base system prompt regardless of prompt size, so per-post calls would multiply
that overhead by the number of posts for no benefit. Batching also means the model
sees every transcript at once, which is what makes same-day de-duplication
possible at all.

The CLI contract this is written against is recorded in
docs/notes/claude-cli-contract.md — it was observed, not assumed. Two details
from it drive the code below: the payload is a JSON string nested inside a JSON
wrapper at `.result`, and a model-level failure still exits 0 with
`is_error: true`, so the exit code alone is not a success check.

Rendering lives in render.py. This module only builds the prompt, runs the
subprocess, and validates what comes back.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from src import atomic, render
from src.records import Stats, Transcript

DEFAULT_MODEL = "claude-opus-5"

MAX_ATTEMPTS = 2

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class SummarizeError(Exception):
    """The CLI failed, or returned something that is not the agreed shape."""


PROMPT_HEADER = """\
You are compiling a daily news digest from Instagram posts. Below is one block
per post, each labelled with the account that posted it and how the text was
obtained:

- "spoken audio" is a transcript of someone talking, so it is conversational and
  may ramble or trail off mid-sentence.
- "text in image" is text read off a graphic by OCR, so it arrives as clipped
  headline fragments, sometimes with the account's own watermark mixed in.
  Ignore watermarks and channel branding.

Group them into distinct news topics and return ONLY a JSON object of this shape:

{"topics": [{"headline": str, "body": str, "tags": [str],
             "sources": [str], "posts": [str]}],
 "skipped": [{"headline": str, "body": str, "tags": [str],
              "sources": [str], "posts": [str], "reason": str}]}

Rules:
- "posts" lists the id of every post the topic was drawn from, copied exactly
  from the "id:" line of the blocks you used. This is what lets a reader open
  the original post, so it must be accurate: never invent an id, and never
  include one you did not actually use.
- Collapse the same story into ONE topic even when several accounts cover it,
  and list every contributing account in that topic's "sources".
- Order topics by significance, most significant first.
- "headline" is a short news headline, no trailing punctuation.
- "body" is 2-4 sentences in neutral news style. Report what was said. Do not
  editorialise, and do not refer to the accounts or to "the video".
- "tags" is 1-3 lowercase single-word topics, e.g. politics, markets, tech,
  health, climate, sports, media, legal.
- "sources" are the account handles that covered the topic, "@"-prefixed.
- Skip transcripts that carry no news content (ads, greetings, music only).
- If nothing in the transcripts is news, return {"topics": []}.

Return the JSON object and nothing else.
"""

INTERESTS_TEMPLATE = """\
This digest is for one reader with specific interests. Judge relevance by what a
story is *about*, not by keywords — a report on the Iran conflict that never says
"Iran" is still relevant, and a passing mention of politics in a personal vlog is
not.

Keep a topic only if it is genuinely about one of these:
{include}
Leave out anything that is mainly:
{exclude}
Every topic you leave out on relevance grounds goes in "skipped", written out in
full — the same "headline", "body", "tags", "sources" and "posts" you would have
given it had you kept it — plus a one-line "reason". Do not silently discard
anything: a reader who cannot see what was dropped cannot tell a well-tuned
filter from one that is throwing away news, and the full text is what lets them
overrule you without the day being compiled again.
"""

TRANSCRIPTS_HEADER = """\
--- TRANSCRIPTS ---
"""


def build_prompt(
    transcripts: Sequence[Transcript],
    interests=None,
) -> str:
    """Assemble the day's prompt.

    The interest filter is part of this one call rather than a second pass per
    post: relevance is a judgement about the same text the model is already
    reading, and a per-post call would multiply the ~27k-token base overhead by
    the number of posts to answer a question it can answer in place.
    """
    header = PROMPT_HEADER + _interests_section(interests) + TRANSCRIPTS_HEADER

    if not transcripts:
        return header + "\n(no transcripts)\n"

    blocks = []
    for t in transcripts:
        source = "text in image" if t.kind == "image" else "spoken audio"
        lines = [f"[@{t.handle}] ({source})", f"id: {t.shortcode}"]
        if t.posted_at:
            lines.append(f"posted: {t.posted_at}")
        if t.caption.strip():
            lines.append(f"caption: {t.caption.strip()}")
        lines.append(f"content: {t.text.strip()}")
        blocks.append("\n".join(lines))

    return header + "\n" + "\n\n".join(blocks) + "\n"


def _interests_section(interests) -> str:
    include = tuple(getattr(interests, "include", ()) or ())
    exclude = tuple(getattr(interests, "exclude", ()) or ())
    if not include and not exclude:
        return ""

    return "\n" + INTERESTS_TEMPLATE.format(
        include="".join(f"  - {item}\n" for item in include) or "  - anything newsworthy\n",
        exclude="".join(f"  - {item}\n" for item in exclude) or "  - nothing in particular\n",
    )


def call_claude(
    prompt: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    model: str = DEFAULT_MODEL,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the CLI and return (topics, skipped).

    Retried once. Beyond that a repeat failure is a real problem — a bad prompt,
    a broken install, an outage — and burning more attempts on it delays the
    error without improving the odds. The transcripts are already on disk, so a
    manual re-run after a fix costs seconds.
    """
    command = [
        "claude", "-p",
        "--output-format", "json",
        "--strict-mcp-config",
        "--model", model,
    ]

    last: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            completed = runner(
                command, input=prompt, capture_output=True, text=True,
            )
            return _parse(completed)
        except SummarizeError as exc:
            last = exc

    raise SummarizeError(f"claude -p failed after {MAX_ATTEMPTS} attempts: {last}")


def summarize_day(
    day: date,
    transcripts: Sequence[Transcript],
    stats: Stats,
    news_dir: str | Path,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    generated: datetime | None = None,
    model: str = DEFAULT_MODEL,
    interests=None,
) -> Path:
    """Write news/<day>.md and return its path.

    With no transcripts the model is not called at all — there is nothing to
    summarize, and the day still gets a file so a silent pipeline failure is
    distinguishable from a genuinely quiet news day.
    """
    topics: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if transcripts:
        topics, skipped = call_claude(
            build_prompt(transcripts, interests), runner=runner, model=model,
        )
        # The run record keeps the flat one-line form: it is a note about what
        # this run decided, and it stays true even after the digest is edited.
        for entry in skipped:
            stats.skipped.append(
                f"{entry['headline']}: {entry['reason'] or 'off topic'}"
            )

    directory = Path(news_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.md"

    atomic.write_text(path, render.render_day(
        day, topics, stats.as_dict(),
        generated=generated or datetime.now(timezone.utc),
        permalinks=permalink_index(transcripts),
        skipped=skipped,
    ))
    return path


def permalink_index(
    transcripts: Sequence[Transcript],
) -> dict[str, tuple[str, str]]:
    """Map post id to (handle, url) so the renderer can link each source.

    Built from the transcripts rather than from the model's output: the model is
    asked to echo ids back, and an echoed id is only trustworthy as a key into
    something already known.
    """
    return {
        t.shortcode: (t.handle, t.permalink)
        for t in transcripts
        if t.shortcode and t.permalink and t.handle
    }


# --- internals -------------------------------------------------------------


def _parse(completed: subprocess.CompletedProcess) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if completed.returncode != 0:
        raise SummarizeError(
            f"claude exited {completed.returncode}: {(completed.stderr or '').strip()[:500]}"
        )

    try:
        wrapper = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SummarizeError(f"could not parse the CLI wrapper JSON: {exc}") from exc

    if not isinstance(wrapper, dict):
        raise SummarizeError("CLI wrapper JSON was not an object")

    # Exit code 0 is not a success check: a model-level failure still exits 0.
    if wrapper.get("is_error"):
        raise SummarizeError(f"CLI reported is_error: {wrapper.get('result')!r}")

    result = wrapper.get("result")
    if not isinstance(result, str):
        raise SummarizeError("CLI wrapper had no string 'result' field")

    payload = _payload(result)
    return _topics(payload), _skipped(payload)


def _payload(result: str) -> Any:
    """Extract the model's JSON object, tolerating a fence or surrounding prose.

    Only the *first* complete JSON object counts. The model sometimes keeps
    talking after it has answered — observed 2026-08-19 restating the entire
    digest as a second JSON object under an "In plain English" heading — and a
    greedy match from the first brace to the last spans both objects and parses
    as nothing at all. `raw_decode` stops at the end of the first value, so
    trailing commentary is ignored rather than fatal.
    """
    cleaned = _FENCE.sub("", result).strip()
    decoder = json.JSONDecoder()

    # Leading prose can itself contain a brace, so the first '{' is not
    # necessarily where the JSON starts. Try each in turn.
    last: json.JSONDecodeError | None = None
    for start in _starts(cleaned):
        try:
            return decoder.raw_decode(cleaned, start)[0]
        except json.JSONDecodeError as exc:
            last = exc

    if last is None:
        raise SummarizeError(f"no JSON object in model output: {cleaned[:300]!r}")
    raise SummarizeError(f"model output was not valid JSON: {last}") from last


def _starts(cleaned: str) -> list[int]:
    """Offsets of every '{' in the output, in order."""
    out = []
    at = cleaned.find("{")
    while at != -1:
        out.append(at)
        at = cleaned.find("{", at + 1)
    return out


def _skipped(payload: Any) -> list[dict[str, Any]]:
    """What the model left out on relevance grounds, in full. Never fatal.

    Kept in the same shape as a topic so it can be stored in the digest and later
    restored to the feed without recompiling the day. Unlike `_topics`, a missing
    body is tolerated rather than raised: a malformed skip list is not worth
    failing a good digest over — the topics are the product, this is the
    explanation — and render.py substitutes a placeholder so the day still writes.
    """
    if not isinstance(payload, dict):
        return []
    entries = payload.get("skipped")
    if not isinstance(entries, list):
        return []

    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        headline = str(entry.get("headline", "")).strip()
        if not headline:
            continue

        body = entry.get("body")
        out.append({
            "headline": headline,
            "body": body.strip() if isinstance(body, str) else "",
            "tags": entry.get("tags") or [],
            "sources": entry.get("sources") or [],
            "posts": entry.get("posts") or [],
            "reason": str(entry.get("reason", "")).strip(),
        })
    return out


def _topics(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise SummarizeError("model output was not an object with a 'topics' list")

    topics = payload.get("topics")
    if not isinstance(topics, list):
        raise SummarizeError("model output had no 'topics' list")

    for topic in topics:
        if not isinstance(topic, dict):
            raise SummarizeError(f"topic was not an object: {topic!r}")
        for required in ("headline", "body"):
            value = topic.get(required)
            if not isinstance(value, str) or not value.strip():
                raise SummarizeError(f"topic is missing {required!r}: {topic!r}")

    return topics

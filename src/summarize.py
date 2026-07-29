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

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class SummarizeError(Exception):
    """The CLI failed, or returned something that is not the agreed shape."""


PROMPT_HEADER = """\
You are compiling a daily news digest from transcribed audio of Instagram video
posts. Below are today's transcripts, one per post, each labelled with the
account that posted it.

Group them into distinct news topics and return ONLY a JSON object of this shape:

{"topics": [{"headline": str, "body": str, "tags": [str], "sources": [str]}]}

Rules:
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

--- TRANSCRIPTS ---
"""


def build_prompt(transcripts: Sequence[Transcript]) -> str:
    if not transcripts:
        return PROMPT_HEADER + "\n(no transcripts)\n"

    blocks = []
    for t in transcripts:
        lines = [f"[@{t.handle}]"]
        if t.posted_at:
            lines.append(f"posted: {t.posted_at}")
        if t.permalink:
            lines.append(f"link: {t.permalink}")
        if t.caption.strip():
            lines.append(f"caption: {t.caption.strip()}")
        lines.append(f"transcript: {t.text.strip()}")
        blocks.append("\n".join(lines))

    return PROMPT_HEADER + "\n" + "\n\n".join(blocks) + "\n"


def call_claude(
    prompt: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    model: str = DEFAULT_MODEL,
) -> list[dict[str, Any]]:
    """Run the CLI and return the validated topic list.

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
) -> Path:
    """Write news/<day>.md and return its path.

    With no transcripts the model is not called at all — there is nothing to
    summarize, and the day still gets a file so a silent pipeline failure is
    distinguishable from a genuinely quiet news day.
    """
    topics = (
        call_claude(build_prompt(transcripts), runner=runner, model=model)
        if transcripts else []
    )

    directory = Path(news_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.md"

    atomic.write_text(path, render.render_day(
        day, topics, stats.as_dict(),
        generated=generated or datetime.now(timezone.utc),
    ))
    return path


# --- internals -------------------------------------------------------------


def _parse(completed: subprocess.CompletedProcess) -> list[dict[str, Any]]:
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

    return _topics(_payload(result))


def _payload(result: str) -> Any:
    """Extract the model's JSON object, tolerating a fence or surrounding prose."""
    cleaned = _FENCE.sub("", result).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT.search(cleaned)
    if not match:
        raise SummarizeError(f"no JSON object in model output: {cleaned[:300]!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise SummarizeError(f"model output was not valid JSON: {exc}") from exc


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

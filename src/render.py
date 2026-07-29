"""Turn the summarizer's topic list into the day's markdown file.

Kept separate from summarize.py on purpose: this is a pure function of its
arguments, so the exact output format is pinned by tests, while the subprocess
call that produces the topics stays in a module with nothing else in it.

`generated` is a parameter rather than datetime.now() so the output is
deterministic under test.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from src.notes import END, START

_WHITESPACE = re.compile(r"\s+")

EMPTY_BODY = "_No posts found for this day._"

# Only ever used for a skipped topic the model described without a body. A kept
# topic with no body is a real error and still raises — but refusing to write a
# whole good digest because the *explanation* of a drop came back thin would be
# the wrong trade.
NO_BODY = "_No text was stored for this topic._"


def render_day(
    day: date,
    topics: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    generated: datetime,
    permalinks: Mapping[str, str] | None = None,
    skipped: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    if generated.tzinfo is None:
        raise ValueError("generated timestamp must be timezone-aware")

    cleaned = [_clean_topic(t, permalinks or {}) for t in topics]
    # Skipped topics are stored in full, after the kept ones, so the filter's
    # judgement can be reversed later without re-summarizing the day. They are
    # deliberately absent from the frontmatter — see _frontmatter.
    dropped = [_clean_topic(t, permalinks or {}, skipped=True)
               for t in (skipped or [])]

    frontmatter = _frontmatter(day, cleaned, stats, generated)
    title = f"# {day.strftime('%B')} {day.day}, {day.year}"

    if cleaned or dropped:
        sections = [_section(t) for t in [*cleaned, *dropped]]
    else:
        sections = [EMPTY_BODY]

    parts = [frontmatter, title, *sections, f"## My Notes\n{START}\n{END}"]
    return "\n\n".join(parts) + "\n"


# --- internals -------------------------------------------------------------


def _clean_topic(
    topic: Mapping[str, Any],
    permalinks: Mapping[str, str],
    skipped: bool = False,
) -> dict[str, Any]:
    headline = _collapse(topic.get("headline", ""))
    if not headline:
        raise ValueError(f"topic has no headline: {topic!r}")

    body = topic.get("body")
    if not isinstance(body, str) or not body.strip():
        if not skipped:
            raise ValueError(f"topic {headline!r} has no body")
        body = NO_BODY

    # Only ids we already know are kept. The model echoes these back, and an
    # invented one would render as a link to a post that does not exist —
    # worse than no link, because it looks checkable and is not.
    posts = [
        p.strip() for p in _as_list(topic.get("posts"))
        if p.strip() in permalinks
    ]

    # One link per handle, not per post. An account that covered the same story
    # twice would otherwise render as "@handle, @handle", which reads as a
    # mistake. The first post it was drawn from is the one linked.
    by_handle: dict[str, str] = {}
    for post in dict.fromkeys(posts):
        handle, url = permalinks[post]
        by_handle.setdefault(handle, url)
    links = list(by_handle.items())

    handles = [_handle(s) for s in _as_list(topic.get("sources")) if s.strip()]
    linked = {handle for handle, _ in links}

    return {
        "headline": headline,
        "body": body.strip(),
        # Collapsed to one line: the reason sits on a `skipped:` meta line, and a
        # newline in it would end the line and leave the tail as body text.
        "skipped": _collapse(topic.get("reason") or "off topic") if skipped else "",
        "tags": [t.strip().lower() for t in _as_list(topic.get("tags")) if t.strip()],
        # Order matters: linked handles first, then any the model named without
        # a usable post id, so nothing the model attributed is silently dropped.
        "sources": [h for h, _ in links] + [h for h in handles if h not in linked],
        "links": links,
    }


def _section(topic: Mapping[str, Any]) -> str:
    """One topic. The sources line carries the links.

    A separate line of bare post ids reads as noise — nobody recognises
    "DbWomCQPL7Y" — so the handle itself is the link. digest.py strips the
    markdown back off when it needs bare handles for filtering.
    """
    lines = [f"## {topic['headline']}"]
    if topic["tags"]:
        lines.append(f"tags: {', '.join(topic['tags'])}")

    if topic["sources"]:
        linked = dict(topic["links"])
        lines.append("sources: " + ", ".join(
            f"[@{handle}]({linked[handle]})" if handle in linked else f"@{handle}"
            for handle in topic["sources"]
        ))

    # Last, so a section reads as: what it is, who covered it, why it was
    # dropped. Position is presentational only — the parser consumes the meta
    # lines as an unordered run and stops at the first line that is not one.
    if topic.get("skipped"):
        lines.append(f"skipped: {topic['skipped']}")

    return "\n".join(lines) + f"\n\n{topic['body']}"


def _frontmatter(
    day: date,
    topics: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    generated: datetime,
) -> str:
    """The header, built from kept topics only.

    A hand edit to a `skipped:` line does not rewrite this header, so it records
    what the run decided rather than the current state. That is safe because
    nothing user-facing reads it for topic membership: the chips come from
    `digest.all_tags` and search from `digest.search`, both of which read the
    sections themselves and see the edit.
    """
    tags = _unique(t for topic in topics for t in topic["tags"])
    handles = _unique(s for topic in topics for s in topic["sources"])

    rows = [
        f"date: {day.isoformat()}",
        f"generated: {generated.isoformat()}",
        f"tags: [{', '.join(tags)}]",
        f"sources: [{', '.join(f'\"@{h}\"' for h in handles)}]",
        f"post_count: {int(stats.get('post_count', 0))}",
        f"transcribed_count: {int(stats.get('transcribed_count', 0))}",
        f"incomplete: {'true' if stats.get('incomplete') else 'false'}",
    ]
    return "---\n" + "\n".join(rows) + "\n---"


def _unique(values: Iterable[str]) -> list[str]:
    """Dedupe while keeping first-seen order, so the header reads in topic order."""
    return list(dict.fromkeys(values))


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _handle(raw: str) -> str:
    return raw.strip().lstrip("@").lower()


def _collapse(value: Any) -> str:
    return _WHITESPACE.sub(" ", str(value)).strip()

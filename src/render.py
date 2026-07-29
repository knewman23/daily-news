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


def render_day(
    day: date,
    topics: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    generated: datetime,
    permalinks: Mapping[str, str] | None = None,
) -> str:
    if generated.tzinfo is None:
        raise ValueError("generated timestamp must be timezone-aware")

    cleaned = [_clean_topic(t, permalinks or {}) for t in topics]

    frontmatter = _frontmatter(day, cleaned, stats, generated)
    title = f"# {day.strftime('%B')} {day.day}, {day.year}"

    if cleaned:
        sections = [_section(t) for t in cleaned]
    else:
        sections = [EMPTY_BODY]

    parts = [frontmatter, title, *sections, f"## My Notes\n{START}\n{END}"]
    return "\n\n".join(parts) + "\n"


# --- internals -------------------------------------------------------------


def _clean_topic(
    topic: Mapping[str, Any],
    permalinks: Mapping[str, str],
) -> dict[str, Any]:
    headline = _collapse(topic.get("headline", ""))
    if not headline:
        raise ValueError(f"topic has no headline: {topic!r}")

    body = topic.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError(f"topic {headline!r} has no body")

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

    return "\n".join(lines) + f"\n\n{topic['body']}"


def _frontmatter(
    day: date,
    topics: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    generated: datetime,
) -> str:
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

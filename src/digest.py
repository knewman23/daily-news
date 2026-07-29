"""Reading generated digests back: index, topics, search, HTML.

Read-only with respect to news/. Nothing here writes.

Unlike sources.json, a malformed digest degrades rather than raising. A source
list that cannot be read means the next run would fetch nothing, which must be
loud. A single old digest with a broken header only affects how that one day
renders, and failing hard there would take the whole page down with it.

Everything from `## My Notes` onward is cut before parsing. The user's journal
must never be mistaken for news — not as a topic, not as a search hit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import markdown
import yaml

FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
NOTES_HEADING = "## My Notes"

_SECTION_SPLIT = re.compile(r"^## ", re.MULTILINE)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_META_LINE = re.compile(r"^(tags|sources):\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class DayMeta:
    date: date
    path: Path
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    post_count: int = 0
    transcribed_count: int = 0
    incomplete: bool = False


@dataclass(frozen=True)
class Topic:
    headline: str
    tags: list[str]
    sources: list[str]
    body: str


@dataclass(frozen=True)
class Hit:
    date: date
    headline: str
    tags: list[str]
    snippet: str


def list_days(news_dir: str | Path) -> list[DayMeta]:
    """Every dated digest, newest first."""
    directory = Path(news_dir)
    if not directory.is_dir():
        return []

    days = []
    for path in directory.iterdir():
        match = FILENAME_RE.match(path.name)
        if match:
            days.append(_meta(path, date.fromisoformat(match.group(1))))
    return sorted(days, key=lambda d: d.date, reverse=True)


def topics_of(path: str | Path) -> list[Topic]:
    body = _news_body(Path(path).read_text(encoding="utf-8"))
    return [t for t in (_topic(chunk) for chunk in _SECTION_SPLIT.split(body)[1:]) if t]


def render_html(path: str | Path) -> str:
    body = _news_body(Path(path).read_text(encoding="utf-8"))
    return markdown.markdown(body, extensions=["extra", "sane_lists"])


def search(
    news_dir: str | Path,
    query: str | None,
    tag: str | None = None,
) -> list[Hit]:
    """Topic sections matching a query, a tag, or both. Newest day first.

    An empty query with no tag returns nothing rather than everything — an
    empty search box should show no results, not the entire archive.
    """
    needle = (query or "").strip().lower()
    wanted = (tag or "").strip().lower()
    if not needle and not wanted:
        return []

    hits = []
    for day in list_days(news_dir):
        for topic in topics_of(day.path):
            if wanted and wanted not in topic.tags:
                continue
            if needle and needle not in f"{topic.headline}\n{topic.body}".lower():
                continue
            hits.append(Hit(day.date, topic.headline, topic.tags,
                            _snippet(topic.body)))
    return hits


def all_tags(news_dir: str | Path) -> list[str]:
    seen: dict[str, None] = {}
    for day in list_days(news_dir):
        for topic in topics_of(day.path):
            for t in topic.tags:
                seen[t] = None
    return list(seen)


# --- internals -------------------------------------------------------------


def _meta(path: Path, day: date) -> DayMeta:
    front = _frontmatter(path.read_text(encoding="utf-8"))
    return DayMeta(
        date=day,
        path=path,
        tags=_str_list(front.get("tags")),
        sources=_str_list(front.get("sources")),
        post_count=_int(front.get("post_count")),
        transcribed_count=_int(front.get("transcribed_count")),
        incomplete=bool(front.get("incomplete", False)),
    )


def _frontmatter(text: str) -> dict:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _news_body(text: str) -> str:
    """Strip the frontmatter and everything from the notes heading onward."""
    body = _FRONTMATTER.sub("", text, count=1)
    return body.split(NOTES_HEADING, 1)[0].rstrip() + "\n"


def _topic(chunk: str) -> Topic | None:
    headline, _, rest = chunk.partition("\n")
    headline = headline.strip()
    if not headline:
        return None

    tags: list[str] = []
    sources: list[str] = []
    lines = rest.splitlines()

    consumed = 0
    for line in lines:
        match = _META_LINE.match(line.strip())
        if not match:
            break
        values = [v.strip() for v in match.group(2).split(",") if v.strip()]
        if match.group(1).lower() == "tags":
            tags = [v.lower() for v in values]
        else:
            sources = values
        consumed += 1

    return Topic(headline, tags, sources, "\n".join(lines[consumed:]).strip())


def _snippet(body: str, limit: int = 200) -> str:
    flat = " ".join(body.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def _str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return [str(v) for v in value]
    except TypeError:
        return []


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

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
_META_LINE = re.compile(r"^(tags|sources|posts|skipped):\s*(.*)$", re.IGNORECASE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


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
    links: list[tuple[str, str]] = field(default_factory=list)
    # Why the interest filter left this out, or "" if it is in the feed. The
    # reason doubles as the flag: there is no skipped topic without one, because
    # a drop nobody can explain is indistinguishable from a bug.
    skipped: str = ""

    @property
    def kept(self) -> bool:
        return not self.skipped


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
    """Every topic in the file, kept and skipped alike, in file order."""
    body = _news_body(Path(path).read_text(encoding="utf-8"))
    return [t for t in (_topic(chunk) for chunk in _SECTION_SPLIT.split(body)[1:]) if t]


def kept_of(path: str | Path) -> list[Topic]:
    return [t for t in topics_of(path) if t.kept]


def skipped_of(path: str | Path) -> list[Topic]:
    """What the filter left out, in file order. Newest-day-first is the caller's job."""
    return [t for t in topics_of(path) if not t.kept]


def render_html(path: str | Path) -> str:
    """Render the day's *feed* as HTML — skipped topics are dropped.

    They are in the file so the filter's judgement can be reversed, but they are
    not news until restored. Sections are filtered before markdown rather than
    hidden after it: the published site would otherwise ship the full text of
    every dropped topic to anyone who opened devtools.

    `nl2br` matters: each topic's `tags:` / `sources:` / `posts:` lines form one
    markdown paragraph, and without it they collapse onto a single run-together
    line ("tags: politics sources: @handle").
    """
    text = Path(path).read_text(encoding="utf-8")
    body = _feed_body(_news_body(text))
    return markdown.markdown(body, extensions=["extra", "sane_lists", "nl2br"])


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
        # Skipped topics are not searchable: a topic the filter dropped turning
        # up in results would blur what the filter is for. Restoring one makes it
        # searchable in the same move, with no reindexing.
        for topic in kept_of(day.path):
            if wanted and wanted not in topic.tags:
                continue
            if needle and needle not in f"{topic.headline}\n{topic.body}".lower():
                continue
            hits.append(Hit(day.date, topic.headline, topic.tags,
                            snippet(topic.body)))
    return hits


def all_tags(news_dir: str | Path) -> list[str]:
    """Every tag in the feed. Skipped topics contribute none — a chip that
    filtered to nothing would be worse than an absent one."""
    seen: dict[str, None] = {}
    for day in list_days(news_dir):
        for topic in kept_of(day.path):
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


def _feed_body(body: str) -> str:
    """Drop skipped sections, keeping the title and the kept ones verbatim.

    A chunk that does not parse as a topic is kept: an unreadable section is a
    malformed digest, and silently deleting it from the view would hide that.
    """
    parts = _SECTION_SPLIT.split(body)
    out = [parts[0]]
    for chunk in parts[1:]:
        topic = _topic(chunk)
        if topic and not topic.kept:
            continue
        out.append(f"## {chunk}")
    return "".join(out).rstrip() + "\n"


def _topic(chunk: str) -> Topic | None:
    headline, _, rest = chunk.partition("\n")
    headline = headline.strip()
    if not headline:
        return None

    tags: list[str] = []
    sources: list[str] = []
    links: list[tuple[str, str]] = []
    skipped = ""
    lines = rest.splitlines()

    consumed = 0
    for line in lines:
        match = _META_LINE.match(line.strip())
        if not match:
            break

        field_name = match.group(1).lower()
        raw = match.group(2)

        if field_name == "tags":
            tags = [v.strip().lower() for v in raw.split(",") if v.strip()]
        elif field_name == "skipped":
            # An empty `skipped:` counts as kept, so clearing the reason and
            # deleting the line mean the same thing. Two spellings of restored
            # would otherwise be one more thing that can disagree.
            skipped = raw.strip()
        else:
            # The sources line carries markdown links: `[@handle](url)`. Strip the
            # markup back off so filtering still compares bare handles, and keep
            # the urls alongside. `posts:` is the older shape, still parsed so
            # digests written before the change keep working.
            links.extend((m.group(1).lstrip("@"), m.group(2))
                         for m in _MD_LINK.finditer(raw))
            if field_name == "sources":
                sources = [
                    v.strip() for v in _MD_LINK.sub(r"\1", raw).split(",") if v.strip()
                ]
        consumed += 1

    return Topic(headline, tags, sources, "\n".join(lines[consumed:]).strip(),
                 links, skipped)


def snippet(body: str, limit: int = 200) -> str:
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

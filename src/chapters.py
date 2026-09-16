"""Filing the archive's topics into the book's threads.

The archive is a pile of days. This turns it into a structure without touching
the days themselves.

**Two levels, and only one of them is a classification target.** Topics are filed
into *threads* — narrow story arcs like "Iran war authorization fight". Threads
are grouped into *parts*, which are the book-shaped units. Parts hold nothing but
thread ids, so splitting, merging or renaming one rewrites `outline.json` alone
and reclassifies not a single item. That separation is the whole point: it is
what lets assignments stay still while the outline keeps moving.

**Nothing here writes to `news/`.** Assignments live in a sidecar keyed by
headline. `CLAUDE.md` reserves digest files for `notes.py` and `topics.py`
because they carry the user's journal, and a book feature has no business being
able to damage it.

**The model's thread ids are checked, not trusted.** An invented id would land in
a day file and produce a thread that exists in assignments and nowhere else,
which the review page could not resolve. An id the outline does not have is
filed as unfiled, with the invented id named in the reason.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from src import atomic, summarize
from src.digest import Topic

log = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess]

DEFAULT_MODEL = summarize.DEFAULT_MODEL
MAX_ATTEMPTS = 2


class ChaptersError(Exception):
    """Base class for every failure this module raises."""


class OutlineError(ChaptersError):
    """`outline.json` is missing, unreadable, or not the shape we expect."""


@dataclass(frozen=True)
class Thread:
    id: str
    title: str
    description: str = ""
    created: str = ""


@dataclass(frozen=True)
class Part:
    id: str
    title: str
    thread_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Outline:
    version: int
    revised_at: str
    parts: list[Part] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)

    def thread(self, thread_id: str) -> Thread | None:
        return next((t for t in self.threads if t.id == thread_id), None)

    @property
    def thread_ids(self) -> set[str]:
        return {t.id for t in self.threads}


# --- the outline file ------------------------------------------------------


def outline_from_dict(data: Any) -> Outline:
    if not isinstance(data, dict):
        raise OutlineError("the outline must be a JSON object")

    version = data.get("version")
    if not isinstance(version, int):
        raise OutlineError("the outline needs an integer \"version\"")

    threads = [
        Thread(
            id=str(t.get("id") or ""),
            title=str(t.get("title") or ""),
            description=str(t.get("description") or ""),
            created=str(t.get("created") or ""),
        )
        for t in data.get("threads") or []
        if isinstance(t, dict)
    ]
    if any(not t.id for t in threads):
        raise OutlineError("every thread needs an \"id\"")

    seen = [t.id for t in threads]
    if len(seen) != len(set(seen)):
        raise OutlineError("thread ids must be unique")

    parts = [
        Part(
            id=str(p.get("id") or ""),
            title=str(p.get("title") or ""),
            thread_ids=[str(x) for x in (p.get("thread_ids") or [])],
        )
        for p in data.get("parts") or []
        if isinstance(p, dict)
    ]

    return Outline(
        version=version,
        revised_at=str(data.get("revised_at") or ""),
        parts=parts,
        threads=threads,
    )


def outline_to_dict(outline: Outline) -> dict:
    return {
        "version": outline.version,
        "revised_at": outline.revised_at,
        "parts": [
            {"id": p.id, "title": p.title, "thread_ids": list(p.thread_ids)}
            for p in outline.parts
        ],
        "threads": [
            {"id": t.id, "title": t.title, "description": t.description,
             "created": t.created}
            for t in outline.threads
        ],
    }


def outline_path(book_dir: str | Path) -> Path:
    return Path(book_dir) / "outline.json"


def load_outline(book_dir: str | Path) -> Outline:
    """Read the outline. Never invents one.

    A missing or corrupt outline is not recoverable by guessing: the structure
    was approved by hand, and silently rebuilding it would discard that and
    re-point every day file at threads the user never agreed to.
    """
    path = outline_path(book_dir)
    if not path.exists():
        raise OutlineError(
            f"no outline at {path}. Run `book.py bootstrap` to derive one."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutlineError(f"could not read {path}: {exc}") from exc

    return outline_from_dict(data)


def save_outline(book_dir: str | Path, outline: Outline) -> None:
    book = Path(book_dir)
    (book / "days").mkdir(parents=True, exist_ok=True)
    atomic.write_json(outline_path(book), outline_to_dict(outline))


# --- day files -------------------------------------------------------------


def day_path(book_dir: str | Path, day: date) -> Path:
    return Path(book_dir) / "days" / f"{day.isoformat()}.json"


def is_classified(book_dir: str | Path, day: date) -> bool:
    return day_path(book_dir, day).exists()


def load_day(book_dir: str | Path, day: date) -> dict | None:
    path = day_path(book_dir, day)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChaptersError(f"could not read {path}: {exc}") from exc


def classified_days(book_dir: str | Path) -> list[date]:
    days = Path(book_dir) / "days"
    if not days.exists():
        return []
    out = []
    for path in sorted(days.glob("*.json")):
        try:
            out.append(date.fromisoformat(path.stem))
        except ValueError:
            continue          # not a day file; leave it alone
    return out


def stale_days(book_dir: str | Path) -> list[date]:
    """Days filed under an older *thread* vocabulary.

    `version` counts the threads, not the outline as a whole, because threads are
    the only thing filing depends on. Moving a thread between parts, renaming a
    part or adding one changes no assignment, so it must not bump `version`:
    doing so would mark every day stale and invite a re-file that recomputes
    exactly what is already on disk — one model call per day, for nothing.

    Bump `version` when a thread is added, removed, split or merged. Record a
    part-only edit in `revised_at` alone.
    """
    current = load_outline(book_dir).version
    out = []
    for day in classified_days(book_dir):
        record = load_day(book_dir, day) or {}
        if record.get("outline_version") != current:
            out.append(day)
    return out


# --- classification --------------------------------------------------------


def classify_day(
    book_dir: str | Path,
    day: date,
    topics: Sequence[Topic],
    runner: Runner = subprocess.run,
    model: str = DEFAULT_MODEL,
) -> dict:
    """File one day's topics into threads and write the day file.

    Raises on a model failure. Deciding that a failure is not fatal is the
    caller's job, not this module's — `run_daily.py` treats it the way it treats
    a failed publish, and `book.py sync` reports it and moves to the next day.
    """
    outline = load_outline(book_dir)
    kept = [t for t in topics if not t.skipped]

    if not kept:
        # No call at all for an empty day: every call carries ~27k tokens of
        # fixed overhead, and there is nothing here to spend it on.
        return _write_day(book_dir, day, outline.version, [], [])

    payload = _call(build_prompt(outline, kept), runner=runner, model=model)
    return _file(book_dir, day, outline, kept, payload)


def build_prompt(outline: Outline, topics: Sequence[Topic]) -> str:
    threads = "\n".join(
        f"- {t.id}: {t.title}" + (f" — {t.description.strip()}" if t.description else "")
        for t in outline.threads
    )
    items = "\n".join(
        f"- {t.headline}\n  {_first_sentence(t.body)}" for t in topics
    )

    return (
        "You are filing news items into the threads of a book outline.\n\n"
        "THREADS:\n" + threads + "\n\n"
        "ITEMS:\n" + items + "\n\n"
        "For each item, choose the one thread it belongs to. Use only the thread "
        "ids listed above; do not invent one. If no thread genuinely fits, return "
        "thread_id null and say in \"why\" what a thread would have to cover. A "
        "forced fit is worse than an unfiled item: unfiled items are what argue "
        "for new threads later.\n\n"
        "Reply with JSON only:\n"
        "{\"assignments\": [{\"headline\": \"<copied exactly>\", "
        "\"thread_id\": \"<id or null>\", \"why\": \"<only when null>\"}]}"
    )


def _call(prompt: str, runner: Runner, model: str) -> Any:
    command = [
        "claude", "-p",
        "--output-format", "json",
        "--strict-mcp-config",
        "--model", model,
    ]

    last: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            completed = runner(command, input=prompt, capture_output=True, text=True)
            return summarize.payload_of(completed)
        except summarize.SummarizeError as exc:
            last = exc

    raise ChaptersError(f"claude -p failed after {MAX_ATTEMPTS} attempts: {last}")


def _file(
    book_dir: str | Path,
    day: date,
    outline: Outline,
    topics: Sequence[Topic],
    payload: Any,
) -> dict:
    """Turn the model's answer into assignments, checking every thread id."""
    answers = {}
    if isinstance(payload, dict):
        for row in payload.get("assignments") or []:
            if isinstance(row, dict) and isinstance(row.get("headline"), str):
                answers[row["headline"].strip()] = row

    known = outline.thread_ids
    assignments: list[dict] = []
    unfiled: list[dict] = []

    for topic in topics:
        row = answers.get(topic.headline.strip())

        if row is None:
            unfiled.append({"headline": topic.headline,
                            "why": "the model returned no answer for this item"})
            continue

        thread_id = row.get("thread_id")
        if thread_id is None:
            unfiled.append({"headline": topic.headline,
                            "why": str(row.get("why") or "no thread fits")})
            continue

        if thread_id not in known:
            unfiled.append({
                "headline": topic.headline,
                "why": f"the model answered with {thread_id!r}, "
                       f"which is not a thread in this outline",
            })
            continue

        assignments.append({"headline": topic.headline, "thread_id": thread_id})

    return _write_day(book_dir, day, outline.version, assignments, unfiled)


def _write_day(
    book_dir: str | Path,
    day: date,
    version: int,
    assignments: list[dict],
    unfiled: list[dict],
) -> dict:
    record = {
        "date": day.isoformat(),
        "outline_version": version,
        "assignments": assignments,
        "unfiled": unfiled,
    }
    path = day_path(book_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(path, record)
    return record


# --- deriving the structure ------------------------------------------------

# A week's headlines fit a prompt comfortably; the whole archive does not. 1,364
# topics is far past what one call can weigh properly, and a call that has to
# skim is a call that returns the obvious.
BATCH_DAYS = 7


def archive_topics(news_dir: str | Path) -> list[tuple[date, list[Topic]]]:
    """Every day's kept topics, oldest first."""
    from src import digest

    out = []
    for meta in sorted(digest.list_days(news_dir), key=lambda m: m.date):
        path = Path(news_dir) / f"{meta.date.isoformat()}.md"
        if path.exists():
            out.append((meta.date, digest.kept_of(path)))
    return out


def batches(days: Sequence[tuple[date, list[Topic]]],
            size: int = BATCH_DAYS) -> list[tuple[str, list[Topic]]]:
    out = []
    for at in range(0, len(days), size):
        chunk = days[at:at + size]
        topics = [t for _, ts in chunk for t in ts]
        if topics:
            out.append((f"{chunk[0][0].isoformat()}..{chunk[-1][0].isoformat()}",
                        topics))
    return out


def candidate_threads(
    label: str,
    topics: Sequence[Topic],
    runner: Runner = subprocess.run,
    model: str = DEFAULT_MODEL,
) -> list[dict]:
    """Recurring story arcs in one batch. Candidates only — nothing is final."""
    items = "\n".join(f"- {t.headline}" for t in topics)
    prompt = (
        f"These are news headlines from {label}.\n\n{items}\n\n"
        "Identify the recurring story arcs — the threads a reader would follow "
        "across days, not one-off events. A thread should cover several items "
        "and have a clear subject a chapter could be written about.\n\n"
        "Ignore how often something appears if it is only one account's running "
        "commentary; prefer arcs with developments.\n\n"
        "Reply with JSON only:\n"
        "{\"threads\": [{\"title\": \"...\", \"description\": \"one sentence\"}]}"
    )

    payload = _call(prompt, runner=runner, model=model)
    rows = payload.get("threads") if isinstance(payload, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("title")]


def consolidate(
    candidates: Sequence[dict],
    revised_at: str,
    version: int = 1,
    runner: Runner = subprocess.run,
    model: str = DEFAULT_MODEL,
) -> Outline:
    """Merge per-batch candidates into one outline: threads grouped into parts."""
    listed = "\n".join(
        f"- {c.get('title')}: {c.get('description', '')}" for c in candidates
    )
    prompt = (
        "These are candidate story threads found independently in consecutive "
        f"stretches of one news archive.\n\n{listed}\n\n"
        "Merge duplicates and near-duplicates into a single thread each, then "
        "group the result into 6-10 parts — the book-shaped units. Give every "
        "thread a slug id: lowercase, hyphenated, stable.\n\n"
        "Reply with JSON only:\n"
        "{\"threads\": [{\"id\": \"...\", \"title\": \"...\", "
        "\"description\": \"...\"}], "
        "\"parts\": [{\"id\": \"...\", \"title\": \"...\", "
        "\"thread_ids\": [\"...\"]}]}"
    )

    payload = _call(prompt, runner=runner, model=model)
    if not isinstance(payload, dict):
        raise ChaptersError("the consolidation reply was not a JSON object")

    for thread in payload.get("threads") or []:
        if isinstance(thread, dict):
            thread.setdefault("created", revised_at)

    return outline_from_dict({
        "version": version,
        "revised_at": revised_at,
        "threads": payload.get("threads") or [],
        "parts": payload.get("parts") or [],
    })


# --- fragmentation ---------------------------------------------------------

# Words that say nothing about what an item is about. Deliberately short: the
# thread-spread guard below does the heavy lifting, because a term common enough
# to need suppressing is a term that appears in most threads anyway.
_STOPWORDS = frozenset("""
about after again against amid among another over under into with without from
that this these those than then there their them they when what which while who
whom whose will would could should says said tell told more most other some such
being been have has had was were are is be as at by for in of on to a an and or
but not no its it his her him she he you your our us we new now first last two
three report reports reported plan plans call calls called make makes made take
takes taken say back down out off up how why also may might can cause claim
claims year years day days week weeks month months time times people
""".split())


def fragments(
    book_dir: str | Path,
    min_items: int = 8,
    min_threads: int = 3,
    max_top_share: float = 0.7,
    max_thread_spread: float = 0.5,
    proper_ratio: float = 0.7,
    limit: int = 20,
) -> list[dict]:
    """Subjects that are filed but scattered — the gap `unfiled` cannot show.

    The revision command proposes new threads from the unfiled queue, which
    finds gaps. It cannot find *mis-grouping*: material that is all filed, just
    filed apart. Israel was the case that exposed this — 84 items across 16
    threads in 6 parts, nothing unfiled, and no part named for it.

    A subject is fragmented when a term recurs often (`min_items`), across
    several threads (`min_threads`), with no thread holding most of it
    (`max_top_share`).

    `max_thread_spread` is what separates a subject from background. A term in
    most of the threads is not a missing chapter, it is the archive's weather —
    "trump" appears everywhere and means nothing structural. A real subject
    clusters in a minority of threads.
    """
    by_term: dict[str, dict[str, int]] = {}
    caps: dict[str, list[int]] = {}       # [capitalised, total], off-initial only

    for day in classified_days(book_dir):
        record = load_day(book_dir, day) or {}
        for row in record.get("assignments") or []:
            thread_id = row.get("thread_id")
            if not thread_id:
                continue
            for term, capitalised, initial in _terms(str(row.get("headline") or "")):
                counts = by_term.setdefault(term, {})
                counts[thread_id] = counts.get(thread_id, 0) + 1
                if not initial:
                    seen = caps.setdefault(term, [0, 0])
                    seen[0] += int(capitalised)
                    seen[1] += 1

    total_threads = len({
        tid for counts in by_term.values() for tid in counts
    }) or 1

    # A subject already named by a part is gathered, not scattered. "iran" is in
    # sixteen threads because The Iran War is a whole part of the book — that is
    # the structure working, not a defect to report.
    covered = {
        w for part in load_outline(book_dir).parts
        for w in re.findall(r"[a-z][a-z'\-]{3,}", part.title.lower())
    }

    out = []
    for term, counts in by_term.items():
        items = sum(counts.values())
        threads = len(counts)
        if items < min_items or threads < min_threads:
            continue
        if threads / total_threads > max_thread_spread:
            continue
        if term in covered:
            continue

        # Proper nouns are the terms that deserve a chapter. "strikes" and
        # "military" spread across threads because three different wars involve
        # strikes; that is description, not a subject anyone would gather.
        capitalised, seen = caps.get(term, (0, 0))
        if not seen or capitalised / seen < proper_ratio:
            continue

        top = max(counts.values())
        share = top / items
        if share > max_top_share:
            continue

        out.append({
            "term": term,
            "items": items,
            "threads": threads,
            "top_share": round(share, 2),
            # How many items sit outside the thread that holds the most of them:
            # the size of the consolidation this would be.
            "away": items - top,
            "where": sorted(counts.items(), key=lambda kv: -kv[1]),
        })

    out.sort(key=lambda r: (-r["away"], -r["items"]))
    return out[:limit]


def _terms(headline: str) -> list[tuple[str, bool, bool]]:
    """(term, was capitalised, was the first word), one per distinct term.

    The possessive is folded away so "Trump's" and "Trump" are one subject
    rather than two half-sized ones.
    """
    words = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", headline)
    out: dict[str, tuple[str, bool, bool]] = {}
    for at, word in enumerate(words):
        term = word.lower().removesuffix("'s").removesuffix("s'")
        if len(term) < 4 or term in _STOPWORDS:
            continue
        out.setdefault(term, (term, word[:1].isupper(), at == 0))
    return list(out.values())


def split_subjects(
    book_dir: str | Path,
    per_thread: int = 30,
    runner: Runner = subprocess.run,
    model: str = DEFAULT_MODEL,
) -> list[dict]:
    """Subjects spread across threads, judged by reading rather than counting.

    `fragments` counts words, and that is precisely why it missed the case it was
    built for: Israel, Israeli, Gaza and Netanyahu are one subject appearing as
    four terms, each individually too small or too concentrated to trip a
    threshold — "gaza" looked like a model citizen at 22 items in a single
    thread. No amount of tuning fixes that, because "the same subject" is a
    semantic judgement, not a frequency.

    So this asks the model, which is what the rest of the pipeline already pays
    for. Thread ids come back checked against the outline, on the same rule
    filing uses: an id that does not exist is reported as unknown rather than
    silently naming a thread nobody can open.
    """
    outline = load_outline(book_dir)
    known = outline.thread_ids
    title = {t.id: t.title for t in outline.threads}

    items: dict[str, list[str]] = {t.id: [] for t in outline.threads}
    for day in classified_days(book_dir):
        record = load_day(book_dir, day) or {}
        for row in record.get("assignments") or []:
            bucket = items.get(row.get("thread_id"))
            if bucket is not None:
                bucket.append(str(row.get("headline") or ""))

    blocks = []
    for thread_id, headlines in items.items():
        if not headlines:
            continue
        shown = headlines[:per_thread]
        lines = "\n".join(f"    - {h}" for h in shown)
        more = (f"\n    …and {len(headlines) - len(shown)} more"
                if len(headlines) > len(shown) else "")
        blocks.append(f"[{thread_id}] {title[thread_id]}\n{lines}{more}")

    if not blocks:
        return []

    prompt = (
        "This is a book outline's threads and the news items filed into each.\n\n"
        + "\n\n".join(blocks)
        + "\n\nFind subjects that are SPLIT: material about one subject filed "
        "across several threads, so that no thread gathers it. Judge by subject, "
        "not by wording — Israel, Israeli, Gaza and Netanyahu are one subject, "
        "not four.\n\n"
        "Ignore a subject that one thread already holds most of, and ignore "
        "subjects so broad they touch everything. Report only what a reader "
        "would expect to find gathered in one place and cannot.\n\n"
        "Use only the bracketed thread ids above. Reply with JSON only:\n"
        "{\"subjects\": [{\"subject\": \"...\", \"why\": \"one sentence\", "
        "\"thread_ids\": [\"...\", \"...\"]}]}"
    )

    payload = _call(prompt, runner=runner, model=model)
    rows = payload.get("subjects") if isinstance(payload, dict) else None

    out = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("subject"):
            continue
        named = [str(t) for t in (row.get("thread_ids") or [])]
        good = [t for t in named if t in known]
        unknown = [t for t in named if t not in known]
        # One thread is not a split subject, it is a thread.
        if len(good) < 2:
            continue
        out.append({
            "subject": str(row["subject"]),
            "why": str(row.get("why") or ""),
            "thread_ids": good,
            "titles": [title[t] for t in good],
            "unknown": unknown,
        })
    return out


# --- the review rollup -----------------------------------------------------


def rollup(book_dir: str | Path, news_dir: str | Path) -> dict:
    """What the review page renders: the outline with its items hung on it.

    Provenance is computed here rather than stored. The handles are already in
    `news/*.md` on every topic's `sources:` line, and a second copy in the day
    files would be one more thing that can drift out of agreement with the
    archive. It is the number that answers the corpus's central weakness: a
    thread fed entirely by one account is that account's preoccupation, not a
    theme, and the page should make that impossible to miss.
    """
    from src import digest

    outline = load_outline(book_dir)
    news = Path(news_dir)

    items: dict[str, list[dict]] = {t.id: [] for t in outline.threads}
    unfiled: list[dict] = []

    for day in classified_days(book_dir):
        record = load_day(book_dir, day) or {}
        path = news / f"{day.isoformat()}.md"
        by_headline = (
            {t.headline.strip(): t for t in digest.kept_of(path)}
            if path.exists() else {}
        )

        for row in record.get("assignments") or []:
            bucket = items.get(row.get("thread_id"))
            if bucket is None:
                continue          # thread retired since this day was filed
            topic = by_headline.get(str(row.get("headline") or "").strip())
            bucket.append({
                "date": day.isoformat(),
                "headline": row.get("headline"),
                "sources": list(topic.sources) if topic else [],
                "tags": list(topic.tags) if topic else [],
            })

        for row in record.get("unfiled") or []:
            unfiled.append({"date": day.isoformat(), **row})

    threads = []
    for thread in outline.threads:
        rows = sorted(items[thread.id], key=lambda r: r["date"])
        threads.append({
            "id": thread.id,
            "title": thread.title,
            "description": thread.description,
            "count": len(rows),
            "first_date": rows[0]["date"] if rows else None,
            "last_date": rows[-1]["date"] if rows else None,
            "handles": _handle_counts(rows),
            "items": rows,
        })

    by_id = {t["id"]: t for t in threads}
    parts = [
        {
            "id": part.id,
            "title": part.title,
            "threads": [by_id[tid] for tid in part.thread_ids if tid in by_id],
        }
        for part in outline.parts
    ]

    placed = {tid for part in outline.parts for tid in part.thread_ids}
    return {
        "version": outline.version,
        "revised_at": outline.revised_at,
        "parts": parts,
        # A thread belonging to no part is a real state, not an error: a
        # revision can add one before deciding where it goes. Surfacing it is
        # what stops it from being invisible.
        "orphan_threads": [t for t in threads if t["id"] not in placed],
        "unfiled": sorted(unfiled, key=lambda r: r["date"], reverse=True),
    }


def _handle_counts(rows: Iterable[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        for handle in row.get("sources") or []:
            counts[handle] = counts.get(handle, 0) + 1
    return [
        {"handle": h, "count": n}
        for h, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _first_sentence(body: str, limit: int = 220) -> str:
    text = " ".join(body.split())
    cut = text.find(". ")
    if 0 < cut < limit:
        return text[: cut + 1]
    return text[:limit]

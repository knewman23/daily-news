#!/usr/bin/env python3
"""Derive and maintain the book outline over the archive.

    .venv/bin/python book.py bootstrap     # derive the first outline
    .venv/bin/python book.py sync          # file every unclassified day
    .venv/bin/python book.py sync --refile # also re-file days on an old version
    .venv/bin/python book.py status        # what is filed, what is not

`book/` is gitignored here and is its own repository pointing at a private
remote. Nothing it holds may reach `site/`, which is committed to a public
repository and served by GitHub Pages.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from src import chapters, config, digest

log = logging.getLogger("book")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maintain the book outline.")
    parser.add_argument("--config", default=config.CONFIG_FILE)
    parser.add_argument("--book", default="book", help="Where the outline lives.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="Derive the first outline from the archive.")
    sub.add_parser("status", help="Show what is filed and what is not.")

    sync = sub.add_parser("sync", help="Classify days that have no day file.")
    sync.add_argument("--refile", action="store_true",
                      help="Also re-file days stamped with an older version.")
    sync.add_argument("--limit", type=int, default=None,
                      help="Stop after this many days.")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    cfg = config.load(args.config)
    book = Path(args.book)

    if args.command == "bootstrap":
        return _bootstrap(cfg, book)
    if args.command == "sync":
        return _sync(cfg, book, refile=args.refile, limit=args.limit)
    return _status(cfg, book)


def _bootstrap(cfg, book: Path) -> int:
    if chapters.outline_path(book).exists():
        log.error("%s already exists; bootstrap will not overwrite it",
                  chapters.outline_path(book))
        return 1

    days = chapters.archive_topics(cfg.paths.news)
    groups = chapters.batches(days)
    if not groups:
        log.error("no topics in %s to derive an outline from", cfg.paths.news)
        return 1

    log.info("deriving candidates from %d day(s) in %d batch(es)",
             len(days), len(groups))

    candidates = []
    for label, topics in groups:
        found = chapters.candidate_threads(label, topics)
        log.info("%s: %d candidate thread(s) from %d topic(s)",
                 label, len(found), len(topics))
        candidates.extend(found)

    log.info("consolidating %d candidate(s)", len(candidates))
    outline = chapters.consolidate(candidates, revised_at=_today())

    chapters.save_outline(book, outline)
    log.info("wrote %s: %d thread(s) in %d part(s)",
             chapters.outline_path(book), len(outline.threads), len(outline.parts))
    for part in outline.parts:
        log.info("  %s", part.title)
        for tid in part.thread_ids:
            thread = outline.thread(tid)
            if thread:
                log.info("      %s", thread.title)
    return 0


def _sync(cfg, book: Path, refile: bool, limit: int | None) -> int:
    outline = chapters.load_outline(book)
    news = Path(cfg.paths.news)

    wanted = [
        meta.date for meta in sorted(digest.list_days(news), key=lambda m: m.date)
        if not chapters.is_classified(book, meta.date)
    ]
    if refile:
        wanted = sorted(set(wanted) | set(chapters.stale_days(book)))
    if limit:
        wanted = wanted[:limit]

    if not wanted:
        log.info("nothing to file; every day is on outline version %d",
                 outline.version)
        return 0

    log.info("filing %d day(s) against outline version %d",
             len(wanted), outline.version)

    failed = 0
    for day in wanted:
        path = news / f"{day.isoformat()}.md"
        try:
            record = chapters.classify_day(book, day, digest.topics_of(path))
        except chapters.ChaptersError as exc:
            # One bad day does not stop the rest: the archive is on disk and a
            # re-run costs only that day.
            log.error("%s: %s", day, exc)
            failed += 1
            continue
        log.info("%s: %d filed, %d unfiled",
                 day, len(record["assignments"]), len(record["unfiled"]))

    if failed:
        log.error("%d day(s) failed; re-run to retry them", failed)
    return 1 if failed else 0


def _status(cfg, book: Path) -> int:
    outline = chapters.load_outline(book)
    news = Path(cfg.paths.news)
    all_days = [m.date for m in digest.list_days(news)]
    filed = chapters.classified_days(book)
    stale = chapters.stale_days(book)

    roll = chapters.rollup(book, news)
    print(f"outline version {outline.version} ({outline.revised_at})")
    print(f"{len(outline.threads)} thread(s) in {len(outline.parts)} part(s)")
    print(f"{len(filed)}/{len(all_days)} day(s) filed, {len(stale)} stale")
    print(f"{len(roll['unfiled'])} unfiled item(s)")

    for part in roll["parts"]:
        print(f"\n{part['title']}")
        for thread in part["threads"]:
            handles = thread["handles"]
            lead = handles[0] if handles else None
            only = (" [single source: %s]" % lead["handle"]) if (
                lead and len(handles) == 1 and thread["count"] > 2) else ""
            print(f"  {thread['count']:4}  {thread['title']}{only}")
    return 0


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


if __name__ == "__main__":
    sys.exit(main())

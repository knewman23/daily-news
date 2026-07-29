#!/usr/bin/env python3
"""Collect past days without tripping Instagram's limits.

    .venv/bin/python backfill.py --days 30              # plan only, nothing downloaded
    .venv/bin/python backfill.py --days 30 --execute    # download, paced

**One profile walk per handle, not one per day.** `run_daily --date X` walks every
profile to build a single day, so thirty days would be thirty walks per handle —
210 walks for what 7 can collect. This walks each profile once, back to the start
of the range, and files every post into the day folder it belongs to. That
difference is the whole point: the request pattern is what gets an account
flagged, not the volume of video downloaded from the CDN.

Everything else here is about being able to stop safely:

  * A plan runs first and downloads nothing, so the cost is known before it is
    paid.
  * A rate-limit response aborts the whole run immediately. It is never retried —
    a 429 is Instagram asking for less, and answering with more is how a slowdown
    becomes a suspension.
  * Progress is on disk after every post, so an aborted run resumes rather than
    restarting.
  * instaloader's own rate controller is left switched on, and extra delays are
    added on top between handles.

This does not summarize anything. Run `run_daily.py --date <day> --no-fetch` per
day afterwards, which needs no network at all.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from instaloader import exceptions as ig

from src import config, fetch, sources
from src.records import PostRef

log = logging.getLogger("backfill")

# Deliberately conservative. Instagram's actual thresholds are undocumented and
# change; these are chosen to look like a person browsing, not a crawler.
DEFAULT_DELAY_BETWEEN_HANDLES = 90.0
DEFAULT_DELAY_BETWEEN_POSTS = 2.0
DEFAULT_MAX_POSTS_PER_HANDLE = 250

# Walking past this many posts for one handle means the range is bigger than the
# profile, or the date filter is not matching. Either way, stop.
SCAN_CEILING = 1200


class RateLimited(Exception):
    """Instagram asked for less. The run stops."""


@dataclass
class Plan:
    handle: str
    by_day: dict[date, list[Any]] = field(default_factory=lambda: defaultdict(list))
    scanned: int = 0
    truncated_reason: str = ""

    @property
    def total(self) -> int:
        return sum(len(posts) for posts in self.by_day.values())


def enumerate_handle(
    loader: Any,
    handle: str,
    start: date,
    end: date,
    max_posts: int,
    scan_ceiling: int = SCAN_CEILING,
) -> Plan:
    """List a handle's posts inside a date range, downloading nothing.

    Pagination carries full post metadata, so this costs roughly one request per
    twelve posts rather than one per post.
    """
    plan = Plan(handle=handle)

    for post in loader.posts_for(handle):
        plan.scanned += 1
        if plan.scanned > scan_ceiling:
            plan.truncated_reason = f"scan ceiling of {scan_ceiling} posts reached"
            break

        posted = _utc(post.date_utc)
        day = posted.astimezone().date()

        if day > end:
            continue                    # newer than the range, or a pinned post
        if day < start:
            # Pinned posts appear before the chronological run and can be years
            # old, so one old post is not the end of the range.
            if plan.scanned > fetch.PINNED_TOLERANCE and _past_range(plan, start):
                break
            continue

        plan.by_day[day].append(post)
        if plan.total >= max_posts:
            plan.truncated_reason = f"per-handle cap of {max_posts} posts reached"
            break

    return plan


def download_plan(
    loader: Any,
    plan: Plan,
    raw_root: Path,
    delay_between_posts: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int]:
    """Download a handle's planned posts. Returns (posts filed, media written)."""
    filed = 0
    written = 0

    for day in sorted(plan.by_day):
        dest = raw_root / day.isoformat()

        for post in plan.by_day[day]:
            try:
                new_files = _download_one(loader, plan.handle, post, dest)
            except ig.TooManyRequestsException as exc:
                raise RateLimited(f"{plan.handle}: {exc}") from exc
            except Exception as exc:
                log.warning("skipping %s/%s: %s", plan.handle, post.shortcode, exc)
                continue

            filed += 1
            written += new_files
            if new_files:
                sleep(delay_between_posts)

    return filed, written


def run(
    cfg,
    days: int,
    handles: list[str] | None = None,
    execute: bool = False,
    max_posts: int = DEFAULT_MAX_POSTS_PER_HANDLE,
    delay_handles: float = DEFAULT_DELAY_BETWEEN_HANDLES,
    delay_posts: float = DEFAULT_DELAY_BETWEEN_POSTS,
    loader: Any = None,
    today: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Plan, and optionally execute, a backfill. Returns a process exit code."""
    end = (today or date.today()) - timedelta(days=1)   # yesterday; today is the daily run's job
    start = end - timedelta(days=days - 1)

    wanted = [
        s.handle for s in sources.enabled_sources(cfg.paths.sources)
        if not handles or s.handle in handles
    ]
    if not wanted:
        log.error("no enabled handles matched")
        return 1

    log.info("range %s .. %s (%d day(s)) across %d handle(s)",
             start.isoformat(), end.isoformat(), days, len(wanted))

    client = loader or fetch.make_loader(cfg.fetch)
    plans: list[Plan] = []

    for index, handle in enumerate(wanted):
        try:
            plan = enumerate_handle(client, handle, start, end, max_posts)
        except ig.TooManyRequestsException as exc:
            log.error("rate limited while listing %s — stopping here", handle)
            log.error("%s", exc)
            _report(plans, start, end)
            return 2
        except Exception as exc:
            log.warning("could not list %s: %s", handle, exc)
            continue

        plans.append(plan)
        log.info("  %-24s %3d post(s) across %2d day(s), %d scanned%s",
                 handle, plan.total, len(plan.by_day), plan.scanned,
                 f" — {plan.truncated_reason}" if plan.truncated_reason else "")

        if index < len(wanted) - 1:
            sleep(delay_handles)

    _report(plans, start, end)

    if not execute:
        print("\nPlan only. Re-run with --execute to download.")
        return 0

    return _execute(client, plans, cfg, delay_handles, delay_posts, sleep)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill past days with one profile walk per handle.",
    )
    parser.add_argument("--days", type=int, default=7,
                        help="How many days back from yesterday. Default 7.")
    parser.add_argument("--handles", default="",
                        help="Comma-separated subset. Default: every enabled handle.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually download. Without this, only a plan is printed.")
    parser.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS_PER_HANDLE)
    parser.add_argument("--delay-handles", type=float, default=DEFAULT_DELAY_BETWEEN_HANDLES,
                        help="Seconds to wait between handles. Default 90.")
    parser.add_argument("--delay-posts", type=float, default=DEFAULT_DELAY_BETWEEN_POSTS,
                        help="Seconds to wait after each downloaded post. Default 2.")
    parser.add_argument("--config", default=config.CONFIG_FILE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    return run(
        config.load(args.config),
        days=args.days,
        handles=[h.strip() for h in args.handles.split(",") if h.strip()] or None,
        execute=args.execute,
        max_posts=args.max_posts,
        delay_handles=args.delay_handles,
        delay_posts=args.delay_posts,
    )


# --- internals -------------------------------------------------------------


def _execute(client, plans, cfg, delay_handles, delay_posts, sleep) -> int:
    total_filed = 0
    total_written = 0

    for index, plan in enumerate(plans):
        log.info("downloading %s (%d post(s))", plan.handle, plan.total)
        try:
            filed, written = download_plan(
                client, plan, cfg.paths.raw, delay_posts, sleep=sleep,
            )
        except RateLimited as exc:
            log.error("rate limited: %s", exc)
            log.error("stopping. %d post(s) filed so far are on disk; re-run to resume.",
                      total_filed)
            return 2

        total_filed += filed
        total_written += written
        log.info("  %s: %d post(s) filed, %d media file(s) written",
                 plan.handle, filed, written)

        if index < len(plans) - 1:
            sleep(delay_handles)

    days_touched = sorted({day for plan in plans for day in plan.by_day})
    log.info("filed %d post(s), wrote %d media file(s), across %d day(s)",
             total_filed, total_written, len(days_touched))

    if days_touched:
        print("\nNow summarize each day (no network needed):")
        for day in days_touched:
            print(f"  .venv/bin/python run_daily.py --date {day.isoformat()} --no-fetch --quiet")

    return 0


def _report(plans: list[Plan], start: date, end: date) -> None:
    if not plans:
        print("\nNothing found.")
        return

    by_day: dict[date, int] = defaultdict(int)
    for plan in plans:
        for day, posts in plan.by_day.items():
            by_day[day] += len(posts)

    total = sum(by_day.values())
    scanned = sum(plan.scanned for plan in plans)

    print(f"\n{'day':12}  posts")
    day = start
    while day <= end:
        print(f"{day.isoformat():12}  {by_day.get(day, 0)}")
        day += timedelta(days=1)

    # Roughly 7 MB per video and 0.3 MB per image on the days measured so far.
    print(f"\n{total} post(s) over {len(by_day)} day(s) with content")
    print(f"{scanned} post(s) listed from {len(plans)} profile walk(s)")
    print(f"~{total * 5 / 1024:.1f} GB of media, deleted again by [retain] media_days")
    print(f"~{total * 12 / 60:.0f} min of transcription")


def _download_one(loader, handle: str, post: Any, dest: Path) -> int:
    """Save one post's media and sidecar. Returns how many files were written."""
    stem = f"{handle}_{post.shortcode}"
    written = 0

    if post.is_video:
        mp4 = dest / f"{stem}.mp4"
        if not mp4.exists():
            loader.download(post, mp4)
            written += 1
        kind = "video"
    else:
        urls = loader.slide_urls(post)
        if not urls:
            return 0
        single = len(urls) == 1
        for number, url in enumerate(urls, start=1):
            target = dest / (f"{stem}.jpg" if single else f"{stem}_{number}.jpg")
            if not target.exists():
                loader.download_image(url, target)
                written += 1
        kind = "image"

    posted = _utc(post.date_utc)
    fetch._write_sidecar(dest / f"{stem}.json", PostRef(
        handle=handle,
        shortcode=post.shortcode,
        posted_at=posted.isoformat(),
        permalink=f"https://www.instagram.com/p/{post.shortcode}/",
        caption=post.caption or "",
    ), kind)

    return written


def _past_range(plan: Plan, start: date) -> bool:
    """True once the walk has clearly left the range rather than hit a pin."""
    return plan.total > 0 or plan.scanned > fetch.PINNED_TOLERANCE * 4


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


if __name__ == "__main__":
    sys.exit(main())

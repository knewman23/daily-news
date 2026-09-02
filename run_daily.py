#!/usr/bin/env python3
"""The 11am run. Entry point for launchd.

fetch -> transcribe -> ocr -> summarize, in that order. Every stage is keyed on
output existence, so re-running a day redoes only what is missing: a failure at
the summarize step costs seconds to retry rather than a fresh round of downloads
and whisper passes.

Two invariants matter more than the happy path.

A day always gets a file when there is anything to say about it, even if that
file says "no posts found" — otherwise a silently broken pipeline is
indistinguishable from a quiet news day.

And a re-run never costs the user their journal entry. summarize writes a fresh
file with empty note markers, so the existing notes are read first and written
back afterwards.
"""

from __future__ import annotations

import argparse
import logging
import random
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

from src import (config, digest, fetch, mailer, notes, ocr, posts, prune,
                 publish, runlog, sources, summarize, transcribe)
from src.records import Stats

log = logging.getLogger("daily-news")


def run_day(
    day: date,
    cfg: config.Config,
    fetcher=None,
    transcriber=None,
    ocr_runner=None,
    summarizer=None,
    publisher=None,
    emailer=None,
    pruner=None,
    notifier=None,
    generated: datetime | None = None,
    full: bool = False,
    skip_fetch: bool = False,
    quiet: bool = False,
    jitter: bool = False,
    sleep=None,
    pick=None,
) -> int:
    """Run one day end to end. Returns a process exit code."""
    _setup_logging(cfg, day)

    # Before `started`, deliberately: the wait is not work, and billing it to
    # the run would report a 45-minute pause as a 45-minute pipeline.
    if jitter and not skip_fetch:
        _wait_out_jitter(cfg.fetch.start_jitter_seconds,
                         sleep or time.sleep, pick or random.uniform)

    started = runlog.now()

    # Assigned before record() below, which closes over it.
    notify = (lambda _m: None) if quiet else (notifier or _notify)

    def record(ok: bool, error: str | None = None, stats: Stats | None = None,
               spoken: int = 0, on_image: int = 0, topics: int = 0) -> int:
        """Write the run history entry, say how it went, and return the exit code."""
        entry = runlog.RunRecord(
            started_at=started,
            finished_at=runlog.now(),
            date=day.isoformat(),
            ok=ok,
            post_count=stats.post_count if stats else 0,
            transcribed_count=stats.transcribed_count if stats else 0,
            spoken_count=spoken,
            image_count=on_image,
            topic_count=topics,
            incomplete=bool(stats.incomplete) if stats else False,
            error=error,
            failures=list(stats.notes) if stats else [],
            skipped=list(stats.skipped) if stats else [],
        )
        runlog.append(cfg.paths.logs, entry)
        # Every return path funnels through here, so this is the one place that
        # cannot be forgotten when another early exit is added later.
        #
        # Good outcomes only. A run that failed has already sent a banner saying
        # what broke, and following it with one saying the run finished would be
        # both a duplicate and a lie about the exit code.
        if ok:
            notify(f"{day.isoformat()} — {_outcome(entry)}")
        return 0 if ok else 1

    do_fetch = fetcher or fetch.fetch_day
    do_transcribe = transcriber or transcribe.transcribe_day
    do_ocr = ocr_runner or ocr.ocr_day
    do_summarize = summarizer or summarize.summarize_day
    do_publish = publisher or publish.publish
    do_email = emailer or mailer.send
    do_prune = pruner or prune.prune

    log.info("starting run for %s", day.isoformat())
    # Before the disabled-sources check below: the run has begun either way, and
    # a start banner with no matching finish is itself worth seeing.
    notify(f"Starting the run for {day.isoformat()}")

    enabled = sources.enabled_sources(cfg.paths.sources)
    if not enabled:
        log.info("every source is disabled, nothing to do")
        return record(True, error="every source is disabled")

    raw = cfg.raw_dir(day)
    extracted = cfg.transcripts_dir(day)

    if skip_fetch:
        # Backfilled days already have their media and sidecars on disk. Fetching
        # would walk every profile again for nothing, which is exactly the request
        # pattern worth avoiding.
        log.info("skipping fetch: working from what is already on disk")
        fetched, fetch_stats = [], Stats()
    else:
      try:
        since = fetch.start_of_day(day) if full else None
        if full:
            log.info("full pull: ignoring watermarks, scanning from %s", since.isoformat())
        fetched, fetch_stats = do_fetch(cfg.paths.sources, raw, cfg.fetch, since=since)
      except fetch.ActionBlocked as exc:
        # Deliberately not the re-authenticate advice below: the session is
        # fine and a fresh one does not help. Verified on 2026-09-02, where
        # re-exporting cookies left the block exactly where it was.
        log.error("Instagram blocked the request pattern: %s", exc)
        log.error(
            "Nothing to fix here. Today is lost; no watermark moved, so the "
            "next run picks up the same window. Do not re-run by hand."
        )
        notify("Instagram temporarily blocked automated access — waiting it out")
        return record(False, error=f"action blocked: {exc}")
      except fetch.SessionExpired as exc:
        log.error("Instagram session is not usable: %s", exc)
        # Only the instaloader backend is fixed by re-exporting cookies. The
        # chrome backend raises a ChromeUnavailable -- itself a SessionExpired,
        # so it aborts the same way -- and its message says what to do instead.
        if cfg.fetch.backend == "instaloader":
            log.error(
                "Re-authenticate with:\n"
                "  .venv/bin/instaloader --load-cookies chrome "
                "--sessionfile ~/.config/instaloader/session-%s",
                cfg.fetch.session_user,
            )
        notify("Instagram session expired — re-authenticate")
        return record(False, error=f"session expired: {exc}")
      except Exception as exc:
        log.exception("fetch failed outright: %s", exc)
        notify(f"Daily news fetch failed: {exc}")
        return record(False, error=f"fetch failed: {exc}")

    spoken, audio_stats = do_transcribe(raw, extracted, cfg.transcribe)
    on_image, image_stats = do_ocr(raw, extracted, cfg.transcribe)

    transcripts = list(spoken) + list(on_image)
    stats = _merge(fetch_stats, audio_stats, image_stats)

    # Media with no sidecar belongs to no post, so nothing would ever transcribe
    # it and it would vanish from the digest without a word.
    stray = posts.orphans(raw)
    if stray:
        stats.fail(f"{len(stray)} media file(s) with no caption sidecar: "
                   f"{', '.join(p.name for p in stray[:3])}")
    # "in the fetch window", not "downloaded": fetch returns a ref for every post
    # inside the cutoff whether or not it had to download it, so calling this
    # "newly fetched" made a full re-pull look like it re-downloaded the day.
    log.info(
        "%d post(s) for the day (%d in the fetch window), %d with usable text "
        "(%d spoken, %d on-image)",
        stats.post_count, len(fetched), stats.transcribed_count,
        len(spoken), len(on_image),
    )

    path = cfg.paths.news / f"{day.isoformat()}.md"
    carried = _existing_notes(path)

    try:
        do_summarize(day, transcripts, stats, cfg.paths.news,
                     generated=generated, interests=cfg.interests)
    except Exception as exc:
        log.exception("summarize failed: %s", exc)
        log.error("transcripts are on disk, so re-running this date is cheap")
        notify(f"Daily news summary failed: {exc}")
        return record(False, error=f"summarize failed: {exc}", stats=stats,
                      spoken=len(spoken), on_image=len(on_image))

    if carried:
        notes.write_notes(path, carried)
        log.info("carried %d character(s) of journal notes into the new file", len(carried))

    for note in stats.notes:
        log.warning("partial failure: %s", note)

    for note in stats.skipped:
        log.info("off topic, left out: %s", note)

    # kept_of, not topics_of: the digest also stores what the filter dropped, and
    # counting those here would inflate the run record's topic count with news the
    # reader never saw.
    topics = len(digest.kept_of(path)) if path.exists() else 0
    log.info("wrote %s with %d topic(s)%s", path, topics,
             " (incomplete)" if stats.incomplete else "")

    # An incomplete day is not notified here: record() carries the problem count
    # in the completion banner, so the day is reported once rather than twice.

    # Publishing comes last and cannot fail the run: the digest is written and
    # readable locally, so being unable to reach GitHub is worth reporting
    # rather than worth discarding a successful run over.
    if quiet:
        log.info("quiet: not publishing or emailing this day")
        return record(True, stats=stats, spoken=len(spoken),
                      on_image=len(on_image), topics=topics)

    outcome = do_publish(cfg, day, topic_count=topics)
    if not outcome.ok:
        log.warning("publish did not complete: %s", outcome.message)
        notify(f"Daily news published locally but not pushed: {outcome.message}")
        stats.fail(f"publish: {outcome.message}")

    # The email is the last thing and, like publishing, cannot fail the run.
    # It is sent after publishing so the link in it points at a live page.
    subject, body = mailer.build_message(
        day,
        # Kept only. A dropped topic is already reported in the email's skipped
        # section; listing it as a headline too would contradict that.
        [t.headline for t in digest.kept_of(path)] if path.exists() else [],
        stats,
        site_url=cfg.email.site_url,
        failures=stats.notes,
        skipped=stats.skipped,
    )
    delivery = do_email(cfg, subject, body)
    if not delivery.ok:
        log.warning("email did not send: %s", delivery.message)

    # Reclaiming disk comes last, after the day is summarized, published, and
    # sent — nothing downstream can then be missing its source.
    reclaimed = do_prune(
        cfg.paths.raw, cfg.paths.transcripts, cfg.paths.news,
        cfg.retain.media_days,
    )
    if reclaimed.files:
        log.info("reclaimed %s MB from %d old media file(s)",
                 reclaimed.megabytes_freed, reclaimed.files)

    return record(True, stats=stats, spoken=len(spoken),
                  on_image=len(on_image), topics=topics)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one day's news digest.")
    parser.add_argument(
        "--date", type=_a_date, default=date.today(),
        help="YYYY-MM-DD, for backfill or a re-run. Defaults to today.",
    )
    parser.add_argument(
        "--config", default=config.CONFIG_FILE, help="Path to config.toml.",
    )
    parser.add_argument(
        "--no-fetch", dest="no_fetch", action="store_true",
        help="Summarize from media already on disk, without contacting Instagram. "
             "Use for days collected by backfill.py.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Do not publish, email, or notify. Use when backfilling many days.",
    )
    parser.add_argument(
        "--jitter", action="store_true",
        help="Wait a random part of [fetch] start_jitter_seconds before "
             "contacting Instagram. For the scheduled run: firing at the same "
             "second every day is a pattern. Not for interactive use.",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Ignore the per-handle watermarks and re-scan the whole day. Use "
             "after a partial run, or to pick up posts an earlier run missed.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_day(args.date, config.load(args.config), full=args.full,
                   skip_fetch=args.no_fetch, quiet=args.quiet,
                   jitter=args.jitter)


# --- internals -------------------------------------------------------------


def _wait_out_jitter(span_seconds: int, sleep, pick) -> None:
    """Wait a random part of `span_seconds` before the run touches Instagram.

    Only the scheduled run passes `--jitter`. A launchd job fires at 11:00:00
    to the second, and seven handles pulled in the same order at the same
    instant every day is a pattern no choice of backend hides. A manual run
    stalling for three quarters of an hour would be its own bug, which is why
    this is opt-in rather than automatic.
    """
    if span_seconds <= 0:
        return

    delay = pick(0, span_seconds)
    log.info("jitter: waiting %s before contacting Instagram", _elapsed(delay))
    sleep(delay)


def _merge(fetched: Stats, spoken: Stats, on_image: Stats) -> Stats:
    """Combine stage stats into the day's totals.

    post_count is the sum of the *extract* stages, not fetch's count. Fetch only
    reports what it newly downloaded, so on a re-run — where everything is
    already on disk and nothing is fetched — taking its count would report a day
    of 28 posts as a day of 0. transcribe counts the videos and ocr counts the
    image posts, and the two sets do not overlap, so their sum is the real total.

    Fetch still contributes its failures: a handle that could not be reached is
    the main reason a day is incomplete.
    """
    merged = Stats(post_count=spoken.post_count + on_image.post_count)
    for part in (fetched, spoken, on_image):
        merged.transcribed_count += part.transcribed_count
        merged.incomplete = merged.incomplete or part.incomplete
        merged.notes.extend(part.notes)
    return merged


def _existing_notes(path: Path) -> str:
    """Read the journal block before it is overwritten.

    A malformed block must not abort the run: the day's news is worth writing
    even when a hand-edit broke the markers. The failure is logged loudly because
    it does mean notes are being dropped.
    """
    if not path.exists():
        return ""
    try:
        return notes.read_notes(path)
    except notes.NotesMarkerError as exc:
        log.error("could not read existing notes from %s, they will be lost: %s", path, exc)
        return ""


def _outcome(entry: runlog.RunRecord) -> str:
    """How a finished run reads on the desktop: what it produced, and how long it took."""
    # A run that is ok and carries an error had nothing to do rather than nothing
    # to show. Reporting "0 topics" for it would read as a broken summarize.
    if entry.error:
        return entry.error

    elapsed = _elapsed(entry.duration_seconds)
    if not entry.post_count:
        body = f"no posts found in {elapsed}"
    elif not entry.topic_count:
        # A day of posts the interest filter kept nothing from is a different
        # thing from a day with no posts, and only one of the two is worth
        # checking the filter over.
        body = f"nothing kept from {_count(entry.post_count, 'post')} in {elapsed}"
    else:
        body = (f"{_count(entry.topic_count, 'topic')} from "
                f"{_count(entry.post_count, 'post')} in {elapsed}")
    if entry.incomplete:
        body += f" ({_count(len(entry.failures), 'problem')} — see Runs)"
    return body


def _elapsed(seconds: float) -> str:
    """A duration as the Runs panel writes it: 8s, 6m12s, 1h23m."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


NOTIFY_TITLE = "Daily News"

# A Shortcut, because on macOS 26 nothing else on this machine could display a
# notification at all. See _notify.
NOTIFY_SHORTCUT = "Daily News Notify"


def _notify(message: str, runner=subprocess.run) -> None:
    """Surface a run on the desktop.

    An unattended run that fails silently is worse than one that fails loudly:
    the first sign of trouble would otherwise be a digest that never appeared.
    The 11am run is unattended by definition, so it also says when it starts and
    what it produced — a banner that never arrived is a failure that never
    reported itself.

    The transport is a Shortcut, which reads like an odd choice and is not. On
    macOS 26, an app must be registered in Notification Center to post one, and
    a command-line tool cannot register: `osascript` posts as Script Editor,
    which is absent from `com.apple.ncprefs`, and terminal-notifier is ad-hoc
    signed, which macOS 26 refuses to register no matter where it is installed
    or how it is re-signed. Both exit 0 while displaying nothing, because
    submitting a notification and displaying one are different things — which is
    how the pre-existing failure banners in this file went years without anyone
    noticing they were never shown.

    Shortcuts is registered and allowed, so a Shortcut wrapping "Show
    Notification" does display. `shortcuts run` passes its input as a file, so
    the message goes through a temporary file and the Shortcut needs a "Get text
    from Shortcut Input" step ahead of the notification — README documents
    building it.

    osascript stays as the fallback for machines where it does work. Neither can
    fail a run: a notification that cannot be delivered is worth being quiet
    about, and never worth losing a digest over.
    """
    if _has_shortcut(runner):
        # A file, not an argument: `shortcuts run` takes its input by path.
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", prefix="daily-news-notify-",
            encoding="utf-8", delete=False,
        )
        with handle as sink:
            sink.write(message)
        try:
            runner(["shortcuts", "run", NOTIFY_SHORTCUT, "-i", handle.name],
                   check=False, capture_output=True)
        finally:
            Path(handle.name).unlink(missing_ok=True)
        return

    runner(["osascript", "-e",
            f'display notification {message!r} with title "{NOTIFY_TITLE}"'],
           check=False, capture_output=True)


def _has_shortcut(runner) -> bool:
    """Whether the notification Shortcut exists.

    Asked rather than assumed because `shortcuts run` exits 0 for a shortcut it
    could not find, so failure is not detectable from the exit code afterwards.
    """
    done = runner(["shortcuts", "list"], check=False, capture_output=True, text=True)
    listed = getattr(done, "stdout", "") or ""
    return NOTIFY_SHORTCUT in listed.splitlines()


def _setup_logging(cfg: config.Config, day: date) -> None:
    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    logfile = cfg.paths.logs / f"{day.isoformat()}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Re-invoked in-process by the tests; without this each run stacks another
    # pair of handlers and every line is written several times over.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for handler in (logging.FileHandler(logfile, encoding="utf-8"),
                    logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def _a_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


if __name__ == "__main__":
    sys.exit(main())
